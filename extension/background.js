// Background Service Worker for Web-Frame CDP Bridge
// Chrome 125+ Flat Sessions: Scoped strictly to the target iframe OOPIF child session

globalThis.backendWsUrl = null;
globalThis.configuredFrontendUrl = null;
globalThis.configuredFrontendHost = null;
globalThis.activeApiToken = null;
let backendWsUrl = null;
let configuredFrontendUrl = null;
let configuredFrontendHost = null;
let activeApiToken = null;
// In-memory cache of domain unlock rules: domain -> { headers: [], patches: [], status, version }
const domainRulesCache = new Map();
let nextDnrRuleId = 1000;
const domainToRuleId = new Map();
let ws = null;
let cachedIframeRect = null;
let discoveryInterval = null;
let attachedHostTab = false;
let iframeSessionId = null;
let iframeTargetInfo = null;
const childIframeSessions = new Map();
let reconnectTimer = null;
console.log('[CDP Bridge] Background service worker initialized with Flat Sessions support.');

// Unlock third-party cookies & storage for all embedded iframes (required for cross-origin sites behind WAF/CDN protection)
function enableThirdPartyCookiesAndStorage() {
  try {
    if (chrome.contentSettings && chrome.contentSettings.cookies) {
      chrome.contentSettings.cookies.set({
        primaryPattern: '<all_urls>',
        secondaryPattern: '<all_urls>',
        setting: 'allow',
      }, () => {
        console.log('[CDP Bridge] Third-party cookies explicitly allowed for all embedded frames.');
      });
    }
    if (chrome.privacy && chrome.privacy.websites && chrome.privacy.websites.thirdPartyCookiesAllowed) {
      chrome.privacy.websites.thirdPartyCookiesAllowed.set({ value: true }, () => {
        console.log('[CDP Bridge] Global thirdPartyCookiesAllowed set to true.');
      });
    }
  } catch (err) {
    console.warn('[CDP Bridge] Error setting contentSettings:', err);
  }

  // Note: Set-Cookie headers are directly upgraded at the network level via Network.setCookie (CDP),
  // which avoids domain-clash race conditions with chrome.cookies.onChanged.
}
enableThirdPartyCookiesAndStorage();
// --- 2. Adaptive Iframe Dynamic DNR & Pre-Flight Probe ---

async function fetchUnlockRules() {
  if (!backendWsUrl || !activeApiToken) return;
  try {
    const parsedWs = new URL(backendWsUrl);
    const protocol = parsedWs.protocol === 'wss:' ? 'https:' : 'http:';
    const httpBase = `${protocol}//${parsedWs.host}`;
    const res = await fetch(`${httpBase}/api/v1/unlock-rules?token=${encodeURIComponent(activeApiToken)}`);
    if (!res.ok) {
      console.warn('[CDP Bridge] Failed to fetch unlock rules from backend:', res.status);
      return;
    }
    const data = await res.json();
    console.log(`[CDP Bridge] Fetched ${data.total} domain unlock rules from backend.`);
    for (const rule of data.rules || []) {
      domainRulesCache.set(rule.domain.toLowerCase(), rule);
      await applyDnrRuleForDomain(rule.domain, rule.headers_stripped || []);
    }
  } catch (err) {
    console.warn('[CDP Bridge] Error fetching unlock rules:', err);
  }
}

// Universal apex domain extractor for multi-subdomain API coverage (e.g. www.reddit.com -> reddit.com covering gql.reddit.com)
const KNOWN_PUBLIC_SUFFIXES = ['herokuapp.com', 'github.io', 'pages.dev', 'vercel.app', 'web.app', 'firebaseapp.com'];
function getApexDomain(domain) {
  if (!domain || typeof domain !== 'string') return '';
  const clean = domain.toLowerCase().trim().replace(/^\./, '');
  for (const suffix of KNOWN_PUBLIC_SUFFIXES) {
    if (clean.endsWith('.' + suffix)) {
      const prefix = clean.slice(0, clean.length - suffix.length - 1);
      const subParts = prefix.split('.');
      return subParts[subParts.length - 1] + '.' + suffix;
    }
  }
  const parts = clean.split('.');
  if (parts.length <= 2) return clean;
  const twoPartTLDs = new Set(['co.uk', 'com.au', 'co.nz', 'co.jp', 'com.br', 'co.in', 'gouv.fr', 'org.uk']);
  const lastTwo = parts.slice(-2).join('.');
  if (twoPartTLDs.has(lastTwo)) {
    return parts.slice(-3).join('.');
  }
  return parts.slice(-2).join('.');
}

async function applyDnrRuleForDomain(domain, headersToStrip) {
  if (!chrome.declarativeNetRequest || !headersToStrip || headersToStrip.length === 0) return;
  try {
    const domainClean = domain.toLowerCase().trim();
    const apexDomain = getApexDomain(domainClean) || domainClean;

    let ruleId = domainToRuleId.get(domainClean);
    if (!ruleId) {
      ruleId = nextDnrRuleId++;
      domainToRuleId.set(domainClean, ruleId);
    }

    const responseHeaders = headersToStrip.map((header) => ({
      header: header.toLowerCase(),
      operation: 'remove',
    }));

    // Universal Origin & Referer spoofing: rewrite subresource requests (XHR, fetch, sub_frame)
    // using the apex root domain so that all cross-subdomain APIs (e.g. gql.reddit.com, oauth.reddit.com)
    // accept in-iframe requests without CORS or 403 rejections.
    const targetOrigin = `https://${domainClean}`;
    const targetReferer = `https://${domainClean}/`;
    const requestHeaders = [
      { header: 'Origin', operation: 'set', value: targetOrigin },
      { header: 'Referer', operation: 'set', value: targetReferer },
    ];
    // Filter applies to both the specific sub-domain AND the root apex domain (||apexDomain matches all subdomains)
    const filterDomain = apexDomain || domainClean;
    const requestRuleId = ruleId + 10000;

    // Rule 1: Strip blocking response headers on ALL resource types (including main_frame)
    const responseRule = {
      id: ruleId,
      priority: 1,
      action: {
        type: 'modifyHeaders',
        responseHeaders,
      },
      condition: {
        urlFilter: `||${filterDomain}`,
        resourceTypes: ['main_frame', 'sub_frame', 'xmlhttprequest', 'other'],
      },
    };

    // Rule 2: Universal Origin & Referer spoofing for ANY sub-request (XHR, fetch, sub_frame, other)
    // INITIATED by this domain, regardless of target destination (e.g. AWS S3, Google Cloud, external APIs).
    // Using initiatorDomains instead of a destination urlFilter ensures all outgoing uploads and API calls
    // carry the site's genuine Origin and Referer, exactly as in a standalone browser tab.
    const initiatorList = Array.from(new Set([domainClean, apexDomain].filter(Boolean)));
    const requestRule = {
      id: requestRuleId,
      priority: 1,
      action: {
        type: 'modifyHeaders',
        requestHeaders,
      },
      condition: {
        initiatorDomains: initiatorList,
        resourceTypes: ['sub_frame', 'xmlhttprequest', 'other'],
      },
    };
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: [ruleId, requestRuleId],
      addRules: [responseRule, requestRule],
    });

    console.log(`[CDP Bridge] Dynamic DNR rules (${ruleId}, ${requestRuleId}) active for '${domainClean}' (Apex: '${filterDomain}'): stripped headers:`, headersToStrip, `spoofed Origin: '${targetOrigin}' on subrequests`);
  } catch (err) {
    console.warn(`[CDP Bridge] Error applying DNR rule for '${domain}':`, err);
  }
}

async function reportUnlockRuleToBackend(domain, headersStripped, jsPatches) {
  if (!backendWsUrl || !activeApiToken) return;
  try {
    const parsedWs = new URL(backendWsUrl);
    const protocol = parsedWs.protocol === 'wss:' ? 'https:' : 'http:';
    const httpBase = `${protocol}//${parsedWs.host}`;

    const payload = {
      domain: domain.toLowerCase().trim(),
      headers_stripped: headersStripped,
      js_patches: jsPatches,
      status: 'unlocked',
    };

    const res = await fetch(`${httpBase}/api/v1/unlock-rules?token=${encodeURIComponent(activeApiToken)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (res.ok) {
      const saved = await res.json();
      domainRulesCache.set(domain.toLowerCase(), saved);
      console.log(`[CDP Bridge] Successfully reported unlock rule for '${domain}' to backend.`);
    }
  } catch (err) {
    console.warn(`[CDP Bridge] Error reporting unlock rule for '${domain}' to backend:`, err);
  }
}

async function preflightProbeForUrl(url) {
  try {
    const parsed = new URL(url);
    const domain = parsed.hostname.toLowerCase();
    if (!domain || domain === 'localhost' || domain === '127.0.0.1') return;

    if (domainRulesCache.has(domain)) {
      const cached = domainRulesCache.get(domain);
      if (cached.headers_stripped && cached.headers_stripped.length > 0) {
        await applyDnrRuleForDomain(domain, cached.headers_stripped);
        return;
      }
    }

    console.log(`[CDP Bridge] Running silent background pre-flight probe for '${domain}'...`);
    // Chrome Extension MV3 with host_permissions (<all_urls>) allows standard fetch with readable response headers!
    // NEVER use mode: 'no-cors' here, as no-cors returns an opaque response stripping all header visibility.
    const resp = await fetch(url, {
      method: 'HEAD',
      credentials: 'omit',
    });
    const headersToStrip = [];
    // Check blocking headers in response if visible
    const xfo = resp.headers.get('x-frame-options');
    const csp = resp.headers.get('content-security-policy');

    if (xfo) {
      headersToStrip.push('x-frame-options');
      headersToStrip.push('frame-options');
    }
    if (csp && /frame-ancestors/i.test(csp)) {
      headersToStrip.push('content-security-policy');
      headersToStrip.push('content-security-policy-report-only');
    }

    if (headersToStrip.length > 0) {
      console.log(`[CDP Bridge] Pre-flight probe detected blocking headers on '${domain}':`, headersToStrip);
      await applyDnrRuleForDomain(domain, headersToStrip);
      await reportUnlockRuleToBackend(domain, headersToStrip, ['spoof-top-hierarchy']);
    }
  } catch (err) {
    // Silent catch: pre-flight is best-effort and does not block visible loading
  }
}
// --- 3. Dual-Channel Detection: Native webNavigation Error Interception ---

if (chrome.webNavigation && chrome.webNavigation.onErrorOccurred) {
  chrome.webNavigation.onErrorOccurred.addListener(async (details) => {
    // Only target sub_frame navigations (the workspace iframe)
    if (details.frameId === 0) return;

    const error = details.error || '';
    const url = details.url || '';
    console.warn(`[CDP Bridge] Native webNavigation error on sub_frame (${details.frameId}):`, error, url);

    // net::ERR_BLOCKED_BY_RESPONSE or CSP block
    if (/ERR_BLOCKED_BY_RESPONSE|ERR_BLOCKED_BY_CLIENT|ERR_FAILED/i.test(error)) {
      try {
        const parsed = new URL(url);
        const domain = parsed.hostname.toLowerCase();
        console.log(`[CDP Bridge] Immediate native block signal for '${domain}'. Synthesizing dynamic DNR rule...`);
        const headersToStrip = ['x-frame-options', 'frame-options', 'content-security-policy', 'content-security-policy-report-only'];
        await applyDnrRuleForDomain(domain, headersToStrip);
        await reportUnlockRuleToBackend(domain, headersToStrip, ['spoof-top-hierarchy', 'strip-meta-csp']);
      } catch (e) {}
    }
  });
}
function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (!backendWsUrl) {
    console.warn('[CDP Bridge] Waiting for backend configuration before connecting...');
    return;
  }
  let targetWsUrl = backendWsUrl;
  if (activeApiToken && !targetWsUrl.includes('token=')) {
    const separator = targetWsUrl.includes('?') ? '&' : '?';
    targetWsUrl = `${targetWsUrl}${separator}token=${encodeURIComponent(activeApiToken)}`;
  }
  console.log('[CDP Bridge] Connecting to backend bridge:', backendWsUrl, '(authenticated via ephemeral session token)');
  ws = new WebSocket(targetWsUrl);
  ws.onopen = async () => {
    console.log('[CDP Bridge] Connected to backend CDP bridge!');
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    await initializeHostAndIframeSession();
  };

  ws.onclose = () => {
    console.log('[CDP Bridge] Disconnected from backend CDP bridge. Reconnecting in 3s...');
    scheduleReconnect();
  };

  ws.onerror = (err) => {
    console.warn('[CDP Bridge] WebSocket error:', err);
  };

  ws.onmessage = async (event) => {
    try {
      const data = JSON.parse(event.data);
      await handleBackendMessage(data);
    } catch (e) {
      console.error('[CDP Bridge] Failed to process incoming message:', e);
    }
  };
}

function scheduleReconnect() {
  if (!reconnectTimer) {
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectWebSocket();
    }, 3000);
  }
}

// Helper to test if a URL or target belongs to the workspace UI host tab (strict exact host match)
function isHostWorkspaceUrl(url) {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    const host = parsed.host.toLowerCase();
    if (configuredFrontendHost && host === configuredFrontendHost.toLowerCase()) return true;
    if (host === 'localhost:5173' || host === '127.0.0.1:5173') return true;
  } catch (e) {}
  return false;
}
// Universal detection of cloud storage buckets, blob endpoints, document downloads and file attachments.
// These URLs MUST NEVER navigate the workspace iframe, as doing so aborts the active job application form
// or triggers AWS S3 / GCS AccessDenied errors on private drop-box uploads.
function isDocumentOrStorageUrl(url) {
  if (!url || typeof url !== 'string') return false;
  try {
    const lower = url.toLowerCase().trim();
    if (lower.startsWith('blob:') || lower.startsWith('data:application/')) return true;

    const parsed = new URL(url);
    const hostname = parsed.hostname.toLowerCase();
    const pathname = parsed.pathname.toLowerCase();

    // 1. Cloud storage & object store domains (AWS S3, Google Cloud Storage, Azure Blob, R2, etc.)
    if (
      hostname.endsWith('.amazonaws.com') ||
      hostname.endsWith('.googleapis.com') ||
      hostname.endsWith('.blob.core.windows.net') ||
      hostname.endsWith('.r2.cloudflarestorage.com') ||
      (hostname.includes('supabase.co') && pathname.includes('/storage/')) ||
      (hostname.includes('firebase') && pathname.includes('/storage')) ||
      hostname.includes('backblazeb2.com') ||
      hostname.includes('digitaloceanspaces.com')
    ) {
      return true;
    }

    // 2. Document & archive file extensions
    const docExtensions = /\.(pdf|docx?|odt|rtf|txt|csv|xlsx?|pptx?|zip|tar|gz|rar|7z)$/i;
    if (docExtensions.test(pathname)) {
      return true;
    }

    // 3. Attachment download indicators
    if (
      parsed.searchParams.has('download') ||
      (parsed.searchParams.get('response-content-disposition') || '').includes('attachment')
    ) {
      return true;
    }

    return false;
  } catch (e) {
    return false;
  }
}

// Find host tab (workspace tab) - strictly matches the workspace tab, NEVER falls back to arbitrary active tabs
async function findHostTab() {
  const tabs = await chrome.tabs.query({});
  const appTab = tabs.find(t => t.url && isHostWorkspaceUrl(t.url));
  return appTab || null;
}
// Attach to host tab and enable auto-attach for child iframes (flat session)
async function initializeHostAndIframeSession() {
  try {
    const tab = await findHostTab();
    if (!tab) {
      console.warn('[CDP Bridge] No valid host tab found.');
      return;
    }

    hostTabId = tab.id;
    hostWindowId = tab.windowId;

    // 1. Attach debugger to host tab if not already attached
    if (!attachedHostTab) {
      await new Promise((resolve) => {
        chrome.debugger.attach({ tabId: hostTabId }, '1.3', () => {
          const err = chrome.runtime.lastError;
          if (err && !err.message.includes('already attached')) {
            console.warn(`[CDP Bridge] Failed to attach to host tab ${hostTabId}:`, err.message);
            attachedHostTab = false;
            resolve(false);
          } else {
            console.log(`[CDP Bridge] Debugger attached to host tab ${hostTabId}`);
            attachedHostTab = true;
            resolve(true);
          }
        });
      });
    }

    if (!attachedHostTab) return;

    // Send Runtime.enable and Target.setAutoAttach asynchronously without blocking flow
    chrome.debugger.sendCommand({ tabId: Number(hostTabId) }, 'Runtime.enable', {}, () => {});
    chrome.debugger.sendCommand({ tabId: Number(hostTabId) }, 'Target.setAutoAttach', {
      autoAttach: true,
      waitForDebuggerOnStart: false,
      flatten: true
    }, (res) => {
      const err = chrome.runtime.lastError;
      if (err) console.warn('[CDP Bridge] Target.setAutoAttach warning:', err.message);
      else console.log('[CDP Bridge] Target.setAutoAttach active:', res);
    });

    // Notify backend about host tab immediately
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'tab_info',
        tabId: hostTabId,
        targetId: `tab-${hostTabId}`,
        url: tab.url,
        title: tab.title,
      }));
    }

    // Start continuous discovery for the iframe target
    startContinuousIframeDiscovery();
  } catch (err) {
    console.error('[CDP Bridge] Error initializing session:', err);
  }
}

function startContinuousIframeDiscovery() {
  if (discoveryInterval) clearInterval(discoveryInterval);

  const discover = () => {
    if (!attachedHostTab || !hostTabId || iframeSessionId) {
      if (iframeSessionId && discoveryInterval) {
        clearInterval(discoveryInterval);
        discoveryInterval = null;
      }
      return;
    }

    // 1. Direct discovery via chrome.debugger.getTargets (Runs immediately, never blocked by DOM)
    chrome.debugger.getTargets((targets) => {
      if (!targets || targets.length === 0 || iframeSessionId) return;

      const targetIframe = targets.find(t => {
        // Exclude the workspace tab itself
        if (t.id === `tab-${hostTabId}` || t.id === String(hostTabId)) return false;
        // Exclude external standalone tabs
        if (t.tabId && t.tabId !== hostTabId) return false;
        // Exclude internal extensions and UI
        if (t.url && (isHostWorkspaceUrl(t.url) || t.url.startsWith('chrome-extension://'))) return false;

        // An iframe target MUST have either an HTTP url OR a title containing an HTTP URL
        const hasHttp = (t.url && t.url.startsWith('http')) || (t.title && t.title.startsWith('http'));
        if (!hasHttp) return false;

        return t.type === 'iframe' || t.type === 'other' || t.type === 'page';
      });

      if (targetIframe && !iframeSessionId) {
        console.log('[CDP Bridge] Candidate target detected for the iframe, requesting attachment via Target.attachToTarget:', targetIframe);
        // Request flat session attachment directly through host debugger
        chrome.debugger.sendCommand({ tabId: Number(hostTabId) }, 'Target.attachToTarget', {
          targetId: targetIframe.id,
          flatten: true
        }, (res) => {
          const err = chrome.runtime.lastError;
          if (!err && res && res.sessionId) {
            iframeSessionId = res.sessionId;
            iframeTargetInfo = {
              targetId: targetIframe.id,
              url: targetIframe.url || targetIframe.title || 'Workspace Target',
              title: targetIframe.title || 'Workspace Target',
              type: 'iframe',
            };
            console.log(`[CDP Bridge] Iframe attached successfully with real Chrome sessionId: ${iframeSessionId}`);
            notifyBackendIframeInfo();
            if (discoveryInterval) {
              clearInterval(discoveryInterval);
              discoveryInterval = null;
            }
          }
        });
      }
    });

    // 2. Non-blocking measure of iframe bounding box in parallel for screenshot fallback
    chrome.debugger.sendCommand({ tabId: Number(hostTabId) }, 'Runtime.evaluate', {
      expression: '(() => { const iframe = document.querySelector("iframe"); if (!iframe) return null; const r = iframe.getBoundingClientRect(); return { src: iframe.src, x: r.x, y: r.y, width: r.width, height: r.height }; })()',
      returnByValue: true
    }, (domResult) => {
      const domIframe = domResult && domResult.result && domResult.result.value;
      if (domIframe && domIframe.width > 0 && domIframe.height > 0) {
        cachedIframeRect = {
          x: domIframe.x,
          y: domIframe.y,
          width: domIframe.width,
          height: domIframe.height,
        };
      }
    });
  };

  discover();
  discoveryInterval = setInterval(discover, 1000);
}

function notifyBackendIframeInfo() {
  if (ws && ws.readyState === WebSocket.OPEN && iframeSessionId && iframeTargetInfo) {
    ws.send(JSON.stringify({
      type: 'iframe_info',
      sessionId: iframeSessionId,
      targetId: iframeTargetInfo.targetId,
      url: iframeTargetInfo.url,
      title: iframeTargetInfo.title,
    }));
  }
}

// Handle incoming CDP command from backend
async function handleBackendMessage(message) {
  if (message.type === 'ping' || message.type === 'pong') {
    if (message.type === 'ping' && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'pong' }));
    }
    return;
  }

  // Ignore messages that are not valid CDP commands
  if (!message.method) {
    console.log('[CDP Bridge] Received non-CDP message from backend, ignoring:', message);
    return;
  }
  if (!attachedHostTab || !hostTabId) {
    await initializeHostAndIframeSession();
  }

  if (!attachedHostTab || !hostTabId) {
    if (message.id !== undefined && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        id: message.id,
        error: { code: -32000, message: 'Host tab not attached in Chrome debugger' }
      }));
    }
    return;
  }
  const method = message.method;
  const params = (message.params && typeof message.params === 'object') ? message.params : {};
  const msgId = message.id;
  const sessionId = message.sessionId;
  console.log('[CDP Bridge] Received command from backend:', method, 'id:', msgId, 'params:', params);
  // Handle Page.navigate: navigate strictly inside the iframe without touching host tab
  if (method === 'Page.navigate') {
    const navUrl = params.url;
    console.log('[CDP Bridge] Navigating inside iframe to:', navUrl);

    // CRITICAL RECURSION GUARD: Prohibit navigating workspace iframe to host application origin
    if (isHostWorkspaceUrl(navUrl)) {
      console.warn('[CDP Bridge] Navigation to host workspace URL blocked (recursion prevention):', navUrl);
      sendResponse(msgId, sessionId, { message: 'Navigation to host workspace application URL is prohibited.' }, null);
      return;
    }

    // Run background pre-flight probe silently to synthesize DNR rules BEFORE first iframe render
    preflightProbeForUrl(navUrl).catch(() => {});
    // 1. Notify frontend to update address bar & iframe src
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'target_navigated',
        url: navUrl,
        title: 'Workspace Target'
      }));
    }
    // 2. If child session is attached, attempt navigation
    const targetSession = iframeSessionId || sessionId;
    if (targetSession) {
      chrome.debugger.sendCommand({ tabId: Number(hostTabId), sessionId: targetSession }, 'Page.navigate', params, (result) => {
        const err = chrome.runtime.lastError;
        if (err && err.message && err.message.includes("wasn't found")) {
          // Chromium OOPIF subframe sessions do not support Page.navigate.
          // Fall back to navigating via window.location.href in the child session.
          console.log('[CDP Bridge] Page.navigate not supported on iframe target; falling back to window.location.href');
          chrome.debugger.sendCommand(
            { tabId: Number(hostTabId), sessionId: targetSession },
            'Runtime.evaluate',
            { expression: `window.location.href = ${JSON.stringify(navUrl)};`, userGesture: true },
            () => {
              sendResponse(msgId, sessionId, null, { frameId: 'iframe-main', loaderId: 'loader-1' });
            }
          );
          return;
        }
        sendResponse(msgId, sessionId, err, result || { frameId: 'iframe-main', loaderId: 'loader-1' });
      });
      return;
    }
    sendResponse(msgId, sessionId, null, { frameId: 'iframe-main' });
    return;
  }
  // Handle Page.captureScreenshot: Hybrid capture architecture
  // 1. If host tab is active and focused, use zero-blink chrome.tabs.captureVisibleTab with canvas cropping
  // 2. If tab is in background or captureVisibleTab fails, fall back gracefully to CDP Page.captureScreenshot
  if (method === 'Page.captureScreenshot') {
    handleHybridCaptureScreenshot(msgId, sessionId);
    return;
  }

  // Construct debuggee: in Chromium Flat Sessions, route to the attached child iframe session!
  const targetSession = sessionId || iframeSessionId;
  const numericTabId = Number(hostTabId);
  const debuggee = targetSession
    ? { tabId: numericTabId, sessionId: targetSession }
    : { tabId: numericTabId };

  const sanitizedParams = (params && typeof params === 'object') ? params : {};

  try {
    chrome.debugger.sendCommand(debuggee, method, sanitizedParams, (result) => {
      let err = chrome.runtime.lastError;
      sendResponse(msgId, targetSession, err, result);
    });
  } catch (cmdErr) {
    console.warn(`[CDP Bridge] sendCommand threw for ${method}:`, cmdErr);
    sendResponse(msgId, targetSession, { message: cmdErr.message }, null);
  }
}
function sendResponse(msgId, sessionId, err, result, meta) {
  if (ws && ws.readyState === WebSocket.OPEN && msgId !== undefined) {
    const resp = { id: msgId };
    if (sessionId) resp.sessionId = sessionId;

    if (err) {
      resp.error = { code: -32000, message: err.message };
    } else {
      resp.result = result || {};
    }
    if (meta) {
      resp._meta = meta;
    }
    ws.send(JSON.stringify(resp));
  }
}
/**
 * Executes hybrid screenshot capture:
 * 1. Checks if the target host tab is active and visible in the focused window.
 * 2. If active, uses zero-blink chrome.tabs.captureVisibleTab with canvas cropping.
 * 3. If inactive or on any error, seamlessly falls back to CDP Page.captureScreenshot.
 */
async function handleHybridCaptureScreenshot(msgId, sessionId) {
  const numericTabId = Number(hostTabId);
  if (!numericTabId) {
    fallbackCdpCaptureScreenshot(msgId, sessionId);
    return;
  }

  try {
    // Inspect tab state: check active status and window focus
    const tab = await chrome.tabs.get(numericTabId);
    let isTabVisible = !!(tab && tab.active);

    if (isTabVisible && tab.windowId !== undefined) {
      try {
        const win = await chrome.windows.get(tab.windowId);
        // Window must be focused or normal (not minimized)
        if (win && win.state === 'minimized') {
          isTabVisible = false;
        }
      } catch (winErr) {
        // Non-blocking: proceed with tab.active if window inspect fails
      }
    }

    if (!isTabVisible) {
      console.log('[Screenshot] Tab is inactive or minimized, using CDP Page.captureScreenshot fallback.');
      fallbackCdpCaptureScreenshot(msgId, sessionId);
      return;
    }

    // Measure iframe bounding rect in parent page for zero-blink cropping
    chrome.debugger.sendCommand({ tabId: numericTabId }, 'Runtime.evaluate', {
      expression: '(() => { const iframe = document.querySelector("iframe"); if (!iframe) return null; const r = iframe.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, dpr: window.devicePixelRatio || 1 }; })()',
      returnByValue: true
    }, async (evalResult) => {
      const rect = evalResult && evalResult.result && evalResult.result.value;
      const targetRect = (rect && rect.width > 0 && rect.height > 0)
        ? rect
        : (cachedIframeRect && cachedIframeRect.width > 0 ? { ...cachedIframeRect, dpr: 1 } : null);

      try {
        // Zero-blink capture using native compositor frame buffer
        const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
          format: 'jpeg',
          quality: 80
        });

        if (!dataUrl) {
          throw new Error('captureVisibleTab returned empty data');
        }

        // If no iframe crop required or possible, return full image data
        if (!targetRect) {
          console.log('[Screenshot] Zero-blink capture succeeded (full tab, no crop).');
          const base64Data = dataUrl.replace(/^data:image\/[a-z]+;base64,/, '');
          sendResponse(msgId, sessionId, null, { data: base64Data }, {
            capture_method: 'captureVisibleTab',
            zero_blink: true,
            cropped: false,
          });
          return;
        }

        // Crop screenshot strictly to iframe dimensions using OffscreenCanvas
        const croppedBase64 = await cropImageDataUrl(dataUrl, targetRect);
        console.log('[Screenshot] Zero-blink capture succeeded with iframe crop.');
        sendResponse(msgId, sessionId, null, { data: croppedBase64 }, {
          capture_method: 'captureVisibleTab',
          zero_blink: true,
          cropped: true,
          rect: targetRect,
        });
      } catch (captureErr) {
        console.warn('[Screenshot] Zero-blink captureVisibleTab failed, falling back to CDP:', captureErr.message);
        fallbackCdpCaptureScreenshot(msgId, sessionId, `captureVisibleTab error: ${captureErr.message}`);
      }
    });
  } catch (err) {
    console.warn('[Screenshot] Error inspecting tab state, falling back to CDP:', err.message);
    fallbackCdpCaptureScreenshot(msgId, sessionId, `tab state error: ${err.message}`);
  }
}

/**
 * Crops a base64 data URL using OffscreenCanvas to match iframe bounding rect.
 */
async function cropImageDataUrl(dataUrl, rect) {
  const response = await fetch(dataUrl);
  const blob = await response.blob();
  const imageBitmap = await createImageBitmap(blob);

  const dpr = rect.dpr || 1;
  const sx = Math.max(0, Math.round(rect.x * dpr));
  const sy = Math.max(0, Math.round(rect.y * dpr));
  const sw = Math.min(imageBitmap.width - sx, Math.round(rect.width * dpr));
  const sh = Math.min(imageBitmap.height - sy, Math.round(rect.height * dpr));

  if (sw <= 0 || sh <= 0) {
    throw new Error('Invalid crop dimensions');
  }

  const canvas = new OffscreenCanvas(sw, sh);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(imageBitmap, sx, sy, sw, sh, 0, 0, sw, sh);

  const croppedBlob = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.8 });
  const arrayBuffer = await croppedBlob.arrayBuffer();
  const bytes = new Uint8Array(arrayBuffer);
  let binary = '';
  const len = bytes.byteLength;
  for (let i = 0; i < len; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Fallback CDP Page.captureScreenshot implementation.
 * Used when tab is in background or captureVisibleTab is unavailable.
 */
function fallbackCdpCaptureScreenshot(msgId, sessionId, reason = 'tab_in_background') {
  const numericTabId = Number(hostTabId);
  chrome.debugger.sendCommand({ tabId: numericTabId }, 'Runtime.evaluate', {
    expression: '(() => { const iframe = document.querySelector("iframe"); if (!iframe) return null; const r = iframe.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; })()',
    returnByValue: true
  }, (evalResult) => {
    const rect = evalResult && evalResult.result && evalResult.result.value;
    const screenshotParams = {
      format: 'jpeg',
      quality: 80,
      optimizeForSpeed: true,
      captureBeyondViewport: false,
    };

    if (rect && rect.width > 0 && rect.height > 0) {
      screenshotParams.clip = {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        scale: 1,
      };
      console.log('[CDP Bridge] Capturing iframe with dynamic Skia clip (fallback):', screenshotParams.clip);
    } else if (cachedIframeRect && cachedIframeRect.width > 0 && cachedIframeRect.height > 0) {
      screenshotParams.clip = {
        x: Math.round(cachedIframeRect.x),
        y: Math.round(cachedIframeRect.y),
        width: Math.round(cachedIframeRect.width),
        height: Math.round(cachedIframeRect.height),
        scale: 1,
      };
    }

    chrome.debugger.sendCommand({ tabId: numericTabId }, 'Page.captureScreenshot', screenshotParams, (result) => {
      const err = chrome.runtime.lastError;
      sendResponse(msgId, sessionId, err, result, {
        capture_method: 'Page.captureScreenshot',
        zero_blink: false,
        reason: reason,
        clip: screenshotParams.clip || null,
      });
    });
  });
}

// Listen for CDP events from Chrome and route to backend
chrome.debugger.onEvent.addListener((source, method, params) => {
  // 1. Detect iframe child session attachment
  if (method === 'Target.attachedToTarget') {
    const { sessionId, targetInfo } = params;
    console.log('[CDP Bridge Event] Target attached:', sessionId, targetInfo);
    const isHostTab = targetInfo && (targetInfo.targetId === String(hostTabId) || targetInfo.targetId === `tab-${hostTabId}` || isHostWorkspaceUrl(targetInfo.url));
    // CRITICAL: Filter out ServiceWorkers, SharedWorkers and web workers so they NEVER hijack
    // the target session. Workers do not have a DOM, which causes DOMSnapshot and DOM calls to fail.
    const isWorker = targetInfo && (
      targetInfo.type === 'service_worker' ||
      targetInfo.type === 'shared_worker' ||
      targetInfo.type === 'worker' ||
      (targetInfo.url && (targetInfo.url.endsWith('.js') || targetInfo.url.includes('/sw.js') || targetInfo.url.includes('worker.js')))
    );

    const isCandidate = !isHostTab && !isWorker && (
      targetInfo.type === 'iframe' ||
      targetInfo.type === 'other' ||
      targetInfo.type === 'page'
    ) && (targetInfo.url ? !isHostWorkspaceUrl(targetInfo.url) : true);

    if (isCandidate) {
      if (!iframeSessionId) {
        // Root workspace iframe session
        iframeSessionId = sessionId;
        iframeTargetInfo = targetInfo;
        console.log('[CDP Bridge] Successfully bound to root workspace iframe session:', iframeSessionId, iframeTargetInfo);
        notifyBackendIframeInfo();

        // Enable Network and Page on the root iframe session to ensure cookies and security policies are active
        try {
          chrome.debugger.sendCommand({ tabId: Number(hostTabId), sessionId: iframeSessionId }, 'Network.enable', {}, () => {});
          chrome.debugger.sendCommand({ tabId: Number(hostTabId), sessionId: iframeSessionId }, 'Page.enable', {}, () => {});
        } catch (e) {}
      } else if (sessionId !== iframeSessionId) {
        // Child sub-iframe session (e.g. Ashby, Greenhouse, Lever, hCaptcha, Turnstile)
        console.log('[CDP Bridge] Registering child sub-iframe session:', sessionId, targetInfo);
        childIframeSessions.set(sessionId, targetInfo);

        try {
          chrome.debugger.sendCommand({ tabId: Number(hostTabId), sessionId }, 'Network.enable', {}, () => {});
          chrome.debugger.sendCommand({ tabId: Number(hostTabId), sessionId }, 'Page.enable', {}, () => {});
        } catch (e) {}

        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            method: 'Target.attachedToTarget',
            params: {
              sessionId: sessionId,
              targetInfo: targetInfo,
              waitingForDebugger: false
            },
            sessionId: sessionId,
            tabId: source.tabId
          }));
        }
      }
    }
    return;
  }

  // 1b. Track target info updates (e.g. child iframe navigating from about:blank to real URL)
  if (method === 'Target.targetInfoChanged') {
    const { targetInfo } = params || {};
    if (targetInfo) {
      if (iframeTargetInfo && iframeTargetInfo.targetId === targetInfo.targetId) {
        iframeTargetInfo = { ...iframeTargetInfo, ...targetInfo };
      }
      for (const [sId, tInfo] of childIframeSessions.entries()) {
        if (tInfo && tInfo.targetId === targetInfo.targetId) {
          childIframeSessions.set(sId, { ...tInfo, ...targetInfo });
          break;
        }
      }
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          method: 'Target.targetInfoChanged',
          params: params,
          sessionId: source.sessionId || iframeSessionId,
          tabId: source.tabId
        }));
      }
    }
    return;
  }

  if (method === 'Target.detachedFromTarget') {
    const { targetId } = params || {};
    if (iframeTargetInfo && targetId === iframeTargetInfo.targetId) {
      console.log('[CDP Bridge] Active root iframe detached:', targetId);
      iframeSessionId = null;
      iframeTargetInfo = null;
      childIframeSessions.clear();
      startContinuousIframeDiscovery();
    } else if (targetId) {
      for (const [sId, tInfo] of childIframeSessions.entries()) {
        if (tInfo && tInfo.targetId === targetId) {
          console.log('[CDP Bridge] Child sub-iframe detached:', targetId, sId);
          childIframeSessions.delete(sId);
          break;
        }
      }
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        method: 'Target.detachedFromTarget',
        params: params,
        sessionId: source.sessionId || iframeSessionId,
        tabId: source.tabId
      }));
    }
    return;
  }

  // 2. Intercept raw HTTP response headers to auto-upgrade Set-Cookie headers for cross-site iframe persistence
  if (method === 'Network.responseReceivedExtraInfo' && params && params.headers) {
    try {
      const headers = params.headers;
      // Headers can have lowercase or mixed-case 'Set-Cookie' / 'set-cookie'
      for (const [key, rawVal] of Object.entries(headers)) {
        if (key.toLowerCase() === 'set-cookie') {
          const cookieLines = Array.isArray(rawVal) ? rawVal : String(rawVal).split('\n');
          for (const line of cookieLines) {
            const parts = line.split(';');
            const firstPart = parts[0] || '';
            const eqIdx = firstPart.indexOf('=');
            if (eqIdx > 0) {
              const name = firstPart.slice(0, eqIdx).trim();
              const value = firstPart.slice(eqIdx + 1).trim();
              let domain = '';
              let path = '/';
              let httpOnly = false;
              for (let i = 1; i < parts.length; i++) {
                const p = parts[i].trim();
                if (p.toLowerCase().startsWith('domain=')) {
                  domain = p.slice(7).trim();
                } else if (p.toLowerCase().startsWith('path=')) {
                  path = p.slice(5).trim();
                } else if (p.toLowerCase() === 'httponly') {
                  httpOnly = true;
                }
              }
              let cleanDomain = (domain || '').replace(/^\./, '').trim();
              if (!cleanDomain && iframeTargetInfo && iframeTargetInfo.url) {
                try { cleanDomain = new URL(iframeTargetInfo.url).hostname; } catch (e) {}
              }
              const targetUrl = `https://${cleanDomain}`;
              // For public suffixes like .herokuapp.com, specifying domain can cause Chrome to reject the cookie.
              // Omitting domain and supplying the full HTTPS url creates an exact host-only cookie that Chrome always accepts.
              const isBarePublicSuffix = KNOWN_PUBLIC_SUFFIXES.some(s => cleanDomain === s || domain === s || domain === '.' + s);
              const setCookieParams = {
                name: name,
                value: value,
                url: targetUrl,
                path: path || '/',
                secure: true,
                sameSite: 'None',
                httpOnly: httpOnly,
              };
              // If the server explicitly set a domain and it's not a bare public suffix (e.g. .reddit.com),
              // preserve the domain attribute with leading dot so all subdomains (www, gql, oauth) share the cookie!
              if (domain && !isBarePublicSuffix) {
                setCookieParams.domain = cleanDomain.startsWith('.') ? cleanDomain : '.' + cleanDomain;
              }
              const currentTargetTab = hostTabId ? Number(hostTabId) : null;
              if (currentTargetTab) {
                chrome.debugger.sendCommand(
                  { tabId: currentTargetTab, sessionId: source.sessionId || iframeSessionId || undefined },
                  'Network.setCookie',
                  setCookieParams,
                  (res) => {
                    const err = chrome.runtime.lastError;
                    if (!err && res && res.success) {
                      console.log(`[CDP Bridge] Injected SameSite=None; Secure cookie '${name}' via Network.setCookie`);
                    }
                  }
                );
              }
            }
          }
        }
      }
    } catch (e) {
      console.warn('[CDP Bridge] Error parsing headers in responseReceivedExtraInfo:', e);
    }
  }



  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      method: method,
      params: params,
      sessionId: source.sessionId || iframeSessionId,
      tabId: source.tabId,
    }));
  }
});

// Handle detach
chrome.debugger.onDetach.addListener((source, reason) => {
  console.log('[CDP Bridge] Debugger detached:', source, reason);
  if (source.tabId === hostTabId) {
    attachedHostTab = false;
    hostTabId = null;
    iframeSessionId = null;
    iframeTargetInfo = null;
    childIframeSessions.clear();
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      method: 'Target.detachedFromTarget',
      params: { tabId: source.tabId, reason: reason },
    }));
  }
});

// Heartbeat to prevent service worker idling and reconnect
chrome.tabs.onActivated.addListener(() => {
  if (!ws || ws.readyState !== WebSocket.OPEN) connectWebSocket();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'complete' && (!ws || ws.readyState !== WebSocket.OPEN)) {
    connectWebSocket();
  }
});

setInterval(() => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    connectWebSocket();
  } else {
    ws.send(JSON.stringify({ type: 'ping' }));
  }
}, 5000);

// Listen for dynamic configuration updates from frontend content script
// Listen for messages from content scripts (dynamic config or iframe URL changes)
globalThis.isAuthorizedSender = function(sender, message) {
  // CRITICAL SECURITY GUARD: Validate ONLY immutable, browser-enforced properties from the Chromium runtime API (sender.origin or sender.url).
  // NEVER inspect message.origin, which is attacker-controlled in the message payload!
  const origin = (sender && sender.origin) || (sender && sender.url) || '';
  try {
    const parsed = new URL(origin);
    const host = parsed.host.toLowerCase();
    const hostname = parsed.hostname.toLowerCase();
    if (configuredFrontendHost && host === configuredFrontendHost.toLowerCase()) return true;
    if (hostname === 'localhost' || hostname === '127.0.0.1') return true;
  } catch (e) {}
  return false;
}

chrome.runtime.onMessage.addListener((message, sender, _sendResponse) => {
  if (!message || typeof message !== 'object') return;

  if (message.type === 'IFRAME_URL_CHANGED') {
    // CRITICAL: Prevent cross-tab leakage!
    // Ignore iframe navigation events from any tab other than the active workspace host tab.
    if (!sender.tab || !hostTabId || Number(sender.tab.id) !== Number(hostTabId)) {
      return;
    }
    const liveUrl = message.url;
    const liveTitle = message.title || '';
    if (!liveUrl || liveUrl.startsWith('about:')) {
      console.log('[CDP Bridge] Ignoring blank/uninitialized iframe URL change:', liveUrl);
      return;
    }
    console.log('[CDP Bridge] Live iframe navigation detected:', liveUrl, liveTitle);
    if (iframeTargetInfo) {
      iframeTargetInfo.url = liveUrl;
      if (liveTitle) iframeTargetInfo.title = liveTitle;
    }
    // If we have an active session, ensure backend has confirmed iframe_info
    if (iframeSessionId && iframeTargetInfo) {
      notifyBackendIframeInfo();
    } else {
      // Re-trigger discovery if sessionId was dropped or not yet attached
      startContinuousIframeDiscovery();
    }

    // Run silent pre-flight probe for dynamic iframe navigations
    preflightProbeForUrl(liveUrl).catch(() => {});

    // Forward immediately to backend for independent logging and state tracking
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'iframe_navigated',
        url: liveUrl,
        title: liveTitle,
        sessionId: iframeSessionId || null,
        timestamp: Date.now(),
      }));
    }
    return;
  }
  if (message.type === 'FRAME_RENDER_FAILURE') {
    // Ignore render failure reports from outside the host workspace tab
    if (!sender.tab || !hostTabId || Number(sender.tab.id) !== Number(hostTabId)) {
      return;
    }
    const failedUrl = message.url || '';
    const failureMode = message.mode || 'unknown';
    console.warn(`[CDP Bridge] Received FRAME_RENDER_FAILURE (${failureMode}) on:`, failedUrl);

    try {
      const parsed = new URL(failedUrl);
      const domain = parsed.hostname.toLowerCase();
      // Report failure to backend to flip status to stale_relearning
      if (backendWsUrl && activeApiToken) {
        const parsedWs = new URL(backendWsUrl);
        const protocol = parsedWs.protocol === 'wss:' ? 'https:' : 'http:';
        const httpBase = `${protocol}//${parsedWs.host}`;
        fetch(`${httpBase}/api/v1/unlock-rules/${encodeURIComponent(domain)}/report-failure?token=${encodeURIComponent(activeApiToken)}`, {
          method: 'POST',
        }).catch(() => {});
      }
    } catch (e) {}
    return;
  }

  if (message.type === 'COBROWSE_SESSION_TOKEN' && message.sessionToken) {
    if (!isAuthorizedSender(sender, message)) {
      console.warn('[CDP Bridge Security] Blocked COBROWSE_SESSION_TOKEN from unauthorized sender/origin:', sender, message && message.origin);
      return;
    }
    const newSessionToken = message.sessionToken.trim();
    if (newSessionToken && newSessionToken !== activeApiToken) {
      activeApiToken = newSessionToken;
      globalThis.activeApiToken = newSessionToken;
      if (ws && ws.readyState === WebSocket.OPEN) {
        // Reconnect with new session token
        ws.close();
      } else {
        connectWebSocket();
      }
      fetchUnlockRules();
    }
    return;
  }

  if (message.type === 'COBROWSE_INIT_CONFIG') {
    if (!isAuthorizedSender(sender, message)) {
      console.warn('[CDP Bridge Security] Blocked COBROWSE_INIT_CONFIG from unauthorized sender/origin:', sender, message && message.origin);
      return;
    }
    if (message.backendWsUrl && message.backendWsUrl !== backendWsUrl) {
      backendWsUrl = message.backendWsUrl;
      if (message.frontendUrl) {
        configuredFrontendUrl = message.frontendUrl;
        try {
          const parsed = new URL(configuredFrontendUrl);
          configuredFrontendHost = parsed.host;
        } catch {}
      }
      try {
        // Save ONLY endpoint coordinates, NEVER tokens
        chrome.storage.local.set({
          cobrowse_backend_ws_url: backendWsUrl,
          cobrowse_frontend_url: configuredFrontendUrl,
          cobrowse_frontend_host: configuredFrontendHost,
        });
        // Purge any legacy token from local storage
        chrome.storage.local.remove(['cobrowse_api_token']);
      } catch {}
      console.log('[CDP Bridge] Config dynamically received from frontend page:', {
        backendWsUrl,
        configuredFrontendHost,
        hasSessionToken: !!activeApiToken,
      });
      if (activeApiToken && (!ws || ws.readyState !== WebSocket.OPEN)) {
        connectWebSocket();
      }
    }
  }
});

// Load any previously saved dynamic config on service worker start (NO tokens restored from storage)
try {
  chrome.storage.local.get(
    ['cobrowse_backend_ws_url', 'cobrowse_frontend_url', 'cobrowse_frontend_host'],
    (items) => {
      if (items && items.cobrowse_backend_ws_url) {
        backendWsUrl = items.cobrowse_backend_ws_url;
        configuredFrontendUrl = items.cobrowse_frontend_url;
        configuredFrontendHost = items.cobrowse_frontend_host;
        console.log('[CDP Bridge] Restored endpoint coordinates from chrome.storage (waiting for in-memory session token):', {
          backendWsUrl,
          configuredFrontendHost,
        });
      }
    }
  );
} catch {}
