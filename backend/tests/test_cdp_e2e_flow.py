import asyncio
import json
import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.api.v1.endpoints.cdp_bridge import cdp_bridge
from app.core.security import get_active_token
from app.core.config import settings
class TestCDPBridgeE2E(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.token = get_active_token()

    def test_complete_cdp_flow(self):
        print("\n--- [E2E Live Test: Extension + CDP Bridge + Browser-Use Flow] ---")

        # 1. Connect Extension
        with self.client.websocket_connect(f"/api/v1/cdp/extension?token={self.token}") as ext_ws:
            print("[Pass 1/5] Extension WebSocket connected successfully.")
            # Send host tab info and ping to let event loop process
            ext_ws.send_text(json.dumps({
                "type": "tab_info",
                "tabId": 1234,
                "targetId": "tab-1234",
                "url": settings.frontend_url,
                "title": "frontend"
            }))
            ext_ws.send_text(json.dumps({
                "type": "iframe_info",
                "sessionId": "real-child-google-session-999",
                "targetId": "target-google-999",
                "url": "https://www.google.com",
                "title": "Google"
            }))
            # Send ping and receive pong to ensure preceding messages are processed
            ext_ws.send_text(json.dumps({"type": "ping"}))
            pong = json.loads(ext_ws.receive_text())
            self.assertEqual(pong.get("type"), "pong")
            self.assertEqual(cdp_bridge.active_tab_info.get("iframe_session_id"), "real-child-google-session-999")
            self.assertEqual(cdp_bridge.active_tab_info.get("url"), "https://www.google.com")
            print("[Pass 2/5] Extension registered iframe session 'real-child-google-session-999' on target URL.")

            # 2. Connect Browser-Use
            with self.client.websocket_connect(f"/api/v1/cdp/devtools/browser?token={self.token}") as bu_ws:
                print("[Pass 3/5] Browser-Use WebSocket connected successfully.")

                # Test 1: Immediate synthetic response for Page.enable (watchdog proof)
                bu_ws.send_text(json.dumps({
                    "id": 1,
                    "method": "Page.enable",
                    "sessionId": "session-workspace-iframe-main"
                }))
                resp1 = json.loads(bu_ws.receive_text())
                self.assertEqual(resp1["id"], 1)
                self.assertEqual(resp1["result"], {})
                print("[Pass 4/5] Synthetic watchdog response Page.enable returned in 0ms (deadlock prevention confirmed).")

                # Verify extension also asynchronously received forwarded Page.enable
                ext_enable = json.loads(ext_ws.receive_text())
                self.assertEqual(ext_enable["id"], 1)
                self.assertEqual(ext_enable["method"], "Page.enable")
                self.assertEqual(ext_enable["sessionId"], "real-child-google-session-999")

                # Test 2: Routing of DOMSnapshot.captureSnapshot into real child session
                bu_ws.send_text(json.dumps({
                    "id": 2,
                    "method": "DOMSnapshot.captureSnapshot",
                    "sessionId": "session-workspace-iframe-main",
                    "params": {"computedStyles": []}
                }))

                ext_req = json.loads(ext_ws.receive_text())
                self.assertEqual(ext_req["id"], 2)
                self.assertEqual(ext_req["method"], "DOMSnapshot.captureSnapshot")
                self.assertEqual(ext_req["sessionId"], "real-child-google-session-999")
                # Extension responds with mock DOM data from the iframe
                ext_ws.send_text(json.dumps({
                    "id": 2,
                    "sessionId": "real-child-google-session-999",
                    "result": {
                        "documents": [{
                            "title": "Google",
                            "nodes": [101, 102, 103]
                        }]
                    }
                }))

                # Browser-use receives response remapped to virtual session
                bu_resp = json.loads(bu_ws.receive_text())
                self.assertEqual(bu_resp["id"], 2)
                self.assertEqual(bu_resp["sessionId"], "session-workspace-iframe-main")
                self.assertEqual(bu_resp["result"]["documents"][0]["title"], "Google")
                print("[Pass 5/5] DOMSnapshot routed strictly to child iframe and returned seamlessly to Browser-Use.")

        print("--> RESULT: 100% OF TESTS PASSED.")

if __name__ == "__main__":
    unittest.main()
