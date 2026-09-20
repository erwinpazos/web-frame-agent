import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uuid
import traceback
from typing import Optional, Dict, Any, Set, List
from pathlib import Path

from browser_use import Agent, Browser, Controller
from browser_use.llm.google.chat import ChatGoogle
from browser_use.agent.views import AgentOutput
from browser_use.browser.views import BrowserStateSummary

from app.core.config import settings
from app.core.logger import logger
from app.api.v1.endpoints.cdp_bridge import cdp_bridge
from app.services.unlock_rules_service import unlock_rules_service
class AgentService:
    """Manages Browser-Use agent execution with Google Cloud Vertex AI."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.active_task_id: Optional[str] = None
        self.active_agent: Optional[Agent] = None
        self.active_browser: Optional[Browser] = None
        self.is_running: bool = False
        self._current_task: Optional[asyncio.Task] = None
        self._subscribers: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        """Register a new WebSocket queue listener."""
        q = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Unregister a WebSocket queue listener."""
        self._subscribers.discard(q)
        return None

    async def broadcast(self, event: Dict[str, Any]) -> None:
        """Broadcast an event to all connected WebSocket clients."""
        dead = []
        for q in list(self._subscribers):
            try:
                await q.put(event)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def _get_llm(self, task_id: Optional[str] = None, session_id: Optional[str] = None, trace_obj: Optional[Any] = None) -> Any:
        """Initializes the configured LLM provider through LLMGateway with Langfuse tracing."""
        from app.core.llm_gateway import LLMGateway
        provider = settings.llm_provider
        model_name = settings.llm_model_name
        logger.info(
            f"Initializing LLM via LLMGateway (provider='{provider}', model='{model_name}', "
            f"langfuse_host='{settings.LANGFUSE_HOST or 'offline'}')"
        )
        return LLMGateway.get_browser_use_llm(
            session_id=session_id or task_id,
            trace_id=task_id,
            trace_obj=trace_obj,
        )
    async def start_task(
        self,
        prompt: str,
        start_url: Optional[str] = None,
        session_id: Optional[str] = None,
        max_steps: int = 15,
        headless: bool = settings.default_headless,
    ) -> str:
        """Starts a background navigation task with Browser-Use."""
        async with self._lock:
            if not cdp_bridge.is_extension_connected:
                raise RuntimeError(
                    "The Chrome extension is not connected to the CDP bridge. "
                    f"Open chrome://extensions, click 'Refresh' on the extension, then reload {settings.frontend_url}."
                )

            if not cdp_bridge.is_iframe_ready:
                raise RuntimeError(
                    "The workspace target view is still initializing. Please wait a moment for the page to finish attaching before sending a prompt."
                )
            if self.is_running:
                raise RuntimeError("An agent task is already running.")

            target_url = start_url or settings.default_target_url
            task_id = str(uuid.uuid4())
            resolved_session_id = session_id or f"session-{task_id[:8]}"

            # Immediately mark running inside lock to reject concurrent launches
            self.is_running = True
            self.active_task_id = task_id

            # Launch background worker
            self._current_task = asyncio.create_task(
                self._execute_agent(task_id, prompt, target_url, resolved_session_id, max_steps, headless)
            )
            return task_id
    async def _execute_agent(
        self,
        task_id: str,
        prompt: str,
        start_url: str,
        session_id: str,
        max_steps: int,
        headless: bool,
    ) -> None:
        logger.info(f"Starting agent task {task_id}: '{prompt}' (start_url: {start_url})")

        await self.broadcast({
            "type": "agent_started",
            "task_id": task_id,
            "prompt": prompt,
            "start_url": start_url,
            "max_steps": max_steps,
        })

        from app.core.llm_gateway import LLMGateway, set_current_trace_obj, set_current_span, set_current_session_id, set_current_trace_id

        langfuse_client = LLMGateway.get_langfuse_client()
        trace_obj = None

        if langfuse_client:
            try:
                trace_obj = langfuse_client.trace(
                    id=task_id,
                    name=f"Agent Task: {prompt[:40]}",
                    session_id=session_id,
                    user_id=settings.langfuse_default_user_id,
                    input={
                        "task": prompt,
                        "start_url": start_url,
                        "max_steps": max_steps,
                    },
                    metadata={
                        "provider": "google_vertex",
                        "model": settings.vertex_model_name,
                        "project": settings.google_cloud_project,
                        "location": settings.google_cloud_location,
                    },
                    tags=["browser-use", "agent-task"],
                )
                set_current_trace_obj(trace_obj)
                set_current_trace_id(task_id)
                set_current_session_id(session_id)
                await asyncio.to_thread(langfuse_client.flush)
            except Exception as e:
                logger.warning(f"Error initializing Langfuse trace: {e}")

        try:
            llm = self._get_llm(task_id=task_id, session_id=session_id, trace_obj=trace_obj)

            if not cdp_bridge.is_extension_connected:
                raise RuntimeError(
                    "The Chrome extension is not connected to the CDP bridge. "
                    f"Open chrome://extensions, click 'Refresh' on the extension, then reload {settings.frontend_url}."
                )

            cdp_url = f"http://{settings.backend_host}:{settings.backend_port}/api/v1/cdp"
            logger.info(f"Using Chrome Extension CDP Bridge on {cdp_url} (no separate window)")
            self.active_browser = Browser(cdp_url=cdp_url)
            # Combined instruction - omit raw URL to prevent browser-use from injecting an auto-navigate action
            full_task = (
                "The target web page is already loaded and displayed in the workspace on the right side of the screen.\n"
                f"Task to accomplish: {prompt}\n"
                "Interact directly with the displayed page to accomplish this task (clicks, text input, scrolling, any needed actions).\n"
                "CRITICAL: Once you have completed the requested action or navigated to the destination page, call the 'done' tool immediately. Do not keep clicking or browsing needlessly.\n"
                "CRITICAL INCEPTION GUARD: NEVER navigate to the host application URL (e.g. localhost:5173, 127.0.0.1:5173). The target website is the external web page loaded in the workspace.\n"
                "Be concise, precise, and summarize what you accomplished in English."
            )
            step_count = 0
            current_step_span = None

            async def on_step_start(agent_instance):
                nonlocal step_count, current_step_span
                step_count += 1
                if trace_obj:
                    try:
                        current_step_span = trace_obj.span(
                            name=f"Step {step_count}: Evaluate & Act",
                            input={
                                "step_number": step_count,
                                "url": cdp_bridge.active_tab_info.get("url") or start_url,
                                "title": cdp_bridge.active_tab_info.get("title") or "",
                            },
                            metadata={
                                "step_number": step_count,
                            },
                        )
                        set_current_span(current_step_span)
                        if hasattr(llm, "current_span"):
                            llm.current_span = current_step_span
                    except Exception as e:
                        logger.warning(f"Error creating step span in Langfuse: {e}")

            async def on_step(state: BrowserStateSummary, model_output: AgentOutput, step_number: int):
                nonlocal current_step_span
                # 1. Step count safety cap
                if step_number > settings.max_task_llm_steps:
                    raise RuntimeError(
                        f"budget_exceeded: Safety cap reached (max {settings.max_task_llm_steps} steps)."
                    )

                # 2. Cumulative USD cost kill-switch
                current_cost = getattr(llm, "cumulative_cost_usd", 0.0)
                if current_cost > settings.max_task_cost_usd:
                    logger.error(
                        f"Cost kill-switch triggered: ${current_cost:.4f} exceeded threshold of ${settings.max_task_cost_usd:.2f}."
                    )
                    raise RuntimeError(
                        f"cost_exceeded: Budget limit reached (${current_cost:.4f} > ${settings.max_task_cost_usd:.2f})."
                    )
                if current_step_span:
                    try:
                        current_step_span.end(
                            output={
                                "thinking": model_output.thinking or "",
                                "actions": [a.model_dump(exclude_none=True) if hasattr(a, "model_dump") else str(a) for a in (model_output.action or [])],
                                "next_goal": model_output.next_goal,
                            }
                        )
                    except Exception:
                        pass
                    current_step_span = None
                    set_current_span(None)
                    if hasattr(llm, "current_span"):
                        llm.current_span = None

                actions_repr: List[Dict[str, Any]] = []
                if model_output.action:
                    for act in model_output.action:
                        try:
                            actions_repr.append(act.model_dump(exclude_none=True))
                        except Exception:
                            actions_repr.append({"raw": str(act)})
                reported_url = cdp_bridge.active_tab_info.get("url") or start_url
                current_cost = getattr(llm, "cumulative_cost_usd", 0.0)
                step_data = {
                    "type": "agent_step",
                    "task_id": task_id,
                    "step_number": step_number,
                    "max_steps": min(max_steps, settings.max_task_llm_steps),
                    "thinking": model_output.thinking or "",
                    "current_url": reported_url,
                    "actions": actions_repr,
                    "next_goal": model_output.next_goal,
                    "cumulative_cost_usd": round(current_cost, 4),
                }
                logger.info(f"Agent step {step_number}/{max_steps} on {reported_url} (cost: ${current_cost:.4f})")
                await self.broadcast(step_data)
            controller = Controller()

            @controller.action("probe_and_unlock_iframe")
            async def probe_and_unlock_iframe(url: str) -> str:
                """Diagnostic and adaptive unlock tool for iframe web navigation. Call this if page content is blank or blocked."""
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    domain = parsed.hostname.lower() if parsed.hostname else url.lower()
                    rule = unlock_rules_service.get_rule(domain)
                    status_info = rule.status.value if rule else "no_rule_found"
                    headers = [h.value for h in rule.headers_stripped] if rule else []
                    patches = [p.value for p in rule.js_patches] if rule else []

                    msg = (
                        f"Iframe status for {domain}: {status_info}. "
                        f"Stripped headers: {headers}. Active patches: {patches}."
                    )
                    logger.info(f"[Agent Tool] probe_and_unlock_iframe called for {url}: {msg}")
                    await self.broadcast({
                        "type": "unlock_probe",
                        "task_id": task_id,
                        "domain": domain,
                        "status": status_info,
                        "headers": headers,
                        "patches": patches,
                    })
                    return msg
                except Exception as e:
                    logger.error(f"[Agent Tool] Error in probe_and_unlock_iframe for {url}: {e}", exc_info=True)
                    return f"Error probing iframe for {url}: {e}"

            self.active_agent = Agent(
                task=full_task,
                llm=llm,
                browser=self.active_browser,
                controller=controller,
                max_steps=max_steps,
                register_new_step_callback=on_step,
                use_vision=True,
            )
            history = await self.active_agent.run(max_steps=max_steps, on_step_start=on_step_start)
            final_result = history.final_result() or "Task completed successfully."
            is_successful = history.is_successful()
            if current_step_span:
                try:
                    current_step_span.end(output={"final_result": final_result})
                except Exception:
                    pass
                current_step_span = None
                set_current_span(None)

            if trace_obj:
                try:
                    trace_obj.update(
                        output={"result": final_result, "success": is_successful},
                        metadata={"status": "completed" if is_successful else "finished"},
                    )
                    await asyncio.to_thread(langfuse_client.flush)
                except Exception as e:
                    logger.warning(f"Error finalizing Langfuse trace: {e}")

            logger.info(f"Agent task {task_id} completed (success={is_successful})")
            
            # Stream the final response in granular token-like chunks via SSE & WebSocket
            chunk_size = settings.stream_chunk_size
            for i in range(0, len(final_result), chunk_size):
                chunk = final_result[i : i + chunk_size]
                await self.broadcast({
                    "type": "agent_stream_chunk",
                    "task_id": task_id,
                    "chunk": chunk,
                })
                await asyncio.sleep(0.02)
            await self.broadcast({
                "type": "agent_finished",
                "task_id": task_id,
                "status": "completed" if is_successful else "finished",
                "result": final_result,
            })

        except asyncio.CancelledError:
            logger.info(f"Agent task {task_id} was cancelled.")
            if current_step_span:
                try:
                    current_step_span.end(level="WARNING", status_message="Task cancelled by user")
                except Exception:
                    pass
                current_step_span = None
                set_current_span(None)

            if trace_obj:
                try:
                    trace_obj.update(
                        output="Execution was interrupted by the user.",
                        metadata={"status": "stopped"},
                    )
                    await asyncio.to_thread(langfuse_client.flush)
                except Exception:
                    pass

            await self.broadcast({
                "type": "agent_finished",
                "task_id": task_id,
                "status": "stopped",
                "result": "Execution was interrupted by the user.",
            })
        except Exception as exc:
            err_msg = str(exc)
            is_budget = "budget_exceeded" in err_msg
            is_structured_failure = (
                "invalid JSON for structured output" in err_msg
                or "Expected tool use in response but none found" in err_msg
                or "ValidationError" in err_msg
                or "ModelOutputTruncatedError" in err_msg
            )

            # Clean and user-friendly error classification
            if is_budget:
                error_type = "budget_exceeded"
                user_friendly_error = err_msg
            elif is_structured_failure and settings.llm_provider == "ollama":
                error_type = "model_structured_output_unsupported"
                user_friendly_error = (
                    f"Ollama model '{settings.llm_model_name}' failed to generate valid structured actions. "
                    "Small local models (< 14B) often lack reliable tool-calling capabilities. "
                    "Try using a larger function-calling model (e.g. qwen2.5:32b, llama3.3:70b) or a cloud provider."
                )
            elif is_structured_failure:
                error_type = "structured_output_error"
                user_friendly_error = (
                    f"Model '{settings.llm_model_name}' produced an invalid action schema: {err_msg}"
                )
            else:
                error_type = "execution_error"
                user_friendly_error = err_msg

            logger.error(f"Error executing agent task {task_id} [{error_type}]: {user_friendly_error}", exc_info=True)

            if current_step_span:
                try:
                    current_step_span.end(level="ERROR", status_message=user_friendly_error)
                except Exception:
                    pass
                current_step_span = None
                set_current_span(None)

            if trace_obj:
                try:
                    trace_obj.update(
                        output={"error": user_friendly_error},
                        metadata={"status": "error", "error_type": error_type},
                    )
                    await asyncio.to_thread(langfuse_client.flush)
                except Exception:
                    pass

            await self.broadcast({
                "type": "agent_error",
                "task_id": task_id,
                "error": user_friendly_error,
                "error_type": error_type,
            })
        finally:
            set_current_trace_obj(None)
            set_current_span(None)
            set_current_trace_id(None)
            set_current_session_id(None)
            async with self._lock:
                self.is_running = False
                self.active_task_id = None
                self.active_agent = None
                if self.active_browser:
                    try:
                        await self.active_browser.close()
                    except Exception as e:
                        logger.warning(f"Error closing browser: {e}")
                    finally:
                        self.active_browser = None
    async def stop_task(self) -> bool:
        """Interrupts the currently active agent task."""
        async with self._lock:
            if not self.is_running or not self._current_task:
                return False

            logger.info(f"Stopping agent task {self.active_task_id}...")
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Error while waiting for task cancellation: {e}")

            self.is_running = False
            self.active_task_id = None
            self.active_agent = None
            if self.active_browser:
                try:
                    await self.active_browser.close()
                except Exception as e:
                    logger.warning(f"Error closing browser on stop: {e}")
                finally:
                    self.active_browser = None

            return True

    def get_status(self) -> Dict[str, Any]:
        """Returns current running state and diagnostics."""
        return {
            "is_running": self.is_running,
            "active_task_id": self.active_task_id,
            "subscribers_count": len(self._subscribers),
            "extension_connected": cdp_bridge.is_extension_connected,
        }

agent_service = AgentService()
