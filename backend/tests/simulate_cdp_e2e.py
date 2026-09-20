import asyncio
import json
import unittest
from websockets.asyncio.client import connect

from app.core.config import settings

async def run_cdp_e2e_simulation():
    backend_ws_ext = f"ws://{settings.backend_host}:{settings.backend_port}/api/v1/cdp/extension"
    backend_ws_bu = f"ws://{settings.backend_host}:{settings.backend_port}/api/v1/cdp/devtools/browser"
    print("\n--- [E2E Live CDP Simulation Test] ---")

    # 1. Connect fake Extension to bridge
    async with connect(backend_ws_ext) as ext_ws:
        print("[1/5] Extension connected to /api/v1/cdp/extension")

        # Extension sends tab_info
        await ext_ws.send(json.dumps({
            "type": "tab_info",
            "tabId": 999,
            "targetId": "tab-999",
            "url": settings.frontend_url,
            "title": "frontend"
        }))

        # Extension sends iframe_info (Google Target)
        await ext_ws.send(json.dumps({
            "type": "iframe_info",
            "sessionId": "real-child-google-session-888",
            "targetId": "target-google-888",
            "url": "https://www.google.com",
            "title": "Google Target"
        }))
        print("[2/5] Extension registered iframe session 'real-child-google-session-888'")

        # 2. Connect fake Browser-Use to bridge
        async with connect(backend_ws_bu) as bu_ws:
            print("[3/5] Browser-Use connected to /api/v1/cdp/devtools/browser")

            # Check immediate synthetic response for Page.enable
            await bu_ws.send(json.dumps({"id": 1, "method": "Page.enable", "sessionId": "session-workspace-iframe-main"}))
            resp = json.loads(await bu_ws.recv())
            assert resp["id"] == 1 and resp["result"] == {}, f"Unexpected resp: {resp}"
            print("[4/5] Synthetic watchdog response Page.enable OK (0ms delay)")

            # Check routing of DOMSnapshot.captureSnapshot into real child session
            await bu_ws.send(json.dumps({
                "id": 2,
                "method": "DOMSnapshot.captureSnapshot",
                "sessionId": "session-workspace-iframe-main",
                "params": {"computedStyles": []}
            }))

            ext_req = json.loads(await ext_ws.recv())
            assert ext_req["id"] == 2
            assert ext_req["method"] == "DOMSnapshot.captureSnapshot"
            assert ext_req["sessionId"] == "real-child-google-session-888", f"Session wasn't routed to child! Got: {ext_req.get('sessionId')}"
            print(f"[5/5] Command DOMSnapshot routed strictly to child session: {ext_req['sessionId']}")

            # Send back answer from extension
            await ext_ws.send(json.dumps({
                "id": 2,
                "sessionId": "real-child-google-session-888",
                "result": {"documents": [{"title": "Google", "nodes": [10, 20, 30]}]}
            }))

            bu_resp = json.loads(await bu_ws.recv())
            assert bu_resp["id"] == 2
            assert bu_resp["sessionId"] == "session-workspace-iframe-main"
            assert bu_resp["result"]["documents"][0]["title"] == "Google"
            print("E2E Full Simulation SUCCESS: Browser-Use receives Google DOM data mapped seamlessly!")

if __name__ == "__main__":
    asyncio.run(run_cdp_e2e_simulation())
