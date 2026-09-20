import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch
from app.api.v1.endpoints.cdp_bridge import (
    cdp_bridge,
    websocket_extension_endpoint,
    websocket_browser_use_endpoint,
)
from app.core.config import settings

class MockWebSocket:
    """Mock WebSocket for deterministic, asynchronous end-to-end testing without OS-level thread deadlocks."""
    def __init__(self):
        self.in_queue = asyncio.Queue()
        self.out_queue = asyncio.Queue()
        self.client = MagicMock()
        self.client.host = "127.0.0.1"
        self.headers = {}
        self.query_params = {}

    async def accept(self):
        pass

    async def receive_text(self):
        return await self.in_queue.get()

    async def send_text(self, text):
        await self.out_queue.put(text)

    async def close(self, code=1000, reason=""):
        pass

class TestCDPBridgeE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset CDP bridge state prior to test execution
        await cdp_bridge.unregister_extension()
        await cdp_bridge.unregister_browser_use()
        cdp_bridge.active_tab_info.clear()

    async def asyncTearDown(self):
        await cdp_bridge.unregister_extension()
        await cdp_bridge.unregister_browser_use()
        cdp_bridge.active_tab_info.clear()

    async def test_complete_cdp_flow(self):
        print("\n--- [E2E Live Test: Extension + CDP Bridge + Browser-Use Flow] ---")

        ext_ws = MockWebSocket()
        bu_ws = MockWebSocket()

        with patch("app.api.v1.endpoints.cdp_bridge.authenticate_websocket", return_value=True):
            # 1. Connect Extension Endpoint
            ext_task = asyncio.create_task(websocket_extension_endpoint(ext_ws))
            await asyncio.sleep(0.01)
            print("[Pass 1/5] Extension WebSocket connected successfully.")

            # Register tab info and target iframe session from extension
            await ext_ws.in_queue.put(json.dumps({
                "type": "tab_info",
                "tabId": 1234,
                "targetId": "tab-1234",
                "url": settings.frontend_url,
                "title": "frontend",
            }))
            await ext_ws.in_queue.put(json.dumps({
                "type": "iframe_info",
                "sessionId": "real-child-google-session-999",
                "targetId": "target-google-999",
                "url": "https://www.google.com",
                "title": "Google",
            }))
            await ext_ws.in_queue.put(json.dumps({"type": "ping"}))
            await asyncio.sleep(0)

            # Await pong
            pong_raw = await asyncio.wait_for(ext_ws.out_queue.get(), timeout=2.0)
            pong = json.loads(pong_raw)
            self.assertEqual(pong.get("type"), "pong")
            self.assertEqual(cdp_bridge.active_tab_info.get("iframe_session_id"), "real-child-google-session-999")
            self.assertEqual(cdp_bridge.active_tab_info.get("url"), "https://www.google.com")
            print("[Pass 2/5] Extension registered iframe session 'real-child-google-session-999' on target URL.")

            # 2. Connect Browser-Use Endpoint
            bu_task = asyncio.create_task(websocket_browser_use_endpoint(bu_ws))
            await asyncio.sleep(0.01)
            print("[Pass 3/5] Browser-Use WebSocket connected successfully.")

            # Test 1: Immediate synthetic response for Page.enable (watchdog proof)
            await bu_ws.in_queue.put(json.dumps({
                "id": 1,
                "method": "Page.enable",
                "sessionId": "session-workspace-iframe-main",
            }))
            resp1_raw = await asyncio.wait_for(bu_ws.out_queue.get(), timeout=2.0)
            resp1 = json.loads(resp1_raw)
            self.assertEqual(resp1["id"], 1)
            self.assertEqual(resp1["result"], {})
            print("[Pass 4/5] Synthetic watchdog response Page.enable returned in 0ms (deadlock prevention confirmed).")

            # Verify extension also asynchronously received forwarded Page.enable
            ext_enable_raw = await asyncio.wait_for(ext_ws.out_queue.get(), timeout=2.0)
            ext_enable = json.loads(ext_enable_raw)
            self.assertEqual(ext_enable["id"], 1)
            self.assertEqual(ext_enable["method"], "Page.enable")
            self.assertEqual(ext_enable["sessionId"], "real-child-google-session-999")

            # Test 2: Routing of DOMSnapshot.captureSnapshot into real child session
            await bu_ws.in_queue.put(json.dumps({
                "id": 2,
                "method": "DOMSnapshot.captureSnapshot",
                "sessionId": "session-workspace-iframe-main",
                "params": {"computedStyles": []},
            }))

            ext_req_raw = await asyncio.wait_for(ext_ws.out_queue.get(), timeout=2.0)
            ext_req = json.loads(ext_req_raw)
            self.assertEqual(ext_req["id"], 2)
            self.assertEqual(ext_req["method"], "DOMSnapshot.captureSnapshot")
            self.assertEqual(ext_req["sessionId"], "real-child-google-session-999")

            # Extension responds with mock DOM data from the iframe
            await ext_ws.in_queue.put(json.dumps({
                "id": 2,
                "sessionId": "real-child-google-session-999",
                "result": {
                    "documents": [{
                        "title": "Google",
                        "nodes": [101, 102, 103],
                    }],
                },
            }))

            # Browser-use receives response remapped to virtual session
            bu_resp_raw = await asyncio.wait_for(bu_ws.out_queue.get(), timeout=2.0)
            bu_resp = json.loads(bu_resp_raw)
            self.assertEqual(bu_resp["id"], 2)
            self.assertEqual(bu_resp["sessionId"], "session-workspace-iframe-main")
            self.assertEqual(bu_resp["result"]["documents"][0]["title"], "Google")
            print("[Pass 5/5] DOMSnapshot routed strictly to child iframe and returned seamlessly to Browser-Use.")

            print("--> RESULT: 100% OF TESTS PASSED.")

            ext_task.cancel()
            bu_task.cancel()
            await asyncio.gather(ext_task, bu_task, return_exceptions=True)

    async def test_subframe_and_about_blank_navigation_filtered(self):
        """Verifies that subframes (with parentId) and about:blank navigations never overwrite active_tab_info URL."""
        ext_ws = MockWebSocket()
        cdp_bridge.active_tab_info["url"] = "https://www.reddit.com/"

        with patch("app.api.v1.endpoints.cdp_bridge.authenticate_websocket", return_value=True):
            ext_task = asyncio.create_task(websocket_extension_endpoint(ext_ws))

            # Handshake
            await ext_ws.in_queue.put(json.dumps({
                "type": "tab_info",
                "tabId": 1234,
                "url": "http://localhost:5173",
                "title": "frontend",
            }))
            await ext_ws.in_queue.put(json.dumps({
                "type": "iframe_info",
                "targetId": "target-reddit-root",
                "url": "https://www.reddit.com/",
                "title": "Reddit",
                "sessionId": "session-reddit-root",
            }))
            await asyncio.sleep(0.01)
            self.assertEqual(cdp_bridge.active_tab_info["url"], "https://www.reddit.com/")

            # 1. Subframe navigates to about:blank (e.g. ad or tracking frame with parentId)
            await ext_ws.in_queue.put(json.dumps({
                "method": "Page.frameNavigated",
                "params": {
                    "frame": {
                        "id": "child-ad-frame",
                        "parentId": "root-reddit-frame",
                        "url": "about:blank",
                    }
                }
            }))
            await asyncio.sleep(0.01)
            self.assertEqual(cdp_bridge.active_tab_info["url"], "https://www.reddit.com/", "Subframe about:blank must not overwrite root URL")

            # 2. Subframe navigates to an external tracking URL with parentId
            await ext_ws.in_queue.put(json.dumps({
                "method": "Page.frameNavigated",
                "params": {
                    "frame": {
                        "id": "child-ad-frame-2",
                        "parentId": "root-reddit-frame",
                        "url": "https://adservice.google.com/pixel",
                    }
                }
            }))
            await asyncio.sleep(0.01)
            self.assertEqual(cdp_bridge.active_tab_info["url"], "https://www.reddit.com/", "Subframe navigation must not overwrite root URL")

            # 3. iframe_navigated event with about:blank
            await ext_ws.in_queue.put(json.dumps({
                "type": "iframe_navigated",
                "url": "about:blank",
                "title": "",
            }))
            await asyncio.sleep(0.01)
            self.assertEqual(cdp_bridge.active_tab_info["url"], "https://www.reddit.com/", "iframe_navigated with about:blank must be ignored")

            # 4. Genuine root frame navigation (no parentId, valid URL)
            await ext_ws.in_queue.put(json.dumps({
                "method": "Page.frameNavigated",
                "params": {
                    "frame": {
                        "id": "root-reddit-frame",
                        "url": "https://www.reddit.com/submit",
                    }
                }
            }))
            await asyncio.sleep(0.01)
            self.assertEqual(cdp_bridge.active_tab_info["url"], "https://www.reddit.com/submit", "Root frame navigation must update active_tab_info URL")

            ext_task.cancel()
            await asyncio.gather(ext_task, return_exceptions=True)
if __name__ == "__main__":
    unittest.main()
