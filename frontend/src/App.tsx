import { useState, useRef, useEffect } from 'react'
import {
  Globe,
  RefreshCw,
  ExternalLink,
  ShieldCheck,
  ShieldAlert,
  Radio,
  HelpCircle,
  ChevronDown,
  ChevronUp,
} from 'lucide-react'
import { Chatbot } from './components/Chatbot'
import { useAgentChat } from './hooks/useAgentChat'

const DEFAULT_URL = 'https://www.google.com'
declare const __COBROWSE_CONFIG__: { backendWsUrl: string; frontendUrl: string; apiToken?: string } | undefined

export function App() {
  const [currentUrl, setCurrentUrl] = useState<string>(DEFAULT_URL)
  const currentUrlRef = useRef<string>(DEFAULT_URL)
  const [inputUrl, setInputUrl] = useState<string>(DEFAULT_URL)
  const [isLoadingIframe, setIsLoadingIframe] = useState<boolean>(false)
  const [showHelp, setShowHelp] = useState<boolean>(false)
  const [backendStatus, setBackendStatus] = useState<'connected' | 'offline' | 'checking'>('checking')
  const [extensionConnected, setExtensionConnected] = useState<boolean>(false)
  const [isIframeReady, setIsIframeReady] = useState<boolean>(false)
  const [panelWidth, setPanelWidth] = useState<number>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('cobrowse_panel_width')
      if (saved) {
        const parsed = parseInt(saved, 10)
        if (!isNaN(parsed) && parsed >= 320 && parsed <= 1200) return parsed
      }
    }
    return 480
  })
  const [isDragging, setIsDragging] = useState<boolean>(false)
  const [modelName, setModelName] = useState<string>('')
  const [authToken, setAuthToken] = useState<string>('')

  const iframeRef = useRef<HTMLIFrameElement>(null)

  const isHostWorkspaceDestination = (url: string): boolean => {
    try {
      const parsed = new URL(url)
      return (
        parsed.origin === window.location.origin ||
        parsed.host === window.location.host ||
        parsed.hostname === 'localhost' ||
        parsed.hostname === '127.0.0.1'
      )
    } catch {
      return false
    }
  }

  const handleAgentUrlChanged = (newUrl: string) => {
    if (
      newUrl &&
      newUrl !== currentUrlRef.current &&
      !newUrl.startsWith('about:') &&
      !newUrl.startsWith('javascript:') &&
      !newUrl.startsWith('data:') &&
      !isHostWorkspaceDestination(newUrl)
    ) {
      currentUrlRef.current = newUrl
      setCurrentUrl(newUrl)
      setInputUrl(newUrl)
      if (iframeRef.current) {
        setIsLoadingIframe(true)
        iframeRef.current.src = newUrl
      }
    }
  }

  // Initialize Agent Chat hook
  const {
    messages,
    isBusy,
    wsStatus,
    sendMessage,
    stopAgent,
    clearChat,
  } = useAgentChat({ onUrlChanged: handleAgentUrlChanged, onIframeStatus: setIsIframeReady, authToken })

  // Periodically check backend health
  // Bootstrap ephemeral session token and check backend health
  useEffect(() => {
    const bootstrapSession = async () => {
      try {
        const res = await fetch('/api/v1/auth/session', { method: 'POST' })
        if (res.ok) {
          const data = await res.json()
          if (data.session_token) {
            setAuthToken(data.session_token)
            window.postMessage({
              type: 'COBROWSE_SESSION_TOKEN',
              sessionToken: data.session_token,
            }, window.location.origin)
          }
        }
      } catch (e) {
        console.warn('Failed to bootstrap session ticket:', e)
      }
    }

    bootstrapSession()

    const checkHealth = async () => {
      try {
        const res = await fetch('/health')
        if (res.ok) {
          const data = await res.json()
          setBackendStatus('connected')
          setExtensionConnected(data.extension_connected || false)
          setIsIframeReady(Boolean(data.iframe_ready))
          const activeModel = data.llm_model || data.vertex_model
          if (activeModel) {
            setModelName(activeModel)
          }
        } else {
          setBackendStatus('offline')
        }
      } catch {
        setBackendStatus('offline')
      }
    }

    checkHealth()
    const interval = setInterval(checkHealth, 5000)
    return () => clearInterval(interval)
  }, [])

  // Listen for iframe navigation events emitted by the browser extension
  useEffect(() => {
    const handleIframeMessage = (event: MessageEvent) => {
      if (
        event.data &&
        event.data.type === 'COBROWSE_IFRAME_NAVIGATED' &&
        typeof event.data.url === 'string'
      ) {
        const nextUrl = event.data.url
        if (
          nextUrl &&
          nextUrl !== currentUrlRef.current &&
          !nextUrl.startsWith('about:') &&
          !nextUrl.startsWith(window.location.origin) &&
          !nextUrl.includes(window.location.host)
        ) {
          currentUrlRef.current = nextUrl
          setCurrentUrl(nextUrl)
          setInputUrl(nextUrl)
        }
      }
    }

    window.addEventListener('message', handleIframeMessage)
    return () => window.removeEventListener('message', handleIframeMessage)
  }, [])

  const handleNavigate = (e: React.FormEvent) => {
    e.preventDefault()
    let dest = inputUrl.trim()
    if (!dest) return

    if (!dest.startsWith('http://') && !dest.startsWith('https://')) {
      dest = 'https://' + dest
    }

    if (isHostWorkspaceDestination(dest)) {
      return
    }

    currentUrlRef.current = dest
    setCurrentUrl(dest)
    setInputUrl(dest)

    if (iframeRef.current) {
      setIsLoadingIframe(true)
      iframeRef.current.src = dest
    }
  }
  const handleReload = () => {
    if (iframeRef.current) {
      setIsLoadingIframe(true)
      iframeRef.current.src = currentUrlRef.current
    }
  }

  // Mouse drag handler for resizing the split pane
  useEffect(() => {
    if (!isDragging) return

    const handleMouseMove = (e: MouseEvent) => {
      const minW = 320
      const maxW = Math.min(window.innerWidth - 360, 1100)
      const newWidth = Math.max(minW, Math.min(maxW, e.clientX))
      setPanelWidth(newWidth)
    }

    const handleMouseUp = () => {
      setIsDragging(false)
      try {
        localStorage.setItem('cobrowse_panel_width', String(panelWidth))
      } catch {}
    }

    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }
  }, [isDragging, panelWidth])

  return (
    <div className="flex flex-col h-screen w-screen bg-[#090a0f] text-[#eceff4] font-sans select-none overflow-hidden">
      {/* Top Header / Control Bar */}
      <header className="h-13 border-b border-[#1e2230] bg-[#10121a] px-4 flex items-center justify-between gap-4 z-20 shrink-0">
        {/* Brand & Workspace Indicator */}
        <div className="flex items-center gap-3 min-w-fit">
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-none bg-cyan-400" />
            <h1 className="text-xs font-mono font-semibold tracking-wider text-slate-100 uppercase">
              WEB-FRAME <span className="text-cyan-400">/</span> AGENT
            </h1>
          </div>
          <span className="hidden sm:inline text-[10px] font-mono text-[#8892b0] border-l border-[#1e2230] pl-3">
            DUAL-VIEW WORKSPACE
          </span>
        </div>

        {/* Global URL Navigation Form */}
        <form onSubmit={handleNavigate} className="flex-1 max-w-2xl flex items-center gap-1.5">
          <div className="relative flex-1 flex items-center">
            <Globe className="w-3.5 h-3.5 text-[#8892b0] absolute left-3 pointer-events-none" />
            <input
              type="text"
              value={inputUrl}
              onChange={(e) => setInputUrl(e.target.value)}
              placeholder="https://www.google.com or any URL..."
              className="w-full bg-[#090a0f] border border-[#1e2230] focus:border-cyan-400 text-xs font-mono text-slate-200 pl-8 pr-3 py-1.5 outline-none transition"
            />
          </div>
          <button
            type="submit"
            className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-cyan-400 text-xs font-mono font-medium border border-cyan-400/30 transition cursor-pointer"
          >
            EXEC
          </button>
          <button
            type="button"
            onClick={handleReload}
            title="Reload target page"
            className="p-1.5 text-[#8892b0] hover:text-slate-200 hover:bg-[#141722] border border-transparent hover:border-[#1e2230] transition cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isLoadingIframe ? 'animate-spin text-cyan-400' : ''}`} />
          </button>
        </form>

        {/* Top Right Controls & Indicators */}
        <div className="flex items-center gap-2">
          {/* Extension Status Helper */}
          <button
            onClick={() => setShowHelp(!showHelp)}
            className={`flex items-center gap-1.5 px-2.5 py-1 border text-xs font-mono transition cursor-pointer ${
              extensionConnected
                ? 'bg-emerald-950/20 text-emerald-400 border-emerald-800/40'
                : 'bg-rose-950/20 text-rose-400 border-rose-800/40 animate-pulse'
            }`}
            title={
              extensionConnected
                ? 'Chrome extension connected to the CDP bridge'
                : 'Extension disconnected. Click for instructions.'
            }
          >
            {extensionConnected ? (
              <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
            ) : (
              <ShieldAlert className="w-3.5 h-3.5 text-rose-400" />
            )}
            <span className="hidden sm:inline text-[10px] tracking-wider uppercase">
              {extensionConnected ? 'EXT_OK' : 'EXT_DISCONNECTED'}
            </span>
            {showHelp ? <ChevronUp className="w-3 h-3 text-[#8892b0]" /> : <ChevronDown className="w-3 h-3 text-[#8892b0]" />}
          </button>

          {/* WebSocket Status */}
          <div
            className="flex items-center gap-1.5 px-2.5 py-1 bg-[#090a0f] border border-[#1e2230] text-[10px] font-mono"
            title={`WebSocket: ${wsStatus} | Backend: ${backendStatus}`}
          >
            <Radio
              className={`w-3 h-3 ${
                wsStatus === 'connected'
                  ? 'text-cyan-400'
                  : 'text-amber-500 animate-pulse'
              }`}
            />
            <span className="text-[#8892b0] hidden xl:inline uppercase">
              {wsStatus === 'connected' ? 'LIVE' : 'CONN_WAIT'}
            </span>
          </div>
        </div>
      </header>

      {/* Extension Help Banner / Dropdown */}
      {showHelp && (
        <div className="bg-[#10121a] border-b border-[#1e2230] p-4 text-xs font-mono z-30 shrink-0">
          <div className="max-w-4xl mx-auto flex items-start gap-3">
            <HelpCircle className="w-5 h-5 text-cyan-400 shrink-0 mt-0.5" />
            <div className="space-y-2">
              <p className="font-semibold text-slate-200 uppercase tracking-wide text-[11px]">
                Direct Browser View Attachment Instructions:
              </p>
              <ol className="list-decimal list-inside space-y-1 text-[#8892b0] leading-relaxed text-[11px]">
                <li>Open Chrome and navigate to <code className="bg-[#090a0f] px-1.5 py-0.5 border border-[#1e2230] text-cyan-400">chrome://extensions</code></li>
                <li>Enable <strong>Developer mode</strong> (top right).</li>
                <li>Click <strong>Load unpacked</strong>.</li>
                <li>Select the <code className="bg-[#090a0f] px-1.5 py-0.5 border border-[#1e2230] text-cyan-400">extension</code> directory in this repository.</li>
                <li>Reload this page (F5) to allow CDP bridge auto-attachment.</li>
              </ol>
            </div>
          </div>
        </div>
      )}

      {/* Main Split-Screen Layout */}
      <div className={`flex-1 flex flex-col md:flex-row w-full h-full overflow-hidden ${isDragging ? 'select-none pointer-events-none' : ''}`}>
        {/* Left Section: Chatbot with Browser-Use Agent */}
        <section
          style={{ width: `${panelWidth}px` }}
          className="h-full shrink-0 flex flex-col overflow-hidden"
        >
          <Chatbot
            messages={messages}
            isBusy={isBusy}
            isReady={isIframeReady}
            modelName={modelName}
            onSendMessage={(prompt, attachments) => sendMessage(prompt, currentUrl, attachments)}
            onStopAgent={stopAgent}
            onClearChat={clearChat}
          />
        </section>

        {/* Draggable Divider Handle */}
        <div
          onMouseDown={(e) => {
            e.preventDefault()
            setIsDragging(true)
          }}
          title="Drag to resize console and workspace"
          className={`hidden md:flex w-1.5 hover:w-2 -ml-0.5 z-30 cursor-col-resize items-center justify-center transition-all group ${
            isDragging ? 'bg-cyan-400 w-2' : 'bg-[#1e2230] hover:bg-cyan-400/80'
          }`}
        >
          <div className="w-0.5 h-6 bg-[#2d3345] group-hover:bg-[#090a0f] rounded-full" />
        </div>

        {/* Right Section: Embedded Target View Iframe */}
        <section className="flex-1 h-full flex flex-col bg-[#090a0f] relative overflow-hidden">
          {/* Iframe Top Bar */}
          <div className="h-7 border-b border-[#1e2230] bg-[#10121a] px-3 flex items-center justify-between text-[10px] font-mono text-[#8892b0]">
            <div className="flex items-center gap-2 truncate">
              <span className="w-1.5 h-1.5 bg-cyan-400" />
              <span className="font-semibold text-slate-300 uppercase">TARGET_DOM:</span>
              <span className="text-cyan-400 truncate">{currentUrl}</span>
            </div>

            <div className="flex items-center gap-2">
              <a
                href={currentUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-[#8892b0] hover:text-cyan-400 transition"
                title="Open in new external tab"
              >
                <span>POP_OUT</span>
                <ExternalLink className="w-2.5 h-2.5" />
              </a>
            </div>
          </div>

          {/* Iframe Container */}
          <div className="flex-1 relative w-full h-full bg-[#090a0f]">
            {/* Attachment Veil: shown until Chrome extension binds to target iframe OOPIF session */}
            {!isIframeReady && (
              <div className="absolute inset-0 bg-[#090a0f]/85 backdrop-blur-xs flex flex-col items-center justify-center p-6 text-center z-20 select-none">
                <div className="max-w-md p-6 bg-[#10121a] border border-[#1e2230] flex flex-col items-center gap-3.5 shadow-2xl">
                  <div className="relative flex items-center justify-center">
                    <Radio className="w-8 h-8 text-amber-500 animate-pulse" />
                  </div>
                  <div className="space-y-1.5">
                    <h3 className="font-mono font-semibold text-xs tracking-wider text-slate-200 uppercase">
                      ATTACH_WAIT: Target View Synchronizing
                    </h3>
                    <p className="text-[11px] font-mono text-[#8892b0] leading-relaxed">
                      The Chrome extension is attaching to the target workspace iframe via CDP.
                      Autonomous agent navigation will be enabled as soon as the session binds.
                    </p>
                  </div>
                  <div className="flex items-center gap-2 px-2.5 py-1 bg-[#090a0f] border border-[#1e2230] text-[10px] font-mono text-[#8892b0]">
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-ping" />
                    <span>AWAITING EXTENSION OOPIF BINDING...</span>
                  </div>
                </div>
              </div>
            )}
            {isLoadingIframe && (
              <div className="absolute inset-0 bg-[#090a0f]/80 backdrop-blur-xs flex flex-col items-center justify-center gap-2.5 z-10 pointer-events-none">
                <RefreshCw className="w-6 h-6 text-cyan-400 animate-spin" />
                <p className="text-xs font-mono text-[#8892b0]">
                  Loading target DOM...
                </p>
              </div>
            )}
            <iframe
              ref={iframeRef}
              defaultValue={DEFAULT_URL}
              src={DEFAULT_URL}
              title="Workspace Target View"
              allow="storage-access; camera; microphone; clipboard-write; clipboard-read; payment; geolocation"
              className="w-full h-full border-none bg-white"
              style={{
                willChange: 'transform',
                transform: 'translateZ(0)',
              }}
              onLoad={() => setIsLoadingIframe(false)}
            />
          </div>
        </section>
      </div>
    </div>
  )
}

export default App
