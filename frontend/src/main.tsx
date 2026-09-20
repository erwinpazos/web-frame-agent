declare const __COBROWSE_CONFIG__: { backendWsUrl: string; frontendUrl: string }

// Expose configuration dynamically to window and DOM for the Chrome extension (NO tokens in DOM)
if (typeof __COBROWSE_CONFIG__ !== 'undefined') {
  const dynamicFrontendUrl = __COBROWSE_CONFIG__.frontendUrl || window.location.origin
  const config = {
    backendWsUrl: __COBROWSE_CONFIG__.backendWsUrl,
    frontendUrl: dynamicFrontendUrl,
  }
  ;(window as unknown as { __COBROWSE_CONFIG__: typeof config }).__COBROWSE_CONFIG__ = config
  document.documentElement.setAttribute('data-cobrowse-ws', config.backendWsUrl)
  document.documentElement.setAttribute('data-cobrowse-frontend', config.frontendUrl)
  window.postMessage({
    type: 'COBROWSE_CONFIG_AVAILABLE',
    backendWsUrl: config.backendWsUrl,
    frontendUrl: config.frontendUrl,
  }, window.location.origin)
}
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
