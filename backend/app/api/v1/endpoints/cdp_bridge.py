import asyncio
import json
from typing import Optional, Dict, Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from app.core.logger import logger
from app.core.config import settings
from app.core.security import authenticate_websocket, mint_cdp_bootstrap_token
router = APIRouter(prefix="/cdp", tags=["CDP Bridge"])


class CDPBridgeManager:
    """Manages the bidirectional relay between browser-use and the Chrome Extension via chrome.debugger."""

    def __init__(self):
        self.extension_ws: Optional[WebSocket] = None
        self.browser_use_ws: Optional[WebSocket] = None
        self.active_tab_info: Dict[str, Any] = {
            "tabId": 1,
            "url": settings.frontend_url,
            "title": "Web-Frame Agent Workspace",
        }
        self.virtual_session_id = settings.cdp_virtual_session_id
        self._lock = asyncio.Lock()
        self.in_flight_commands: Dict[int, float] = {}  # msg_id -> timestamp
    @property
    def is_extension_connected(self) -> bool:
        return self.extension_ws is not None

    @property
    def is_iframe_ready(self) -> bool:
        return self.extension_ws is not None and bool(self.active_tab_info.get("iframe_session_id"))
    async def register_extension(self, ws: WebSocket) -> bool:
        """Registers the Chrome Extension. Rejects concurrent connections if one is already active."""
        async with self._lock:
            if self.extension_ws is not None and self.extension_ws != ws:
                logger.warning("[CDP Bridge] Additional Chrome Extension connection rejected: bridge already has an active extension.")
                return False
            self.extension_ws = ws
            logger.info("Chrome Extension CDP Bridge registered.")
            return True

    async def unregister_extension(self, ws: Optional[WebSocket] = None):
        """Unregisters the extension if the disconnecting socket matches the active one."""
        async with self._lock:
            if ws is not None and self.extension_ws != ws:
                # Disconnection from an already rejected/secondary socket: do not touch active extension
                return
            self.extension_ws = None
            logger.info("Chrome Extension CDP Bridge unregistered.")
            # Immediately fail all pending in-flight commands so browser-use does not hang
            if self.browser_use_ws and self.in_flight_commands:
                logger.warning(f"Extension disconnected with {len(self.in_flight_commands)} in-flight commands. Failing immediately.")
                pending_ids = list(self.in_flight_commands.keys())
                self.in_flight_commands.clear()
                for cmd_id in pending_ids:
                    try:
                        await self.browser_use_ws.send_text(json.dumps({
                            "id": cmd_id,
                            "error": {
                                "code": -32000,
                                "message": "extension_disconnected",
                            }
                        }))
                    except Exception:
                        pass

    async def register_browser_use(self, ws: WebSocket) -> bool:
        """Registers Browser-Use. Rejects concurrent connections if one is already active."""
        async with self._lock:
            if self.browser_use_ws is not None and self.browser_use_ws != ws:
                logger.warning("[CDP Bridge] Additional Browser-Use connection rejected: bridge already has an active browser-use connection.")
                return False
            self.browser_use_ws = ws
            logger.info("Browser-Use connected to CDP Bridge.")
            return True

    async def unregister_browser_use(self, ws: Optional[WebSocket] = None):
        """Unregisters browser-use and clears in-flight tracking."""
        async with self._lock:
            if ws is not None and self.browser_use_ws != ws:
                return
            self.browser_use_ws = None
            self.in_flight_commands.clear()
            logger.info("Browser-Use disconnected from CDP Bridge.")

cdp_bridge = CDPBridgeManager()


# --- HTTP Discovery Endpoints for Playwright / browser-use ---

@router.get("/json/version")
@router.get("/version")
async def get_version(request: Request):
    """Returns Chrome DevTools Protocol version info expected by Playwright/Browser-Use."""
    host = request.headers.get("host", f"{settings.backend_host}:{settings.backend_port}")
    return JSONResponse({
        "Browser": "Chrome/128.0.0.0 (Web-Frame Agent CDP Bridge)",
        "Protocol-Version": "1.3",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "V8-Version": "12.8.0",
        "WebKit-Version": "537.36",
        "webSocketDebuggerUrl": f"ws://{host}/api/v1/cdp/devtools/browser?token={mint_cdp_bootstrap_token()}",
    })


@router.get("/json")
@router.get("/json/list")
async def get_targets(request: Request):
    """Returns the list of active targets/tabs available for debugging."""
    host = request.headers.get("host", f"{settings.backend_host}:{settings.backend_port}")
    tab_id = cdp_bridge.active_tab_info.get("tabId", 1)
    url = cdp_bridge.active_tab_info.get("url", settings.frontend_url)
    title = cdp_bridge.active_tab_info.get("title", "Web-Frame Agent")

    return JSONResponse([
        {
            "description": "",
            "devtoolsFrontendUrl": "",
            "id": str(tab_id),
            "title": title,
            "type": "page",
            "url": url,
            "webSocketDebuggerUrl": f"ws://{host}/api/v1/cdp/devtools/page/{tab_id}?token={mint_cdp_bootstrap_token()}",
        }
    ])


# --- WebSocket for Chrome Extension ---

@router.websocket("/extension")
async def websocket_extension_endpoint(websocket: WebSocket):
    """Extension connects here to receive CDP commands and send results/events."""
    authenticated = await authenticate_websocket(websocket)
    if not authenticated:
        return
    registered = await cdp_bridge.register_extension(websocket)
    if not registered:
        await websocket.close(code=1008, reason="Another extension is already connected")
        return
    try:
        while True:
            text = await websocket.receive_text()
            try:
                data = json.loads(text)
                if data.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                    continue

                if data.get("type") == "tab_info":
                    cdp_bridge.active_tab_info.update({
                        "tabId": data.get("tabId"),
                        "targetId": data.get("targetId") or str(data.get("tabId", 1)),
                        "url": data.get("url") or settings.default_target_url,
                        "title": data.get("title") or "Workspace Target",
                    })
                    logger.info(f"Target selected from extension: {cdp_bridge.active_tab_info}")
                    continue

                if data.get("type") == "iframe_info":
                    cdp_bridge.active_tab_info.update({
                        "iframe_session_id": data.get("sessionId"),
                        "targetId": data.get("targetId") or cdp_bridge.active_tab_info.get("targetId"),
                        "url": data.get("url") or cdp_bridge.active_tab_info.get("url"),
                        "title": data.get("title") or cdp_bridge.active_tab_info.get("title"),
                    })
                    logger.info(f"Target iframe session registered from extension: {cdp_bridge.active_tab_info}")
                    from app.services.agent_service import agent_service
                    asyncio.create_task(agent_service.broadcast({
                        "type": "iframe_status",
                        "iframe_ready": True,
                        "url": cdp_bridge.active_tab_info.get("url"),
                    }))
                    continue
                if data.get("type") in ("iframe_navigated", "target_navigated"):
                    new_url = data.get("url", "")
                    new_title = data.get("title", "")
                    if new_url and not new_url.startswith("about:") and new_url != cdp_bridge.active_tab_info.get("url"):
                        cdp_bridge.active_tab_info["url"] = new_url
                        if new_title:
                            cdp_bridge.active_tab_info["title"] = new_title
                        logger.info(f"[Iframe State] Live URL changed: {new_url} (Title: '{new_title}')")
                    continue

                # Log screenshot metadata if returned by extension
                if "_meta" in data:
                    meta = data.pop("_meta")
                    cap_method = meta.get("capture_method", "unknown")
                    zero_blink = meta.get("zero_blink", False)
                    reason = meta.get("reason", "")
                    if zero_blink:
                        logger.info(f"[Screenshot] Method: {cap_method} (Zero-Blink) | Cropped: {meta.get('cropped')} | Rect: {meta.get('rect')}")
                    else:
                        logger.warning(f"[Screenshot] Method: {cap_method} (CDP Fallback) | Reason: {reason} | Clip: {meta.get('clip')}")

                # Log page lifecycle & navigation events occurring inside the iframe
                evt_method = data.get("method")
                if evt_method == "Page.frameNavigated":
                    frame = data.get("params", {}).get("frame", {})
                    f_url = frame.get("url", "")
                    parent_id = frame.get("parentId")
                    # Only root frame of the target session updates active_tab_info URL; subframes (ads, tracking pixels, about:blank) are ignored
                    if f_url and not parent_id and not f_url.startswith("about:"):
                        cdp_bridge.active_tab_info["url"] = f_url
                        logger.info(f"[Iframe Navigation] Root frame navigated to: {f_url}")
                    elif f_url:
                        logger.debug(f"[Iframe Navigation] Ignoring subframe navigation (parentId={parent_id}) to: {f_url}")
                elif evt_method == "Page.loadEventFired":
                    logger.info(f"[Iframe Lifecycle] Page load event fired for current iframe")
                elif evt_method == "Page.domContentEventFired":
                    logger.info(f"[Iframe Lifecycle] DOMContentLoaded event fired for current iframe")

                # Prevent browser-use from crashing when an iframe navigates/swaps targets
                if data.get("method") == "Target.detachedFromTarget":
                    detached_params = data.get("params", {})
                    detached_target = detached_params.get("targetId")
                    if detached_target and detached_target == cdp_bridge.active_tab_info.get("targetId"):
                        logger.info(f"Active iframe target {detached_target} detached, clearing stale session.")
                        cdp_bridge.active_tab_info.pop("iframe_session_id", None)
                        cdp_bridge.active_tab_info["targetId"] = str(cdp_bridge.active_tab_info.get("tabId", 1))
                    else:
                        logger.info(f"Target detached (non-active): {detached_target}")
                    continue

                # Forward responses and events from extension to browser-use, remapping to virtual session
                if cdp_bridge.browser_use_ws:
                    resp_id = data.get("id")
                    if resp_id is not None and resp_id in cdp_bridge.in_flight_commands:
                        cdp_bridge.in_flight_commands.pop(resp_id, None)
                    if data.get("sessionId"):
                        data["sessionId"] = cdp_bridge.virtual_session_id
                    text = json.dumps(data)
                    await cdp_bridge.browser_use_ws.send_text(text)
            except Exception as e:
                logger.warning(f"Error handling extension message: {e}")

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await cdp_bridge.unregister_extension(websocket)


# --- WebSocket for Browser-Use / Playwright ---

@router.websocket("/devtools/browser")
@router.websocket("/devtools/page/{target_id}")
async def websocket_browser_use_endpoint(websocket: WebSocket, target_id: Optional[str] = None):
    """Browser-Use connects here to send CDP commands and receive events."""
    authenticated = await authenticate_websocket(websocket, allow_cdp_bootstrap=True)
    if not authenticated:
        return
    registered = await cdp_bridge.register_browser_use(websocket)
    if not registered:
        await websocket.close(code=1008, reason="Another browser-use session is already connected")
        return
    try:
        while True:
            text = await websocket.receive_text()
            try:
                data = json.loads(text)
                method = data.get("method", "")
                msg_id = data.get("id")
                session_id = data.get("sessionId")
                params = data.get("params", {})

                # Detailed action logging for debugging DOM & agent interaction
                if method == "Input.dispatchMouseEvent":
                    m_type = params.get("type")
                    m_x = params.get("x")
                    m_y = params.get("y")
                    m_btn = params.get("button", "left")
                    logger.info(f"[Action Mouse] {m_type} button={m_btn} at ({m_x}, {m_y})")
                elif method == "Input.dispatchKeyEvent":
                    k_type = params.get("type")
                    k_text = params.get("text") or params.get("key") or ""
                    logger.info(f"[Action Key] {k_type} key='{k_text}'")
                elif method == "Page.captureScreenshot":
                    logger.info(f"[Action Screenshot] Requesting screenshot from extension...")
                elif method == "Runtime.evaluate":
                    expr = str(params.get("expression", ""))[:120]
                    logger.info(f"[Action Script] Runtime.evaluate: {expr}...")
                else:
                    logger.info(f"CDP call from browser-use: {method}")

                target_id_str = str(cdp_bridge.active_tab_info.get("targetId") or cdp_bridge.active_tab_info.get("tabId", 1))
                page_url = cdp_bridge.active_tab_info.get("url", settings.default_target_url)
                page_title = cdp_bridge.active_tab_info.get("title", "Workspace Target")

                target_info = {
                    "targetId": target_id_str,
                    "type": "page",
                    "title": page_title,
                    "url": page_url,
                    "attached": True,
                    "canAccessOpener": False,
                    "browserContextId": "default",
                }

                # --- 1. Synthesize Target Domain Commands (Restricted by Chrome for Extensions) ---
                if method == "Target.setDiscoverTargets":
                    await websocket.send_text(json.dumps({"id": msg_id, "result": {}}))
                    continue

                elif method == "Target.setAutoAttach":
                    await websocket.send_text(json.dumps({"id": msg_id, "result": {}}))
                    continue

                elif method == "Target.getTargets":
                    await websocket.send_text(json.dumps({
                        "id": msg_id,
                        "result": {
                            "targetInfos": [target_info]
                        }
                    }))
                    continue

                elif method == "Target.attachToTarget":
                    chosen_target_id = str(params.get("targetId", target_id_str))
                    assigned_session_id = cdp_bridge.virtual_session_id
                    # 1. Send the command response
                    await websocket.send_text(json.dumps({
                        "id": msg_id,
                        "result": {
                            "sessionId": assigned_session_id
                        }
                    }))

                    # 2. Immediately dispatch Target.attachedToTarget event required by SessionManager
                    attach_event = {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": assigned_session_id,
                            "targetInfo": target_info,
                            "waitingForDebugger": False,
                        }
                    }
                    await websocket.send_text(json.dumps(attach_event))
                    continue

                elif method in ("Target.detachFromTarget", "Target.activateTarget", "Target.closeTarget"):
                    await websocket.send_text(json.dumps({"id": msg_id, "result": {}}))
                    continue

                # --- 2. Synthesize Browser Domain Commands ---
                elif method in ("Browser.getWindowForTarget", "Browser.getWindowBounds"):
                    await websocket.send_text(json.dumps({
                        "id": msg_id,
                        "result": {
                            "windowId": 1,
                            "bounds": {
                                "left": 0,
                                "top": 0,
                                "width": settings.default_viewport_width,
                                "height": settings.default_viewport_height,
                                "windowState": "normal"
                            }
                        }
                    }))
                    continue

                elif method == "Browser.getVersion":
                    await websocket.send_text(json.dumps({
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "1.3",
                            "product": "Chrome/128.0.0.0",
                            "revision": "@0",
                            "userAgent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/128.0.0.0 Safari/537.36"
                            ),
                            "jsVersion": "12.8.0",
                        }
                    }))
                    continue

                elif method == "Page.navigate":
                    target_nav_url = params.get("url", "")
                    # Prevent recursive navigation to host application URL
                    from urllib.parse import urlparse
                    try:
                        parsed_nav = urlparse(target_nav_url)
                        nav_host = parsed_nav.netloc.lower()
                        frontend_host = urlparse(settings.frontend_url).netloc.lower()
                        if nav_host in (frontend_host, f"{settings.backend_host}:{settings.backend_port}", "localhost:5173", "127.0.0.1:5173"):
                            logger.warning(f"Blocked recursive navigation to host application URL: {target_nav_url}")
                            await websocket.send_text(json.dumps({
                                "id": msg_id,
                                "error": {
                                    "code": -32000,
                                    "message": "Navigation to host workspace application URL is prohibited."
                                }
                            }))
                            continue
                    except Exception as parse_err:
                        logger.warning(f"Error parsing navigation URL '{target_nav_url}': {parse_err}")

                    logger.info(f"Navigating workspace iframe to: {target_nav_url}")
                    cdp_bridge.active_tab_info["url"] = target_nav_url
                    if cdp_bridge.extension_ws:
                        live_session = cdp_bridge.active_tab_info.get("iframe_session_id")
                        if live_session:
                            data["sessionId"] = live_session
                        else:
                            data.pop("sessionId", None)
                        await cdp_bridge.extension_ws.send_text(json.dumps(data))
                    else:
                        await websocket.send_text(json.dumps({
                            "id": msg_id,
                            "result": {
                                "frameId": "main-frame",
                                "loaderId": "loader-1",
                            }
                        }))
                    continue

                elif method in (
                    "Browser.grantPermissions",
                    "Browser.resetPermissions",
                    "Browser.setDownloadBehavior",
                    "Emulation.setFocusEmulationEnabled",
                    "Runtime.runIfWaitingForDebugger",
                ):
                    await websocket.send_text(json.dumps({"id": msg_id, "result": {}}))
                    continue

                # Page.enable & Network.enable: forward asynchronously to the extension so Chromium activates
                # domain event streams, while returning immediate synthetic success to browser-use to avoid watchdog deadlocks
                elif method in ("Page.enable", "Network.enable", "Page.setLifecycleEventsEnabled"):
                    if cdp_bridge.extension_ws:
                        live_session = cdp_bridge.active_tab_info.get("iframe_session_id")
                        fwd_data = dict(data)
                        if live_session:
                            fwd_data["sessionId"] = live_session
                        else:
                            fwd_data.pop("sessionId", None)
                        await cdp_bridge.extension_ws.send_text(json.dumps(fwd_data))
                    await websocket.send_text(json.dumps({"id": msg_id, "result": {}}))
                    continue
                # --- 3. Forward All Page-Level Operations (Page, DOM, Accessibility, Input) to Extension ---
                if cdp_bridge.extension_ws:
                    live_session = cdp_bridge.active_tab_info.get("iframe_session_id")
                    if live_session:
                        data["sessionId"] = live_session
                    else:
                        data.pop("sessionId", None)
                    if msg_id is not None:
                        import time
                        cdp_bridge.in_flight_commands[msg_id] = time.time()
                    text = json.dumps(data)
                    await cdp_bridge.extension_ws.send_text(text)
                else:
                    if msg_id is not None:
                        await websocket.send_text(json.dumps({
                            "id": msg_id,
                            "error": {
                                "code": -32000,
                                "message": "Chrome Extension CDP Bridge is not connected. Please reload the extension in chrome://extensions.",
                            }
                        }))
            except Exception as e:
                logger.warning(f"Error handling browser-use message: {e}")
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await cdp_bridge.unregister_browser_use(websocket)
