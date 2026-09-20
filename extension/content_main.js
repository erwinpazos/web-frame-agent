// Content script injected into the MAIN world at document_start
// Modular JS Patch Library for anti-framebusting, cookie partitioning, and DOM meta-CSP stripping.

(function() {
  'use strict';

  // Named patch catalog with closed whitelist enforcement
  const PATCHES = {
    // 1. Spoof window hierarchy (AWS WAF / Cloudflare frame checks: window.top !== window.self, window.parent, window.frameElement)
    'spoof-top-hierarchy': function() {
      try {
        Object.defineProperty(window, 'top', {
          get: function() { return window; },
          set: function() {},
          configurable: true,
        });
      } catch (e) {}

      try {
        Object.defineProperty(window, 'parent', {
          get: function() { return window; },
          set: function() {},
          configurable: true,
        });
      } catch (e) {}

      try {
        Object.defineProperty(window, 'frameElement', {
          get: function() { return null; },
          set: function() {},
          configurable: true,
        });
      } catch (e) {}
    },

    // 2. Hide automated browser flag
    'hide-webdriver': function() {
      try {
        if (navigator.webdriver) {
          Object.defineProperty(navigator, 'webdriver', {
            get: function() { return undefined; },
            configurable: true,
          });
        }
      } catch (e) {}
    },

    // 3. Hook document.cookie to force SameSite=None; Secure; Partitioned on all written cookies
    'rewrite-cookies-chips': function() {
      try {
        const proto = Document.prototype;
        const cookieDesc = Object.getOwnPropertyDescriptor(proto, 'cookie') ||
                           Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'cookie');

        if (cookieDesc && cookieDesc.set) {
          const originalSet = cookieDesc.set;
          const originalGet = cookieDesc.get;

          Object.defineProperty(document, 'cookie', {
            get: function() {
              return originalGet.call(this);
            },
            set: function(val) {
              if (typeof val === 'string') {
                if (/SameSite=(Lax|Strict)/i.test(val)) {
                  val = val.replace(/SameSite=(Lax|Strict)/gi, 'SameSite=None');
                  if (!/Secure/i.test(val)) val += '; Secure';
                  if (!/Partitioned/i.test(val)) val += '; Partitioned';
                } else if (!/SameSite=/i.test(val)) {
                  val += '; SameSite=None; Secure; Partitioned';
                }
              }
              return originalSet.call(this, val);
            },
            configurable: true,
          });
        }
      } catch (e) {}
    },

    // 4. Contain window.open to keep all popups, links, and external portals inside the iframe
    'contain-window-open': function() {
      try {
        window.open = function(url, target, features) {
          if (url && typeof url === 'string') {
            try {
              window.location.assign(url);
            } catch (err) {
              window.location.href = url;
            }
          }
          return window;
        };
      } catch (e) {}

      try {
        const interceptLink = function(event) {
          try {
            let target = event.target;
            while (target && target.tagName !== 'A' && target.tagName !== 'AREA') {
              target = target.parentElement;
            }
            if (target && (target.target === '_blank' || target.target === '_top' || target.target === '_parent')) {
              target.target = '_self';
            }
          } catch (e) {}
        };
        document.addEventListener('click', interceptLink, true);
        window.addEventListener('click', interceptLink, true);
      } catch (e) {}
    },

    // 5. Strip DOM <meta http-equiv> Content-Security-Policy & X-Frame-Options tags
    'strip-meta-csp': function() {
      try {
        // Immediate clean of existing elements
        const removeBlockingMetas = () => {
          const metas = document.querySelectorAll('meta[http-equiv]');
          for (const meta of metas) {
            const equiv = meta.getAttribute('http-equiv') || '';
            if (/^(content-security-policy|x-frame-options|frame-options)$/i.test(equiv.trim())) {
              console.log('[CDP Bridge] Stripped DOM <meta http-equiv> tag:', equiv);
              meta.remove();
            }
          }
        };

        removeBlockingMetas();

        // Observe head insertions before DOM parsing completes
        const observer = new MutationObserver((mutations) => {
          for (const mutation of mutations) {
            for (const node of mutation.addedNodes) {
              if (node.nodeType === 1 && node.tagName === 'META') {
                const equiv = node.getAttribute('http-equiv') || '';
                if (/^(content-security-policy|x-frame-options|frame-options)$/i.test(equiv.trim())) {
                  console.log('[CDP Bridge] Stripped dynamic DOM <meta http-equiv> tag:', equiv);
                  node.remove();
                }
              }
            }
          }
        });

        const observeTarget = document.documentElement || document;
        if (observeTarget) {
          observer.observe(observeTarget, { childList: true, subtree: true });
        }
        document.addEventListener('DOMContentLoaded', removeBlockingMetas);
        window.addEventListener('DOMContentLoaded', removeBlockingMetas);
      } catch (e) {}
    },
  };

  // Default baseline patches applied everywhere safely
  PATCHES['spoof-top-hierarchy']();
  PATCHES['hide-webdriver']();
  PATCHES['rewrite-cookies-chips']();
  PATCHES['contain-window-open']();
  PATCHES['strip-meta-csp']();

  // Hook History API in MAIN world to capture SPA navigations (pushState / replaceState)
  try {
    const notifyParentOfNavigation = () => {
      try {
        if (window !== window.top && window.location && window.location.href) {
          const liveUrl = window.location.href;
          const liveTitle = document.title || '';
          window.parent.postMessage({
            type: 'COBROWSE_IFRAME_NAVIGATED',
            url: liveUrl,
            title: liveTitle,
          }, '*');
        }
      } catch (e) {}
    };

    const origPushState = history.pushState;
    history.pushState = function() {
      const res = origPushState.apply(this, arguments);
      notifyParentOfNavigation();
      return res;
    };

    const origReplaceState = history.replaceState;
    history.replaceState = function() {
      const res = origReplaceState.apply(this, arguments);
      notifyParentOfNavigation();
      return res;
    };

    window.addEventListener('popstate', notifyParentOfNavigation);
    window.addEventListener('hashchange', notifyParentOfNavigation);
    window.addEventListener('DOMContentLoaded', notifyParentOfNavigation);
  } catch (e) {}
})();
