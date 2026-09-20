// 0. Detect Web-Frame dynamic configuration from authorized frontend page and transmit to background
// Strict Origin validation: NEVER accept postMessage from unauthorized third-party origins!
const ALLOWED_ORIGIN_HOSTS = new Set([
  'localhost:5173',
  '127.0.0.1:5173',
  'localhost:3000',
  '127.0.0.1:3000',
]);

function isAuthorizedOrigin(origin) {
  if (!origin) return false;
  try {
    const parsed = new URL(origin);
    if (ALLOWED_ORIGIN_HOSTS.has(parsed.host)) return true;
    if (parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1') return true;
  } catch (e) {}
  return false;
}

function detectAndRelayConfig() {
  try {
    if (!isAuthorizedOrigin(window.location.origin)) {
      return;
    }
    const wsUrl = document.documentElement.getAttribute('data-cobrowse-ws');
    const feUrl = document.documentElement.getAttribute('data-cobrowse-frontend');
    if (wsUrl) {
      chrome.runtime.sendMessage({
        type: 'COBROWSE_INIT_CONFIG',
        backendWsUrl: wsUrl,
        frontendUrl: feUrl,
        origin: window.location.origin,
      });
    }
  } catch {}
}

window.addEventListener('message', (event) => {
  // CRITICAL SECURITY GUARD: Block any forged postMessage from third-party sites!
  if (!isAuthorizedOrigin(event.origin)) {
    return;
  }

  if (event.data && event.data.type === 'COBROWSE_SESSION_TOKEN' && event.data.sessionToken) {
    try {
      chrome.runtime.sendMessage({
        type: 'COBROWSE_SESSION_TOKEN',
        sessionToken: event.data.sessionToken,
        origin: event.origin,
      });
    } catch {}
  }
  if (event.data && event.data.type === 'COBROWSE_CONFIG_AVAILABLE' && event.data.backendWsUrl) {
    try {
      chrome.runtime.sendMessage({
        type: 'COBROWSE_INIT_CONFIG',
        backendWsUrl: event.data.backendWsUrl,
        frontendUrl: event.data.frontendUrl,
        origin: event.origin,
      });
    } catch {}
  }
});
detectAndRelayConfig();
document.addEventListener('DOMContentLoaded', detectAndRelayConfig);
window.addEventListener('load', detectAndRelayConfig);
// Content script injected into all frames to keep navigations inside the workspace iframe

// 1. Override window.open to redirect within the same frame
try {
  const originalOpen = window.open;
  window.open = function(url, target, features) {
    if (url) {
      window.location.href = url;
      return window;
    }
    return originalOpen ? originalOpen.apply(window, arguments) : window;
  };
} catch (e) {
  // Ignore if window is frozen
}
// Unlock unpartitioned cookies for cross-origin workspace iframe via W3C Storage Access API
async function requestIframeStorageAccess() {
  try {
    if (document.requestStorageAccess) {
      const hasAccess = document.hasStorageAccess ? await document.hasStorageAccess() : false;
      if (!hasAccess) {
        await document.requestStorageAccess();
        console.log('[CDP Bridge] Storage access granted for embedded frame:', window.location.hostname);
      }
    }
  } catch (e) {}
}

document.addEventListener('click', requestIframeStorageAccess, { capture: true, passive: true });
document.addEventListener('pointerdown', requestIframeStorageAccess, { capture: true, passive: true });
document.addEventListener('keydown', requestIframeStorageAccess, { capture: true, passive: true });
if (document.hasStorageAccess) {
  document.hasStorageAccess().then((has) => {
    if (!has && document.requestStorageAccess) {
      document.requestStorageAccess().catch(() => {});
    }
  }).catch(() => {});
}

// 2. Intercept link clicks with target="_blank" or external windows and force in-frame navigation
// EXCEPT for OAuth authentication portals (Google, Apple, Microsoft, GitHub) which forbid iframes.
function isAuthPortalUrl(url) {
  if (!url || typeof url !== 'string') return false;
  const lower = url.toLowerCase();
  return (
    lower.includes('accounts.google.com') ||
    lower.includes('appleid.apple.com') ||
    lower.includes('login.microsoftonline.com') ||
    lower.includes('github.com/login/oauth') ||
    (lower.includes('facebook.com/v') && lower.includes('/dialog/oauth'))
  );
}

function forceSameFrameNavigation(event) {
  try {
    const anchor = event.target && event.target.closest ? event.target.closest('a') : null;
    if (anchor) {
      const href = anchor.href || anchor.getAttribute('href') || '';
      if (isAuthPortalUrl(href)) {
        // Allow OAuth login portals to open in native popup/tab
        return;
      }
      if (anchor.target && anchor.target.toLowerCase() !== '_self') {
        anchor.target = '_self';
      }
      // Only intervene if anchor has target="_blank" and a real external URL
      if (href && anchor.target === '_self' && !href.startsWith('javascript:') && !href.startsWith('#')) {
        // Let standard browser click proceed naturally inside _self
      }
    }
  } catch (e) {}
}

document.addEventListener('click', forceSameFrameNavigation, true);
document.addEventListener('auxclick', forceSameFrameNavigation, true);
// 3. Intercept form submissions with target="_blank"
document.addEventListener('submit', (event) => {
  try {
    if (event.target && event.target.target && event.target.target.toLowerCase() !== '_self') {
      event.target.target = '_self';
    }
  } catch (e) {}
}, true);

// 4. Notify parent window and background service worker of genuine navigation events.
// CRITICAL: Only the DIRECT child iframe of the workspace tab (window.parent === window.top)
// represents the workspace target. Nested sub-frames (e.g. Google widgets, ads, analytics)
// MUST NOT emit top-level navigation updates to avoid overriding the user's active target.
function notifyParentOfNavigation() {
  try {
    const isDirectChild = window !== window.top && window.parent === window.top;
    if (isDirectChild && window.location && window.location.href) {
      const liveUrl = window.location.href;
      if (!liveUrl || liveUrl.startsWith('about:')) return;
      const liveTitle = document.title || '';
      // Post message to parent frontend window
      window.parent.postMessage({
        type: 'COBROWSE_IFRAME_NAVIGATED',
        url: liveUrl,
        title: liveTitle
      }, '*');

      // Send message to extension background worker for direct backend logging
      chrome.runtime.sendMessage({
        type: 'IFRAME_URL_CHANGED',
        url: liveUrl,
        title: liveTitle
      }).catch(() => {});
    }
  } catch (e) {}
}
window.addEventListener('DOMContentLoaded', notifyParentOfNavigation);
window.addEventListener('load', notifyParentOfNavigation);
window.addEventListener('popstate', notifyParentOfNavigation);
window.addEventListener('hashchange', notifyParentOfNavigation);
notifyParentOfNavigation();
// 5. Heuristic DOM & Framebusting Watchdog
// Detects empty body or instant redirection loops in the iframe and reports to background
if (window !== window.top) {
  setTimeout(() => {
    try {
      if (document.body && document.body.children.length === 0 && !document.body.innerText.trim()) {
        console.warn('[CDP Bridge] Heuristic Watchdog: empty DOM body detected after 3.5s on', window.location.href);
        chrome.runtime.sendMessage({
          type: 'FRAME_RENDER_FAILURE',
          url: window.location.href,
          mode: 'empty_dom_body',
        }).catch(() => {});
      }
    } catch (e) {}
  }, 3500);

  window.addEventListener('beforeunload', () => {
    try {
      const loadTime = performance.now();
      if (loadTime < 800) {
        // Fast outgoing navigation without user interaction suggests framebusting redirection
        console.warn('[CDP Bridge] Heuristic Watchdog: fast beforeunload (<800ms) suggests framebusting on', window.location.href);
        chrome.runtime.sendMessage({
          type: 'FRAME_RENDER_FAILURE',
          url: window.location.href,
          mode: 'fast_redirect_framebusting',
        }).catch(() => {});
      }
    } catch (e) {}
  });
}
