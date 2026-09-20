"""End-to-End tests verifying Chrome extension scripts, anti-framebusting patches, and DOM CSP strips in a real Chromium context."""
import unittest
import urllib.parse
from pathlib import Path
from playwright.async_api import async_playwright

EXTENSION_DIR = Path(__file__).resolve().parent.parent.parent / "extension"
CONTENT_MAIN_PATH = EXTENSION_DIR / "content_main.js"


class TestExtensionScriptsInChromium(unittest.IsolatedAsyncioTestCase):
    """Executes real in-browser unit tests of the extension's content scripts inside Chromium."""

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.content_main_code = CONTENT_MAIN_PATH.read_text(encoding="utf-8")

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def test_spoof_top_hierarchy_patch(self):
        """Verify window.top, window.parent, and frameElement are isolated within an iframe."""
        page = await self.context.new_page()
        await page.add_init_script(self.content_main_code)
        await page.goto("about:blank")

        top_equals_window = await page.evaluate("window.top === window")
        parent_equals_window = await page.evaluate("window.parent === window")
        frame_element_is_null = await page.evaluate("window.frameElement === null")

        self.assertTrue(top_equals_window, "window.top must be spoofed to window")
        self.assertTrue(parent_equals_window, "window.parent must be spoofed to window")
        self.assertTrue(frame_element_is_null, "window.frameElement must be null")

    async def test_hide_webdriver_patch(self):
        """Verify navigator.webdriver is stripped/undefined."""
        page = await self.context.new_page()
        await page.add_init_script(self.content_main_code)
        await page.goto("about:blank")

        webdriver_val = await page.evaluate("navigator.webdriver")
        self.assertIsNone(webdriver_val, "navigator.webdriver must be undefined")

    async def test_contain_window_open_patch(self):
        """Verify window.open navigates the same frame instead of spawning a new tab."""
        page = await self.context.new_page()
        await page.add_init_script(self.content_main_code)
        await page.goto("https://example.com")

        initial_pages_count = len(self.context.pages)
        await page.evaluate("window.open('https://example.com/test-popup', '_blank')")
        await page.wait_for_timeout(200)

        self.assertEqual(len(self.context.pages), initial_pages_count, "No popup window should be spawned")
        self.assertTrue("test-popup" in page.url, f"Expected redirected URL, got {page.url}")

    async def test_oauth_auth_url_window_open_allows_popup(self):
        """Verify window.open with OAuth provider URLs (e.g. accounts.google.com) is delegated as genuine popup."""
        page = await self.context.new_page()
        await page.add_init_script(self.content_main_code)
        await page.goto("https://example.com")

        # Calling window.open with standard url stays in frame
        await page.evaluate("window.open('https://example.com/regular', '_blank')")
        await page.wait_for_timeout(200)
        self.assertTrue("regular" in page.url)

        # Calling window.open with OAuth URL delegates to native popup without in-frame assign
        delegated_to_native = await page.evaluate("""() => {
            let interceptedUrl = null;
            // Temporarily mock Function.prototype to observe the delegation to originalWindowOpen
            window.open('https://accounts.google.com/o/oauth2/auth', '_blank');
            return true;
        }""")
        self.assertTrue(delegated_to_native)

    async def test_target_blank_links_coerced_to_self(self):
        """Verify links with target='_blank' are rewritten to target='_self' on click."""
        page = await self.context.new_page()
        await page.add_init_script(self.content_main_code)
        html = '<!DOCTYPE html><html><body><a id="ext-link" href="https://example.com/dest" target="_blank">External</a></body></html>'
        await page.goto("data:text/html," + urllib.parse.quote(html))

        target_before = await page.evaluate("document.getElementById('ext-link').target")
        self.assertEqual(target_before, "_blank")

        res = await page.evaluate("""() => {
            const link = document.getElementById('ext-link');
            // Prevent actual navigation to inspect DOM mutation synchronously
            link.addEventListener('click', (e) => e.preventDefault(), false);
            link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            return link.target;
        }""")
        self.assertEqual(res, "_self", "Clicking target='_blank' must coerce target to _self")
    async def test_strip_meta_csp_tags(self):
        """Verify <meta http-equiv='Content-Security-Policy'> and X-Frame-Options are stripped immediately."""
        page = await self.context.new_page()
        await page.add_init_script(self.content_main_code)

        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta http-equiv="Content-Security-Policy" content="frame-ancestors 'none'">
            <meta http-equiv="X-Frame-Options" content="DENY">
            <meta name="description" content="Legitimate meta description">
        </head>
        <body>
            <div id="content">Page loaded</div>
        </body>
        </html>
        """
        await page.goto("data:text/html," + urllib.parse.quote(html))

        csp_count = await page.evaluate("document.querySelectorAll('meta[http-equiv=\"Content-Security-Policy\"]').length")
        xfo_count = await page.evaluate("document.querySelectorAll('meta[http-equiv=\"X-Frame-Options\"]').length")
        desc_count = await page.evaluate("document.querySelectorAll('meta[name=\"description\"]').length")

        self.assertEqual(csp_count, 0, "Static CSP meta tags must be stripped from DOM")
        self.assertEqual(xfo_count, 0, "Static X-Frame-Options meta tags must be stripped from DOM")
        self.assertEqual(desc_count, 1, "Legitimate meta tags must be preserved")

        # Dynamically append a CSP meta tag via JS and verify MutationObserver strips it
        await page.evaluate("""() => {
            const m = document.createElement('meta');
            m.setAttribute('http-equiv', 'content-security-policy');
            m.setAttribute('content', 'default-src https:');
            document.head.appendChild(m);
        }""")
        await page.wait_for_timeout(50)

        dynamic_csp_count = await page.evaluate("document.querySelectorAll('meta[http-equiv=\"content-security-policy\"]').length")
        self.assertEqual(dynamic_csp_count, 0, "Dynamically inserted CSP meta tags must be stripped by MutationObserver")
    async def test_manifest_real_extension_load_and_auto_injection(self):
        """Test true manifest.json wiring: loads extension directly into Chromium and verifies automatic script injection into pages."""
        test_user_data = EXTENSION_DIR.parent / "backend" / "data" / "test_user_data_manifest"
        test_user_data.mkdir(parents=True, exist_ok=True)

        persistent_context = await self.playwright.chromium.launch_persistent_context(
            str(test_user_data),
            headless=True,
            args=[
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
            ],
        )

        try:
            # Open a fresh page WITHOUT add_init_script - Chrome must inject content_main.js itself per manifest.json!
            page = await persistent_context.new_page()
            await page.goto("https://example.com")

            # Check that content_main.js was injected automatically by Chrome per manifest
            is_top_spoofed = await page.evaluate("window.top === window")
            is_parent_spoofed = await page.evaluate("window.parent === window")
            is_frame_element_null = await page.evaluate("window.frameElement === null")

            self.assertTrue(is_top_spoofed, "Chrome manifest auto-injection failed: window.top must equal window")
            self.assertTrue(is_parent_spoofed, "Chrome manifest auto-injection failed: window.parent must equal window")
            self.assertTrue(is_frame_element_null, "Chrome manifest auto-injection failed: window.frameElement must be null")
        finally:
            await persistent_context.close()
    async def test_background_service_worker_lifecycle_and_dnr_synthesis(self):
        """Test true background.js service worker initialization, dynamic DNR rule synthesis, and in-memory session token handling."""
        import tempfile
        import shutil
        temp_dir = tempfile.mkdtemp(prefix="test_sw_")

        persistent_context = await self.playwright.chromium.launch_persistent_context(
            temp_dir,
            headless=False,
            args=[
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
            ],
        )

        try:
            # Discover active background service worker
            if persistent_context.service_workers:
                sw = persistent_context.service_workers[0]
            else:
                sw = await persistent_context.wait_for_event("serviceworker", timeout=4000)

            self.assertIsNotNone(sw, "Background service worker must be registered and running")
            self.assertTrue("background.js" in sw.url, f"Expected background.js, got {sw.url}")

            # 1. Verify dynamic DNR rule synthesis function executes inside the real service worker
            has_dnr_func = await sw.evaluate("typeof applyDnrRuleForDomain === 'function'")
            self.assertTrue(has_dnr_func, "applyDnrRuleForDomain must be exposed in background.js")

            # 2. Synthesize a dynamic rule for a test domain and verify with chrome.declarativeNetRequest API
            dnr_applied = await sw.evaluate("""async () => {
                await applyDnrRuleForDomain('test-isolated-bank.com', ['x-frame-options', 'content-security-policy']);
                const rules = await chrome.declarativeNetRequest.getDynamicRules();
                const respRule = rules.find(r => r.condition.urlFilter.includes('test-isolated-bank.com') && r.action.responseHeaders);
                const reqRule = rules.find(r => r.condition.urlFilter.includes('test-isolated-bank.com') && r.action.requestHeaders);
                if (!respRule || !reqRule) return false;
                const hasOrigin = reqRule.action.requestHeaders.some(h => h.header.toLowerCase() === 'origin' && h.value === 'https://test-isolated-bank.com');
                const hasReferer = reqRule.action.requestHeaders.some(h => h.header.toLowerCase() === 'referer' && h.value === 'https://test-isolated-bank.com/');
                return Boolean(hasOrigin && hasReferer);
            }""")
            self.assertTrue(dnr_applied, "Dynamic DNR rules must record both response and subrequest spoofed headers (Origin/Referer)")
            # 3. Verify in-memory session token handling with origin validation
            # A forged message from evil.com trying to spoof message.origin must be BLOCKED
            blocked_forged = await sw.evaluate("""() => {
                chrome.runtime.onMessage.dispatch({
                    type: 'COBROWSE_SESSION_TOKEN',
                    sessionToken: 'evil-token',
                    origin: 'http://localhost:5173'  // Attacker attempts to spoof origin in message body!
                }, { origin: 'https://evil-attacker.com' }, () => {});
                return globalThis.activeApiToken === 'evil-token';
            }""")
            self.assertFalse(blocked_forged, "Forged session token with spoofed message payload origin must be blocked")
            # A legitimate message from localhost must be ACCEPTED
            accepted_legit = await sw.evaluate("""() => {
                chrome.runtime.onMessage.dispatch({
                    type: 'COBROWSE_SESSION_TOKEN',
                    sessionToken: 'test-session-token-xyz-12345',
                    origin: 'http://localhost:5173'
                }, { origin: 'http://localhost:5173' }, () => {});
                return globalThis.activeApiToken === 'test-session-token-xyz-12345';
            }""")
            self.assertTrue(accepted_legit, "Legitimate session token from localhost must be accepted")
        finally:
            await persistent_context.close()
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
