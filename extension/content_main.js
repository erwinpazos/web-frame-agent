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

    // 4. Contain window.open to keep navigation inside the iframe, EXCEPT for OAuth authentication portals
    // (Google, Apple, Microsoft, GitHub) which strictly forbid iframe embedding and must open as genuine popups.
    'contain-window-open': function() {
      const isAuthUrl = function(url) {
        if (!url || typeof url !== 'string') return false;
        const lower = url.toLowerCase();
        return (
          lower.includes('accounts.google.com') ||
          lower.includes('appleid.apple.com') ||
          lower.includes('login.microsoftonline.com') ||
          lower.includes('github.com/login/oauth') ||
          (lower.includes('facebook.com/v') && lower.includes('/dialog/oauth'))
        );
      };

      // Universal detection of cloud storage buckets, blob endpoints, document downloads and file attachments.
      // These URLs MUST NEVER navigate the workspace iframe, as doing so aborts the active job application form
      // or triggers AWS S3 / GCS AccessDenied errors on private drop-box uploads.
      const isDocumentOrStorageUrl = function(url) {
        if (!url || typeof url !== 'string') return false;
        try {
          const lower = url.toLowerCase().trim();
          if (lower.startsWith('blob:') || lower.startsWith('data:application/')) return true;

          const parsed = new URL(url, window.location.href);
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
      };

      try {
        const originalWindowOpen = window.open;
        window.open = function(url, target, features) {
          if (url && typeof url === 'string') {
            // 1. OAuth providers fail with 403 or reject in-iframe rendering: delegate to native popup
            if (isAuthUrl(url)) {
              console.log('[CDP Bridge] OAuth portal detected, delegating to native popup:', url);
              const popupWin = originalWindowOpen.call(window, url, target || '_blank', features || 'width=520,height=640,menubar=no,toolbar=no');
              if (popupWin) {
                // Monitor popup completion: when closed, ensure storage access without aborting in-flight login requests
                const checkClosed = setInterval(() => {
                  try {
                    if (popupWin.closed) {
                      clearInterval(checkClosed);
                      console.log('[CDP Bridge] OAuth popup closed, ensuring storage access.');
                      if (document.requestStorageAccess) {
                        document.requestStorageAccess().catch(() => {});
                      }
                    }
                  } catch (e) {
                    clearInterval(checkClosed);
                  }
                }, 500);
              }
              return popupWin;
            }

            // 2. Document previews, downloads, and cloud storage URLs: delegate to native tab/download
            // to protect the active workspace iframe form from being overwritten by S3 AccessDenied or binary streams.
            if (isDocumentOrStorageUrl(url)) {
              console.log('[CDP Bridge] Document/Storage URL detected, delegating to native tab:', url);
              return originalWindowOpen.call(window, url, target || '_blank', features);
            }

            // 3. Standard web pages: contain within the workspace iframe
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
            if (!target || !target.href) return;

            // Allow OAuth authentication links to open in external popup/tab
            if (isAuthUrl(target.href)) {
              return;
            }

            // Document previews, downloads, and storage bucket links must NEVER navigate the iframe.
            // Ensure they open in a separate tab or trigger native download, preserving the application form.
            if (isDocumentOrStorageUrl(target.href) || target.hasAttribute('download')) {
              target.target = '_blank';
              return;
            }

            // General web links: contain navigation inside the workspace iframe
            if (target.target === '_blank' || target.target === '_top' || target.target === '_parent') {
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
    // 6. Request W3C Storage Access API on user interactions (click, pointerdown, keydown)
    'request-storage-access': function() {
      try {
        const unlockStorage = async function() {
          try {
            if (document.requestStorageAccess) {
              const has = document.hasStorageAccess ? await document.hasStorageAccess() : false;
              if (!has) {
                await document.requestStorageAccess();
                console.log('[CDP Bridge] Main-world storage access unlocked for:', window.location.hostname);
              }
            }
          } catch (e) {}
        };
        document.addEventListener('click', unlockStorage, { capture: true, passive: true });
        document.addEventListener('pointerdown', unlockStorage, { capture: true, passive: true });
        document.addEventListener('keydown', unlockStorage, { capture: true, passive: true });
        if (document.hasStorageAccess) {
          document.hasStorageAccess().then((has) => {
            if (!has && document.requestStorageAccess) {
              document.requestStorageAccess().catch(() => {});
            }
          }).catch(() => {});
        }
      } catch (e) {}
    },
  };

  // Default baseline patches applied everywhere safely
  PATCHES['spoof-top-hierarchy']();
  PATCHES['hide-webdriver']();
  PATCHES['rewrite-cookies-chips']();
  PATCHES['contain-window-open']();
  PATCHES['strip-meta-csp']();
  PATCHES['request-storage-access']();

  // Hook History API in MAIN world to capture SPA navigations (pushState / replaceState)
  try {
    const notifyParentOfNavigation = () => {
      try {
        const isDirectChild = window !== window.top && window.parent === window.top;
        if (isDirectChild && window.location && window.location.href) {
          const liveUrl = window.location.href;
          if (!liveUrl || liveUrl.startsWith('about:')) return;
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
