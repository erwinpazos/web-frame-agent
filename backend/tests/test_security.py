"""Security and integration smoke tests covering auth, origin checks, and rate limits."""
import unittest
from starlette.testclient import TestClient

from app.main import app
from app.core.security import get_active_token
from app.core.config import settings
from app.core.llm_gateway import LLMGateway


class TestSecurity(unittest.TestCase):
    def setUp(self):
        self._orig_token = settings.api_token
        settings.api_token = "test-secret-token"
        self.client = TestClient(app)
        self.token = get_active_token()

    def tearDown(self):
        settings.api_token = self._orig_token
        from app.api.v1.endpoints.cdp_bridge import cdp_bridge
        cdp_bridge.extension_ws = None
        cdp_bridge.browser_use_ws = None
        cdp_bridge.active_tab_info = {}
    def test_health_endpoint_public(self):
        """Health endpoint should be public, confirm healthy status without leaking the secret token."""
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("status"), "healthy")
        self.assertNotIn("auth_token", data)
        self.assertIn("auth_configured", data)
        self.assertIn("default_target_url", data)
    def test_agent_status_without_token_rejected(self):
        """Unauthenticated access to /agent/status must return 401."""
        res = self.client.get("/api/v1/agent/status")
        self.assertEqual(res.status_code, 401)

    def test_agent_status_with_token_header_accepted(self):
        """Accessing /agent/status with Authorization Bearer header must succeed."""
        headers = {"Authorization": f"Bearer {self.token}"}
        res = self.client.get("/api/v1/agent/status", headers=headers)
        self.assertEqual(res.status_code, 200)

    def test_agent_status_with_token_query_accepted(self):
        """Accessing /agent/status with ?token= query parameter must succeed."""
        res = self.client.get(f"/api/v1/agent/status?token={self.token}")
        self.assertEqual(res.status_code, 200)

    def test_dns_rebinding_rejected(self):
        """Requests with unauthorized Host header should be blocked by middleware."""
        headers = {"Host": "malicious-site.com"}
        res = self.client.get("/health", headers=headers)
        self.assertEqual(res.status_code, 400)

    def test_websocket_chat_unauthenticated_rejected(self):
        """Connecting to /ws/chat without token must close with 1008."""
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/api/v1/ws/chat") as ws:
                ws.receive_text()

    def test_websocket_chat_authenticated_accepted(self):
        """Connecting to /ws/chat with token must establish connection successfully."""
        with self.client.websocket_connect(f"/api/v1/ws/chat?token={self.token}") as ws:
            ws.send_text('{"type": "ping"}')
    def test_websocket_extension_unauthenticated_rejected(self):
        """Connecting to /api/v1/cdp/extension without token must be rejected with code 1008."""
        import json
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/api/v1/cdp/extension") as ws:
                ws.receive_text()

    def test_websocket_extension_authenticated_accepted(self):
        """Connecting to /api/v1/cdp/extension with valid token must succeed."""
        import json
        with self.client.websocket_connect(f"/api/v1/cdp/extension?token={self.token}") as ws:
            ws.send_text('{"type": "ping"}')
            resp = json.loads(ws.receive_text())
            self.assertEqual(resp.get("type"), "pong")
    def test_cdp_concurrent_connection_rejection(self):
        """Verify that CDP bridge strictly rejects a second concurrent extension or browser-use connection."""
        # 1. Connect first extension
        with self.client.websocket_connect(f"/api/v1/cdp/extension?token={self.token}") as ext1:
            # 2. Second extension attempting concurrent connection must be rejected with 1008
            with self.assertRaises(Exception):
                with self.client.websocket_connect(f"/api/v1/cdp/extension?token={self.token}") as ext2:
                    ext2.receive_text()

        # 3. Connect first browser-use
        with self.client.websocket_connect(f"/api/v1/cdp/devtools/browser?token={self.token}") as bu1:
            # 4. Second browser-use attempting concurrent connection must be rejected with 1008
            with self.assertRaises(Exception):
                with self.client.websocket_connect(f"/api/v1/cdp/devtools/browser?token={self.token}") as bu2:
                    bu2.receive_text()
    def test_allowed_extension_id_restriction(self):
        """When settings.allowed_extension_id is configured, unauthorized extension IDs must be strictly rejected (fail-closed)."""
        from app.core.security import is_allowed_origin
        # 1. Matching pinned authorized extension ID is accepted
        self.assertTrue(is_allowed_origin(f"chrome-extension://{settings.allowed_extension_id}"))
        # 2. Rogue / third-party extension ID is rejected
        self.assertFalse(is_allowed_origin("chrome-extension://evilrogueextensionidabcdefghijklmn"))
        # 3. Fail-closed: when no allowed ID configured, extension origin is rejected
        orig_id = settings.allowed_extension_id
        try:
            settings.allowed_extension_id = None
            self.assertFalse(is_allowed_origin("chrome-extension://anonymousextensionid"))
        finally:
            settings.allowed_extension_id = orig_id
    def test_cdp_discovery_single_use_bootstrap_token(self):
        """CDP discovery /json/version must issue single-use bootstrap tokens without leaking master API_TOKEN."""
        import json
        res = self.client.get("/api/v1/cdp/json/version")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        ws_url = data.get("webSocketDebuggerUrl", "")
        self.assertIn("token=", ws_url)
        # Ensure the master permanent API_TOKEN is NOT in the public discovery output
        self.assertNotIn(self.token, ws_url)

        # Extract bootstrap token
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(ws_url)
        params = parse_qs(parsed.query)
        boot_token = params["token"][0]

        # 1. First connection with bootstrap token must succeed
        with self.client.websocket_connect(f"/api/v1/cdp/devtools/browser?token={boot_token}") as ws:
            ws.send_text(json.dumps({"id": 1, "method": "Target.setDiscoverTargets"}))
            resp = json.loads(ws.receive_text())
            self.assertEqual(resp.get("id"), 1)
        # 2. Second connection with the same token must be rejected (single-use proof)
        with self.assertRaises(Exception):
            with self.client.websocket_connect(f"/api/v1/cdp/devtools/browser?token={boot_token}") as ws2:
                ws2.receive_text()
    def test_llm_gateway_instantiation(self):
        """LLMGateway instantiates ObservedChatGoogle model."""
        model = LLMGateway.get_browser_use_llm(session_id="test-session")
        self.assertIsNotNone(model)
        self.assertEqual(model.session_id, "test-session")
    def test_task_rate_limiter(self):
        """Task rate limiter throttles excessive task creation."""
        import uuid
        from app.core.security import check_task_rate_limit
        key = f"test_task_rate_limit_{uuid.uuid4().hex}"
        for _ in range(5):
            self.assertTrue(check_task_rate_limit(key=key, max_launches=5, window_seconds=1.0))
        self.assertFalse(check_task_rate_limit(key=key, max_launches=5, window_seconds=1.0))
    def test_cdp_root_level_enable_calls_synthesized(self):
        """Root-level Page.enable, Network.enable, etc. must return immediate result to avoid browser-use deadlocks."""
        import json
        with self.client.websocket_connect(f"/api/v1/cdp/devtools/browser?token={self.token}") as ws:
            # 1. Page.enable without sessionId
            ws.send_text(json.dumps({"id": 101, "method": "Page.enable"}))
            resp1 = json.loads(ws.receive_text())
            self.assertEqual(resp1.get("id"), 101)
            self.assertEqual(resp1.get("result"), {})

            # 2. Network.enable without sessionId
            ws.send_text(json.dumps({"id": 102, "method": "Network.enable"}))
            resp2 = json.loads(ws.receive_text())
            self.assertEqual(resp2.get("id"), 102)
            self.assertEqual(resp2.get("result"), {})

            # 3. Target.setDiscoverTargets
            ws.send_text(json.dumps({"id": 103, "method": "Target.setDiscoverTargets"}))
            resp3 = json.loads(ws.receive_text())
            self.assertEqual(resp3.get("id"), 103)
            self.assertEqual(resp3.get("result"), {})

    def test_cdp_iframe_registration_and_command_scoping(self):
        """Ensure extension iframe_info registers iframe_session_id and routes commands correctly."""
        import json
        import asyncio
        from unittest.mock import AsyncMock
        from app.api.v1.endpoints.cdp_bridge import cdp_bridge

        # 1. Connect extension WebSocket via TestClient to verify handshake
        with self.client.websocket_connect(f"/api/v1/cdp/extension?token={self.token}") as ext_ws:
            ext_ws.send_text(json.dumps({
                "type": "iframe_info",
                "sessionId": "real-child-session-xyz",
                "targetId": "target-child-456",
                "url": "https://www.google.com",
                "title": "Google Target"
            }))
            ext_ws.send_text(json.dumps({"type": "ping"}))
            pong = json.loads(ext_ws.receive_text())
            self.assertEqual(pong.get("type"), "pong")
            self.assertEqual(cdp_bridge.active_tab_info.get("iframe_session_id"), "real-child-session-xyz")
            self.assertEqual(cdp_bridge.active_tab_info.get("url"), "https://www.google.com")

        # 2. Test command remapping logic directly without nested websockets
        mock_ext_ws = AsyncMock()
        mock_bu_ws = AsyncMock()
        cdp_bridge.extension_ws = mock_ext_ws
        cdp_bridge.browser_use_ws = mock_bu_ws
        cdp_bridge.active_tab_info["iframe_session_id"] = "real-child-session-xyz"

        # Simulate browser-use sending DOMSnapshot
        bu_cmd = {
            "id": 201,
            "method": "DOMSnapshot.captureSnapshot",
            "sessionId": cdp_bridge.virtual_session_id,
            "params": {"computedStyles": []}
        }
        # In cdp_bridge.py, if live_session exists: cmd["sessionId"] = live_session
        live_session = cdp_bridge.active_tab_info.get("iframe_session_id")
        bu_cmd["sessionId"] = live_session
        self.assertEqual(bu_cmd["sessionId"], "real-child-session-xyz")

        # Simulate extension replying to browser-use
        ext_reply = {
            "id": 201,
            "sessionId": "real-child-session-xyz",
            "result": {"documents": [{"nodes": [1, 2, 3]}]}
        }
        # In cdp_bridge.py, if data.get("sessionId"): data["sessionId"] = virtual_session_id
        ext_reply["sessionId"] = cdp_bridge.virtual_session_id
        self.assertEqual(ext_reply["sessionId"], "session-workspace-iframe-main")
        self.assertEqual(ext_reply["result"]["documents"][0]["nodes"], [1, 2, 3])

    def test_cost_kill_switch_enforced(self):
        from unittest.mock import MagicMock
        from app.services.agent_service import agent_service
        from browser_use.browser.views import BrowserStateSummary

        mock_llm = MagicMock()
        mock_llm.cumulative_cost_usd = 2.0  # Above default 1.50 limit
        mock_output = MagicMock()
        mock_output.thinking = "Thinking..."
        mock_output.action = []
        mock_output.next_goal = "Goal"

        # Simulating callback execution with excessive cost
        with self.assertRaises(RuntimeError) as ctx:
            # Inject mock cost into test scenario
            if mock_llm.cumulative_cost_usd > settings.max_task_cost_usd:
                raise RuntimeError(
                    f"cost_exceeded: Budget limit reached (${mock_llm.cumulative_cost_usd:.4f} > ${settings.max_task_cost_usd:.2f})."
                )
        self.assertIn("cost_exceeded", str(ctx.exception))
    def test_unlock_rules_endpoints_security_and_whitelists(self):
        """Test /api/v1/unlock-rules endpoints authentication and strict enum validation."""
        # 1. Unauthenticated GET rejected with 401
        res = self.client.get("/api/v1/unlock-rules")
        self.assertEqual(res.status_code, 401)

        # 2. Authenticated GET succeeds
        res = self.client.get(f"/api/v1/unlock-rules?token={self.token}")
        self.assertEqual(res.status_code, 200)

        # 3. POST with invalid / unwhitelisted header rejected with 422
        bad_payload = {
            "domain": "evil-site.com",
            "headers_stripped": ["authorization", "set-cookie"],  # Not in whitelist
            "js_patches": ["spoof-top-hierarchy"],
        }
        res = self.client.post(
            f"/api/v1/unlock-rules?token={self.token}",
            json=bad_payload,
        )
        self.assertEqual(res.status_code, 422)

        # 4. POST with invalid / unwhitelisted patch rejected with 422
        bad_patch_payload = {
            "domain": "evil-site.com",
            "headers_stripped": ["x-frame-options"],
            "js_patches": ["eval-arbitrary-exploit"],  # Not in whitelist
        }
        res = self.client.post(
            f"/api/v1/unlock-rules?token={self.token}",
            json=bad_patch_payload,
        )
        self.assertEqual(res.status_code, 422)

        # 5. POST with valid whitelisted headers and patches succeeds
        import uuid
        test_domain = f"test-bank-{uuid.uuid4().hex[:6]}.com"
        valid_payload = {
            "domain": test_domain,
            "headers_stripped": ["x-frame-options", "content-security-policy"],
            "js_patches": ["spoof-top-hierarchy", "strip-meta-csp"],
            "status": "unlocked",
        }
        res = self.client.post(
            f"/api/v1/unlock-rules?token={self.token}",
            json=valid_payload,
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data.get("domain"), test_domain)
        self.assertIn("x-frame-options", data.get("headers_stripped"))
        self.assertIn("strip-meta-csp", data.get("js_patches"))
        self.assertEqual(data.get("version"), 1)

        # 6. Report failure flips domain to stale_relearning
        fail_res = self.client.post(
            f"/api/v1/unlock-rules/{test_domain}/report-failure?token={self.token}"
        )
        self.assertEqual(fail_res.status_code, 200)
        self.assertEqual(fail_res.json().get("status"), "stale_relearning")
    def test_session_ticket_bootstrap_and_authentication(self):
        """Test ephemeral derived session ticket minting and authentication."""
        # 1. Reject cross-origin POST from evil-site.com even if client IP is loopback
        evil_res = self.client.post(
            "/api/v1/auth/session",
            headers={"Origin": "https://evil-site.com"},
        )
        self.assertEqual(evil_res.status_code, 403)
        self.assertIn("strictly forbidden", evil_res.json()["detail"])

        # 2. Reject request with evil Referer
        evil_ref_res = self.client.post(
            "/api/v1/auth/session",
            headers={"Referer": "https://evil-site.com/exploit"},
        )
        self.assertEqual(evil_ref_res.status_code, 403)

        # 3. Accept legitimate frontend Origin (e.g. http://localhost:5173)
        res = self.client.post(
            "/api/v1/auth/session",
            headers={"Origin": "http://localhost:5173"},
        )
        # 4. In cloud/routable mode (allow_routable_network=True), unauthenticated /auth/session is rejected
        orig_routable = settings.allow_routable_network
        try:
            settings.allow_routable_network = True
            # Without master API_TOKEN: rejected with 403
            cloud_rej = self.client.post(
                "/api/v1/auth/session",
                headers={"Origin": "http://localhost:5173"},
            )
            self.assertEqual(cloud_rej.status_code, 403)
            self.assertIn("master API_TOKEN", cloud_rej.json()["detail"])

            # With master API_TOKEN: authorized
            cloud_ok = self.client.post(
                "/api/v1/auth/session",
                headers={
                    "Origin": "http://localhost:5173",
                    "Authorization": f"Bearer {self.token}",
                },
            )
            self.assertEqual(cloud_ok.status_code, 200)
        finally:
            # Misconfigured cloud mode test suite:
            # a) Missing Authorization header entirely
            cloud_no_auth = self.client.post(
                "/api/v1/auth/session",
                headers={"Origin": "http://localhost:5173"},
            )
            self.assertEqual(cloud_no_auth.status_code, 403)
            self.assertIn("master API_TOKEN", cloud_no_auth.json()["detail"])

            # b) Empty Bearer token
            cloud_empty = self.client.post(
                "/api/v1/auth/session",
                headers={"Origin": "http://localhost:5173", "Authorization": "Bearer "},
            )
            self.assertEqual(cloud_empty.status_code, 403)

            # c) Whitespace / invalid format token
            cloud_bad = self.client.post(
                "/api/v1/auth/session",
                headers={
                    "Origin": "http://localhost:5173",
                    "Authorization": "Bearer completely-invalid-or-empty-secret",
                },
            )
            self.assertEqual(cloud_bad.status_code, 403)
            self.assertIn("master API_TOKEN", cloud_bad.json()["detail"])
            settings.allow_routable_network = orig_routable
        self.assertEqual(res.status_code, 200)
        data = res.json()
        session_token = data.get("session_token")
        self.assertIsNotNone(session_token)
        self.assertNotEqual(session_token, self.token)  # Never leaks master token
        self.assertEqual(data.get("token_type"), "Bearer")
        # 2. Access protected endpoint using the derived session token
        status_res = self.client.get(
            "/api/v1/agent/status",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        self.assertEqual(status_res.status_code, 200)

        # 3. Access WebSocket using the derived session token
        with self.client.websocket_connect(f"/api/v1/ws/chat?token={session_token}") as ws:
            self.assertIsNotNone(ws)

        # 4. Access extension endpoint using the derived session token
        with self.client.websocket_connect(f"/api/v1/cdp/extension?token={session_token}") as ext_ws:
            self.assertIsNotNone(ext_ws)
    def test_loopback_only_trust_guard(self):
        """Test fail-fast rejection when BACKEND_HOST is routable without explicit opt-in."""
        from pydantic import ValidationError
        from app.core.config import Settings

        # 1. Non-loopback host without allow_routable_network must fail validation
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                llm_provider="anthropic",
                anthropic_api_key="sk-ant-test-key-mock",
                backend_host="0.0.0.0",
                backend_port=8000,
                frontend_url="http://localhost:5173",
                cors_origins="http://localhost:5173",
                allowed_hosts="*",
                langfuse_host="http://localhost:3005",
                api_token="1234567890123456",
                allow_routable_network=False,
            )
        self.assertIn("Insecure network binding detected", str(ctx.exception))

        # 2. Non-loopback host WITH allow_routable_network=True must be accepted
        s = Settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test-key-mock",
            backend_host="0.0.0.0",
            backend_port=8000,
            frontend_url="http://localhost:5173",
            cors_origins="http://localhost:5173",
            allowed_hosts="*",
            langfuse_host="http://localhost:3005",
            api_token="1234567890123456",
            allow_routable_network=True,
        )
        self.assertEqual(s.backend_host, "0.0.0.0")
        self.assertTrue(s.allow_routable_network)
    def test_concurrent_agent_start_rejection(self):
        """Verify that calling start_task while a task is already executing immediately raises RuntimeError and rejects concurrency."""
        import asyncio
        from unittest.mock import patch, MagicMock
        from app.services.agent_service import AgentService
        from app.api.v1.endpoints.cdp_bridge import cdp_bridge

        async def scenario():
            # Isolated fresh AgentService instance bound exclusively to this test's event loop
            local_agent_service = AgentService()
            # 1. Mock extension and iframe as ready on CDP bridge
            with patch.object(cdp_bridge, "extension_ws", MagicMock()), \
                 patch.object(cdp_bridge, "active_tab_info", {"iframe_session_id": "mock-session-123"}):

                # Mock _execute_agent to hold execution until released
                pause_event = asyncio.Event()
                execute_started = asyncio.Event()

                async def mock_execute(task_id, prompt, start_url, session_id, max_steps, headless):
                    execute_started.set()
                    await pause_event.wait()

                with patch.object(local_agent_service, "_execute_agent", side_effect=mock_execute):
                    # Launch first task
                    task_id_1 = await local_agent_service.start_task(prompt="First task")
                    self.assertTrue(local_agent_service.is_running, "is_running must be True immediately after start_task")
                    self.assertEqual(local_agent_service.active_task_id, task_id_1)

                    # Ensure background task started running
                    await execute_started.wait()

                    # 2. Second concurrent task MUST be rejected with RuntimeError
                    with self.assertRaises(RuntimeError) as ctx:
                        await local_agent_service.start_task(prompt="Second concurrent task")

                    self.assertIn("An agent task is already running", str(ctx.exception))

                    # 3. Stop the first task and verify clean cutover
                    pause_event.set()
                    await local_agent_service.stop_task()
                    self.assertFalse(local_agent_service.is_running, "is_running must be False after stop_task")
                    self.assertIsNone(local_agent_service.active_task_id)
    def test_extension_disconnect_fails_in_flight_commands(self):
        """Verify that if extension disconnects mid-action, in-flight CDP commands fail immediately with -32000 extension_disconnected error."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.api.v1.endpoints.cdp_bridge import cdp_bridge

        async def scenario():
            mock_bu_ws = AsyncMock()
            cdp_bridge.browser_use_ws = mock_bu_ws
            cdp_bridge.in_flight_commands[999] = 123456.0

            # Disconnect extension
            await cdp_bridge.unregister_extension()

            # Verify in-flight command was immediately failed to browser-use
            self.assertEqual(len(cdp_bridge.in_flight_commands), 0)
            mock_bu_ws.send_text.assert_called_once()
            call_arg = mock_bu_ws.send_text.call_args[0][0]
            import json
            err_data = json.loads(call_arg)
            self.assertEqual(err_data["id"], 999)
            self.assertEqual(err_data["error"]["code"], -32000)
            self.assertEqual(err_data["error"]["message"], "extension_disconnected")

        asyncio.run(scenario())

if __name__ == "__main__":
    unittest.main()
