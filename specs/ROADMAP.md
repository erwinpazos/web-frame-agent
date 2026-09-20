# Specifications - Roadmap of Future Evolutions

> **Status**: In progress (Section 1 Security 100% complete, Sections 6.1 and 8.1 complete).
> **Source**: Repo audit (Sept. 2026) - real architecture = extension <-> backend CDP bridge, not the standalone Chromium from the original `plan.md`.
> **Recommended execution order**: Section 1 (Security) -> Section 6 (Technical debt) -> Section 2 (Adaptive unlocking) -> Sections 3/4 (Bridge + extension robustness) -> Section 7 (Observability) -> Section 5 (Co-browsing) -> Section 8 (Publication).

Priorities: P0 = blocking before any network exposure / publication - P1 = product differentiator - P2 = robustness / comfort - P3 = polish.

---

## 1. Security (absolute priority, before everything else)

### 1.1 API authentication - `backend/app/api/`

**Problem**: no endpoint is authenticated. Today, any local process - or any web page visited by the user through a poorly restricted CORS/WebSocket request - can connect to `/api/v1/cdp/*` and **directly control the browser** (clicks, typing, JS execution inside the iframe). This is the project's highest risk.

- [x] Shared token (HMAC or ephemeral token generated at backend boot, displayed in the UI) required on:
  - `/api/v1/ws/chat` (via query param or `Sec-WebSocket-Protocol` header at handshake)
  - `/api/v1/agent/*` (`Authorization` header)
  - `/api/v1/cdp/*` **as absolute priority** (WS handshake + HTTP discovery endpoints `/json/*`)
- [x] Validate the origin of WebSocket connections at handshake (reject if `Origin` not in whitelist - the CORS middleware **does not cover WS** in FastAPI/Starlette).
- [x] The CDP bridge must accept **only one extension connection** and **only one browser-use connection** at a time, with explicit rejection of additional connections (prevent session hijacking by a second client).

**Acceptance criterion**: a request without a valid token on `/api/v1/cdp/json/version`, or a WS handshake from an unauthorized origin, receives a refusal (401 / close 1008). An integration test proves the legitimate frontend + extension still work.

### 1.3 Secret protection (.env not versioned) - verified

**State**: `.env` is strictly ignored by `.gitignore` (`.env`, `.env.*` with an exception for `.env.example`). The GCP service account key points to an external local path outside the repo (`C:\Users\erwin\gcp\...`).

- [x] `.env` not versioned and ignored by Git.
- [x] GCP key stored outside the repository.
- [x] Keep `.env.example` up to date with the required variables (without sensitive values).
### 1.5 Strict CORS - `backend/app/main.py`

**Problem**: `allow_origins` comes from `.env` with a dev-friendly wide default (`localhost:5173` + `127.0.0.1:5173`), `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]`.

- [x] Restrict `allow_methods`/`allow_headers` to the strict minimum (no `*`).
- [x] Add a middleware that rejects requests whose `Host` header is not `127.0.0.1`/`localhost` (DNS rebinding protection against a server accidentally listening on 0.0.0.0) - and bind the server to `127.0.0.1` by default, never `0.0.0.0`.

### 1.6 Rate limiting - REST + WS endpoints

**Problem**: an agent stuck in an error loop (or a second malicious client) can spam `start_task` -> unbounded LLM costs + saturated browser. `agent_service` only limits to **one concurrent task**, not frequency.

- [x] Per IP/session rate limit on `POST /agent/run` and WS `start_task` messages (e.g. 5 launches / minute).
- [x] **Per-task budget**: LLM step cap (`MAX_TASK_LLM_STEPS=30`); exceeding it -> clean stop with an `agent_error` of type `budget_exceeded`.

---

## 2. Adaptive iframe unlocking system (the real differentiator)

**Vision**: move from a **global, static, aggressive** strip (`rules.json` removes XFO/CSP everywhere, `<all_urls>`) to a system that is **per-domain, measured, learned, strictly authenticated, and exposed to the agent**.

### Architecture & Separation of Concerns
1. **Mechanical execution (Deterministic, no LLM)**: native Chromium signals (`chrome.webNavigation.onErrorOccurred`), pre-flight HEAD/GET background probes, dynamic DNR rules, and sandboxed named JS patch recipes.
2. **Strategic observer (LLM via agent tool)**: `browser-use` can call `probe_and_unlock_iframe(url)` to understand why a page was locked, inspect diagnostics, and request escalation without writing code or generating rules.
3. **Closed whitelists**: recipes never contain arbitrary code or free-form headers. Headers and JS patches are validated against strict enums.

---

### 2.1 Dual-Channel Detection & Signal Pipeline
- Channel 1 — Native HTTP Header Errors (Zero-Timeout):
  - Do NOT rely on 3-second DOM timeouts for HTTP headers.
  - Hook `chrome.webNavigation.onErrorOccurred` specifically for `sub_frame` requests.
  - Intercept native browser abort codes immediately:
    - `net::ERR_BLOCKED_BY_RESPONSE` (typical for `X-Frame-Options: DENY / SAMEORIGIN`).
    - `net::ERR_BLOCKED_BY_CLIENT` or CSP frame-ancestors violation errors.
  - Signal triggers instant rule synthesis for the target domain without waiting.
- Channel 2 — Heuristic JS Framebusting & Blank DOM (Timeout-Based):
  - Used **strictly for JavaScript framebusting** (`top !== self`, body clearing, redirection loops) where no browser error event exists.
  - `MutationObserver` on `document.documentElement` + timeout (body empty `< N` nodes after 3s, or rapid outgoing `beforeunload` without user gesture).
  - Triggers JS patch recipe escalation.

### 2.2 Background Pre-Flight Probe (Eliminating the "First-Visit Breakage")
- Problem: navigating directly to an unknown domain in the visible iframe results in a broken page flash before the rule can be learned.
- Solution:
  - When a navigation to a new/unknown domain is requested (via agent or URL bar), the background service worker executes a silent `fetch(target_url, { method: "HEAD", mode: "no-cors" / credentials: "omit" })` (or low-overhead GET with early abort) **before** updating the visible iframe `src`.
  - Analyzes response headers (`x-frame-options`, `content-security-policy`).
  - Pre-mints the dynamic DNR rule for the domain **prior** to the iframe load.
  - Outcome: ~90% of header-based blocks are unlocked transparently on the very first render without visible flickering or reload loops.

### 2.3 Targeted Per-Domain Rule Generation (`declarativeNetRequest`)
- Replace the static `rules.json` with `chrome.declarativeNetRequest.updateDynamicRules()` generated **per domain and per detected header**:
  - Strip only identified blocking headers: `x-frame-options`, `content-security-policy` (frame-ancestors only when possible, or full CSP on sub-frame document only).
  - Scope strictly to `resourceTypes: ["sub_frame"]` on `urlFilter: "||domain.com"`.
  - Dynamic rule lifecycle: persisted in extension local cache and synced with backend store.

### 2.4 Strict API Security & Closed Whitelists (`/api/v1/unlock-rules`)
- Vulnerability: exposing raw header deletion or code injection to an API turns it into an arbitrary security-stripping backdoor.
- Enforcement:
  - Authentication: `GET` and `POST /api/v1/unlock-rules` strictly protected by `API_TOKEN` (Bearer header or query token) identical to CDP and chat endpoints.
  - Closed Header Whitelist: `POST` payload only accepts a rigid enum of headers:
    - `ALLOWED_STRIP_HEADERS = {"x-frame-options", "content-security-policy", "content-security-policy-report-only", "frame-options"}`.
    - Any request attempting to strip other headers (e.g. `authorization`, `set-cookie`, `access-control-allow-origin`) is rejected with `422 Unprocessable Entity`.
  - Closed JS Patch Whitelist: `js_patches` only accepts vetted primitive identifiers:
    - `ALLOWED_JS_PATCHES = {"spoof-top-hierarchy", "hide-webdriver", "rewrite-cookies-chips", "contain-window-open", "strip-meta-csp"}`.
    - Zero arbitrary JS code allowed in storage or transit.

### 2.5 Recipe Expiration & Adaptive Re-learning Strategy
- Problem: static `status: "unlocked"` recipes cause silent permanent failures when sites update their defenses or add new protections.
- Strategy:
  - TTL & Stale-While-Revalidate: each recipe has an `updated_at` and `expires_at` (default TTL: 7 days).
  - Failure-Triggered Re-learning: if a domain marked as `status: "unlocked"` triggers an `onErrorOccurred` or DOM framebusting failure, it does **not** fail permanently. The recipe status immediately flips to `status: "stale_relearning"`.
  - The probe pipeline re-runs (background pre-flight -> header update -> JS patch escalation) and updates the recipe with a bumped version number.
  - Backend SQLite store tracks: `{domain, headers_stripped, js_patches, success_count, failure_count, last_verified_at, version}`.

### 2.6 DOM `<meta http-equiv>` CSP Stripping
- Problem: `declarativeNetRequest` only inspects network HTTP headers. Sites using `<meta http-equiv="Content-Security-Policy" content="...">` or `<meta http-equiv="X-Frame-Options">` embedded in raw HTML escape DNR rules.
- Solution:
  - Dedicated `strip-meta-csp` primitive in `content_main.js` (running at `document_start` before parsing completes).
  - Synchronous `MutationObserver` on `document.documentElement` targeting `<meta>` tags:
    - Detects and removes any `<meta http-equiv>` matching CSP or X-Frame directives before browser enforcement engine locks the context.
  - Tagged as a distinct patch in the domain recipe (`js_patches: ["strip-meta-csp"]`).

### 2.7 Expose Unlocking as an Agent Tool (`probe_and_unlock_iframe`)
- Dedicated `browser-use` tool: `probe_and_unlock_iframe(url)`.
- When an agent encounters an empty page or navigation failure:
  - Invokes the tool to receive structured status: `{domain, detected_headers, applied_dnr_rules, applied_js_patches, verdict: "unlocked" | "needs_relearning" | "unrecoverable_ghost_tab_fallback"}`.
  - Broadcasts an `unlock_probe` event to the frontend WebSocket for real-time human visibility in the chat feed.
---

## 3. CDP Bridge - `backend/app/api/v1/endpoints/cdp_bridge.py`

### 3.1 Own the "shim" naming
- [ ] Rename the module to `cdp_shim.py` (or add a 10-line header comment): "**This is not a real DevTools server - it is a protocol proxy/fabricator that emulates the `Target.*`/`Browser.*` domains for Playwright/browser-use and relays domain commands to the extension.**" Cross-reference in the README (Section 8.3).

### 3.3 Clean reconnection mid-action
- [ ] If the extension disconnects while a command is in flight: immediately reply with a **CDP error** `{id, error: {code: -32000, message: "extension_disconnected"}}` to browser-use (instead of leaving the client on a silent timeout), so its retry logic applies.
- [ ] Track in-flight commands `msg_id -> client_ws` with a purge timer (the current queue has no timeout at all: a dead extension = browser-use hanging until its global timeout).

### 3.4 Command-typed timeouts
- [ ] Config per class: `Page.navigate` (30 s), click/input (5 s), `Page.captureScreenshot` (15 s, the OffscreenCanvas can be slow), `Runtime.evaluate` (10 s). Configurable in `.env` / `config.py`.

---

## 4. Chrome Extension

### 4.1 Generic anti-detection lock
- [ ] Monitor vectors beyond framing: `Runtime.enable` timing attacks (CreepJS/Cloudflare), `navigator`/`WebGL`/`plugins` consistency, Chrome 125+ flat sessions visible page-side. Low-cost: do not enable `Runtime.enable` globally for no reason; keep the existing spoofs.
- [ ] Maintain a written watch (README section or doc) of known detections per vendor (Cloudflare / DataDome / Akamai) and bypass status - the real asset remains "real Chrome, real IP, real profile".

### 4.2 JS dialogs (`alert`/`confirm`/`prompt`)
- [ ] **Verify current behavior**: a blocking `alert()` in the iframe can freeze the page thread and the debugger -> the agent locks up.
- [ ] If unhandled: hook `JS.dialogOpening` (CDP `Page.javascriptDialogOpening`) relayed to the backend, configurable policy (auto-dismiss by default, auto-accept on application checkboxes if the human confirms); or interception via `content_main.js` (override `window.alert` -> no-op + log).

### 4.3 Command queue during disconnection
- [ ] Current state: 5 s heartbeat + auto-reconnect, but the backend **queues nothing** - if the extension is absent, commands receive an immediate error (correct for `/run`), but during a running task it is the same: confirm the behavior and add a short retry window (e.g. 20 s) for idempotent commands (DOM read/screenshot) before erroring, to allow for the extension's WS reconnection.

### 4.4 Explicit host tab binding
- [ ] `chrome.debugger.attach({tabId})` targets a tab, but the host tab choice is implicit (`findHostTab()` = first `localhost:5173` match, fallback active tab). Make the binding **explicit**: the frontend passes its `tabId` (via a message to the extension on load), and the bridge only talks to that specific tab.
---

## 5. Human / agent coordination (co-browsing)

### 5.1 Soft-lock on human interaction
- [ ] Frontend side: capture `mousedown`/`keydown`/`wheel` in the iframe container (internal events of a cross-origin iframe do not propagate -> use `window.addEventListener('blur')` + pointer-events overlay, or a message from the content script running with `all_frames: true`).
- [ ] On a human gesture: pause the agent's CDP actions for X ms (configurable, default ~1500 ms) - the shim defers `Input.*` commands, not read-only ones.
- [ ] End of pause: re-sync of the DOM state before the next action (browser-use recaptures).

### 5.2 "Who is in control" indicator
- [ ] Iframe border: pulsing blue = agent active, green = human control, amber = conflict/pause. Manual "I take control" toggle (full agent pause) in the top bar.

### 5.3 Conflict journal
- [ ] If an agent action and a human gesture land within < N ms of each other on the same zone (`Input.dispatchMouseEvent` coordinates): structured `confluence_log` (step, coords, ts) visible in the chat thread and in Langfuse - turn silent bugs into data.

---

## 6. Architecture cleanup / technical debt

### 6.1 `browser_manager.py` and Playwright leftovers
- [x] Decide: **delete** or mark `# LEGACY - not used by the critical path`. *(Done: router disabled and module marked LEGACY)*.
- [x] Remove the associated `.chrome_profile`/anti-detection leftovers. *(Done: folder deleted from disk and references cleaned up in the backend)*.
### 6.2 Align the docs with the real architecture
- [x] `plan.md`: obsolete standalone-Chromium plan deleted from the repo (history preserved in git); `README.md` and this roadmap are the references.
- [x] Create the root `README.md` with the real CDP bridge diagram, the startup guides, and the security warning.

### 6.3 Frontend naming consistency
- [x] Harmonization: the code uses `Chatbot.tsx` and `useAgentChat.ts` (unified chat-first model), the docs are aligned.
---

## 7. Observability

### 7.1 Per-domain metrics
- [ ] Aggregations from the 2.5 store: unlock success rate, average time to OK render, top persistently failing sites -> table in the UI or export, to prioritize patches.

### 7.2 CDP tracing at the same level as the LLM
- [ ] `llm_gateway` already traces every generation (tokens/cost/latency). Extend the principle to the bridge: a span per CDP command `{method, latency, ok/err, payload size}` to Langfuse (or the trace backend) with the same `session_id` as the task.
- [ ] Benefit: distinguish "the LLM is slow" (spaced steps) from "the browser/extension is slow" (long CDP spans) - currently impossible to diagnose.

---

## 8. Packaging for open-source publication

- [x] Audit of all hardcoded values: the old hardcoded default target URL appeared **at least 4 times** (`agent_service.py`, `agent.py`, `ws.py`, `cdp_bridge.py`, `App.tsx`) -> a single config constant `DEFAULT_TARGET_URL`.
- [ ] Verify ports/model/project: already good via `core/config.py`, extend the same practice to the frontend (Vite `.env` for `VITE_API_URL`).

### 8.2 Generic default profile
- [ ] Ship `extension/profiles/generic-default.json` (header-probe unlocking + minimal top spoof) as the **default** behavior, with per-site profiles as optional examples. Prove it on 2-3 different site families in the docs -> the project sells as "multi-site browser automation", not as a single-site hack.

### 8.3 README "How it works"
- [ ] Central section with the full-flow diagram (frontend -> WS -> agent -> CDP shim -> extension -> debugger -> iframe) and the 3 key ideas:
  1. the **CDP shim** fabricates the `Target.*`/`Browser.*` responses so Playwright believes it is driving a dedicated Chromium;
  2. the **extension** is the only real command executor, via `chrome.debugger` scoped to the iframe;
  3. the trio **dnet-request + MAIN world content scripts + OffscreenCanvas** makes the site visible, controllable, and capturable by the LLM.
- [ ] This is the repo's technical selling point - do not leave it implicit in the code.

### 8.4 Publication repo hygiene
- [ ] Explicit license (currently none) + ToS warning: automating third-party websites may violate their terms of service; project for educational/personal use only.
- [ ] Minimal CI: backend `ruff`/`pytest`, frontend `tsc --noEmit`/`oxlint`, secret scan (1.3).
- [ ] Step-by-step installation guide (the inline help in `App.tsx` is good content, to duplicate in the README with screenshots).

---

## Dependency Summary

```
1.1 (CDP auth) --> 4.4 (explicit host tab binding)
1.1, 1.3, 1.5, 1.6 --> 8.x (publication)
2.1 (probe) --> 2.2 (targeted rules) --> 2.5 (learning) --> 7.1 (metrics)
2.3 (failure detection) --> 2.4 (profiles) --> 2.6 (agent tool)
6.1, 6.2 (cleanup) --> 8.3 (README)
5.1 (soft-lock) --> 2.7 (Storage Access API, needs the human gesture)
```
