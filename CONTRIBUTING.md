# Contributing to Web-Frame Agent

Thank you for your interest in contributing to Web-Frame Agent!

This document outlines the development workflow, security invariants, testing procedures, and code standards for this project.

---

## 1. Code of Conduct & Standards

- **Language**: All code comments, docstrings, variable/function names, commit messages, and documentation must be written in professional English.
- **Commit Messages**: Must follow the [Conventional Commits](https://www.conventionalcommits.org/) specification (`feat:`, `fix:`, `test:`, `docs:`, `ci:`, `chore:`).
- **Formatting**:
  - No emojis anywhere in code, commit messages, console output, log statements, or markdown documentation.
  - Keep code explicit, minimal, and performant. Avoid unnecessary abstractions.

---

## 2. Architecture & Security Invariants

Before proposing changes, please be mindful of the architectural boundaries and security models enforced in this project:

1. **Chromium CDP Bridge Concurrency**:
   - The CDP bridge accepts strictly **one** Chrome extension connection and **one** `browser-use` connection concurrently.
   - Any secondary connection attempt must fail fast with a `1008 Policy Violation` rejection code.
2. **Loopback Confinement vs. Cloud Mode**:
   - By default (`BACKEND_HOST=127.0.0.1`), bootstrap session minting (`POST /api/v1/auth/session`) is restricted to the local loopback network socket descriptor (`request.scope["client"]`).
   - In cloud mode (`ALLOW_ROUTABLE_NETWORK=true`), unauthenticated session minting is blocked fail-closed; callers must authenticate using the master `API_TOKEN`.
3. **In-Flight Command Tracking**:
   - If the Chrome extension disconnects during active navigation or inspection, all pending in-flight commands must fail immediately with code `-32000` to prevent deadlocks in `browser-use`.
4. **Extension Isolation**:
   - Authentication secrets (`session_token`) exist in volatile memory only and must never be persisted to `chrome.storage.local`, `localStorage`, or cookies.

---

## 3. Local Development Setup

### Prerequisites

- **Python 3.12+** with `uv` package manager installed.
- **Node.js 22+** and **Bun** (or `pnpm` / `npm`).
- **Google Chrome** or **Chromium** (version 120+).

### Backend Setup

```bash
cd backend
cp .env.example .env
# Fill in your GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, and GOOGLE_APPLICATION_CREDENTIALS in .env
uv sync
```

To run the backend server:

```bash
uv run python run.py
```

### Frontend Setup

```bash
cd frontend
bun install
bun run dev
```

### Chrome Extension Setup

1. Open Chrome and navigate to `chrome://extensions/`.
2. Enable **Developer mode** in the top right corner.
3. Click **Load unpacked** and select the `extension/` directory.

---

## 4. Running Tests & Quality Checks

All contributions must pass the entire test suite and frontend checks before being merged.

### Backend & Security Tests

Run the unified test runner from the repository root:

```bash
python run_tests.py
```

This executes all three test suites in isolated processes to prevent proactor event loop conflicts on Windows:
- `backend/tests/test_security.py` (unit, anti-spoofing, rate limiting, and origin verification)
- `backend/tests/test_cdp_e2e_flow.py` (CDP bridge routing and watchdog lifecycle)
- `backend/tests/test_extension_scripts.py` (in-browser sandbox security and message handling)

### Frontend Checks

From the `frontend/` directory:

```bash
# Run the linter
bun run lint

# Verify production build
bun run build
```

---

## 5. Pull Request Guidelines

1. **Create a Feature Branch**: Work on a topic branch branched from `main` (e.g., `feat/add-new-metric` or `fix/cdp-buffer-timeout`).
2. **Atomic Commits**: Keep your commits focused and provide clear Conventional Commit messages.
3. **No Regressions**: Ensure `python run_tests.py` and `bun run build` exit with code `0`.
4. **Update Documentation**: If your pull request introduces new configuration settings, update `backend/.env.example` and `README.md` accordingly.
