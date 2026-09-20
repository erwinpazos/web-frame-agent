import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.agent_service import agent_service
from app.core.logger import logger
from app.core.config import settings
from app.core.security import authenticate_websocket, check_task_rate_limit

router = APIRouter(tags=["WebSocket"])


@router.websocket("/ws/chat")
async def websocket_chat_endpoint(websocket: WebSocket):
    """Bidirectional WebSocket for live streaming of agent steps, thoughts, and control commands."""
    if not await authenticate_websocket(websocket):
        return

    logger.info("WebSocket client connected to /ws/chat (authenticated)")
    queue = agent_service.subscribe()

    async def sender():
        """Forwards all broadcasted events from the agent service to this client."""
        try:
            while True:
                event = await queue.get()
                await websocket.send_text(json.dumps(event))
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception as e:
            logger.warning(f"Error in WebSocket sender: {e}")

    async def receiver():
        """Handles incoming messages from the frontend client."""
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                    msg_type = payload.get("type")

                    if msg_type == "start_task":
                        prompt = payload.get("task", "").strip()
                        url = payload.get("url", settings.default_target_url)
                        session_id = payload.get("session_id")
                        max_steps = int(payload.get("max_steps", 15))
                        headless = payload.get("headless", False)
                        if prompt:
                            client_ip = websocket.client.host if websocket.client else "127.0.0.1"
                            if not check_task_rate_limit(client_ip, max_launches=5, window_seconds=60.0):
                                await websocket.send_text(
                                    json.dumps({
                                        "type": "agent_error",
                                        "error": "Too many task launches. Please wait before starting a new one.",
                                    })
                                )
                                continue
                            try:
                                task_id = await agent_service.start_task(
                                    prompt=prompt,
                                    start_url=url,
                                    session_id=session_id,
                                    max_steps=max_steps,
                                    headless=headless,
                                )
                                await websocket.send_text(
                                    json.dumps({
                                        "type": "task_accepted",
                                        "task_id": task_id,
                                    })
                                )
                            except Exception as err:
                                err_payload = {
                                    "type": "agent_error",
                                    "error": str(err),
                                }
                                # 1. Send direct to sender socket
                                await websocket.send_text(json.dumps(err_payload))
                                # 2. Also broadcast to agent_service subscribers (SSE & all clients)
                                await agent_service.broadcast(err_payload)

                    elif msg_type == "stop_task":
                        stopped = await agent_service.stop_task()
                        await websocket.send_text(
                            json.dumps({
                                "type": "task_stopped",
                                "success": stopped,
                            })
                        )

                except json.JSONDecodeError:
                    logger.warning("Received invalid JSON on WebSocket")

        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception as e:
            logger.warning(f"Error in WebSocket receiver: {e}")

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())

    try:
        done, pending = await asyncio.wait(
            [sender_task, receiver_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        agent_service.unsubscribe(queue)
        logger.info("WebSocket client disconnected from /ws/chat")
