import { useState, useRef, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Send,
  Square,
  Terminal,
  User,
  ChevronDown,
  ChevronRight,
  RotateCcw,
  Activity,
  ArrowUpRight,
} from 'lucide-react'
import type { ChatMessage, AgentStep } from '../types/chat'

interface ChatbotProps {
  messages: ChatMessage[]
  isBusy: boolean
  isReady?: boolean
  modelName?: string
  onSendMessage: (prompt: string) => void
  onStopAgent: () => void
  onClearChat: () => void
}

export function Chatbot({
  messages,
  isBusy,
  isReady = true,
  modelName,
  onSendMessage,
  onStopAgent,
  onClearChat,
}: ChatbotProps) {
  const [inputText, setInputText] = useState('')
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({})
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isBusy])

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    if (!inputText.trim() || isBusy || !isReady) return
    onSendMessage(inputText)
    setInputText('')
  }

  const toggleStepExpansion = (stepKey: string) => {
    setExpandedSteps((prev) => ({
      ...prev,
      [stepKey]: !prev[stepKey],
    }))
  }

  return (
    <div className="flex flex-col h-full bg-[#10121a] text-[#eceff4] font-sans">
      {/* Console Header */}
      <div className="h-13 border-b border-[#1e2230] px-4 flex items-center justify-between bg-[#10121a] z-10 shrink-0">
        <div className="flex items-center gap-2.5">
          <div className="w-6 h-6 border border-cyan-400/40 bg-cyan-950/20 flex items-center justify-center text-cyan-400">
            <Terminal className="w-3.5 h-3.5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono font-semibold tracking-wider text-slate-200 uppercase">
                AGENT_CONSOLE
              </span>
              <span className="text-[10px] px-1.5 py-0.2 bg-[#141722] text-cyan-400 border border-[#1e2230] font-mono">
                {modelName || 'LLM_TARGET'}
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          {isBusy && (
            <button
              onClick={onStopAgent}
              className="flex items-center gap-1 px-2 py-0.5 bg-rose-950/30 hover:bg-rose-900/40 text-rose-300 border border-rose-800/50 text-[11px] font-mono transition cursor-pointer"
              title="Interrupt agent execution"
            >
              <Square className="w-2.5 h-2.5 fill-rose-300" />
              <span>ABORT</span>
            </button>
          )}

          <button
            onClick={onClearChat}
            className="p-1 text-[#8892b0] hover:text-slate-200 hover:bg-[#141722] border border-transparent hover:border-[#1e2230] transition cursor-pointer"
            title="Clear console"
          >
            <RotateCcw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Messages Scroll Area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.map((msg) => (
          <div key={msg.id} className="space-y-2">
            {msg.sender === 'user' ? (
              /* User Command Block */
              <div className="flex items-start justify-end gap-2.5">
                <div className="max-w-[85%] bg-[#141722] border border-[#2d3345] px-3.5 py-2 text-xs leading-relaxed">
                  <div className="flex items-center justify-between gap-4 mb-1 border-b border-[#1e2230] pb-1">
                    <span className="text-[10px] font-mono text-cyan-400 uppercase tracking-wider">PROMPT</span>
                    <span className="text-[10px] font-mono text-[#8892b0]">{msg.timestamp}</span>
                  </div>
                  <p className="text-slate-200 font-mono text-[12px]">{msg.text}</p>
                </div>
                <div className="w-5 h-5 border border-[#2d3345] bg-[#141722] flex items-center justify-center shrink-0 text-[#8892b0] text-[10px] mt-0.5">
                  <User className="w-3 h-3" />
                </div>
              </div>
            ) : msg.sender === 'system' ? (
              /* System Notice */
              <div className="bg-[#090a0f] border border-[#1e2230] p-3 text-[11px] font-mono text-[#8892b0] leading-relaxed">
                <span className="text-cyan-400 font-semibold mr-1.5">[SYSTEM]</span>
                {msg.text}
              </div>
            ) : (
              /* Agent Response */
              <div className="flex items-start gap-2.5">
                <div className="w-5 h-5 border border-cyan-400/40 bg-cyan-950/20 flex items-center justify-center shrink-0 text-cyan-400 text-[10px] mt-0.5">
                  <Terminal className="w-3 h-3" />
                </div>

                <div className="flex-1 space-y-2 max-w-[92%]">
                  {/* Status Indicator */}
                  {msg.status === 'thinking' && (!msg.steps || msg.steps.length === 0) && (
                    <div className="inline-flex items-center gap-2 px-3 py-1.5 bg-[#090a0f] border border-[#1e2230] text-cyan-400 font-mono text-xs animate-pulse">
                      <Activity className="w-3 h-3 animate-spin" />
                      <span>[ANALYZING DOM & EXECUTING REASONING LOOP]</span>
                    </div>
                  )}

                  {/* Steps Accordion */}
                  {msg.steps && msg.steps.length > 0 && (
                    <div className="space-y-1.5">
                      {msg.steps.map((step: AgentStep, sIdx: number) => {
                        const stepKey = `${msg.id}-step-${sIdx}`
                        const isExpanded = !!expandedSteps[stepKey]

                        return (
                          <div
                            key={sIdx}
                            className="bg-[#090a0f] border border-[#1e2230] text-xs font-mono"
                          >
                            <button
                              onClick={() => toggleStepExpansion(stepKey)}
                              className="w-full px-3 py-1.5 bg-[#090a0f] hover:bg-[#141722] flex items-center justify-between text-left text-slate-300 transition cursor-pointer"
                            >
                              <div className="flex items-center gap-2 truncate">
                                <span className="text-[10px] px-1 bg-[#141722] text-cyan-400 border border-cyan-400/20">
                                  STEP_{step.step_number}/{step.max_steps}
                                </span>
                                <span className="truncate text-[11px] text-[#8892b0]">
                                  {step.next_goal || step.thinking || 'Navigation action'}
                                </span>
                              </div>
                              {isExpanded ? (
                                <ChevronDown className="w-3 h-3 text-[#8892b0] shrink-0" />
                              ) : (
                                <ChevronRight className="w-3 h-3 text-[#8892b0] shrink-0" />
                              )}
                            </button>

                            {isExpanded && (
                              <div className="p-3 bg-[#10121a] border-t border-[#1e2230] space-y-2 text-[11px] leading-relaxed">
                                {step.thinking && (
                                  <div>
                                    <span className="font-semibold text-cyan-400 block mb-0.5 text-[10px] uppercase">
                                      THOUGHT_TRACE:
                                    </span>
                                    <p className="text-slate-300 font-mono bg-[#090a0f] p-2 border border-[#1e2230]">
                                      {step.thinking}
                                    </p>
                                  </div>
                                )}

                                {step.actions && step.actions.length > 0 && (
                                  <div>
                                    <span className="font-semibold text-slate-400 block mb-1 text-[10px] uppercase">
                                      CDP_ACTIONS:
                                    </span>
                                    <div className="flex flex-wrap gap-1">
                                      {step.actions.map((act, aIdx) => (
                                        <span
                                          key={aIdx}
                                          className="px-1.5 py-0.5 bg-[#090a0f] text-cyan-300 font-mono text-[10px] border border-[#1e2230]"
                                        >
                                          {JSON.stringify(act)}
                                        </span>
                                      ))}
                                    </div>
                                  </div>
                                )}

                                {step.current_url && (
                                  <div className="flex items-center gap-1 text-[10px] text-[#8892b0] truncate pt-1 border-t border-[#1e2230]">
                                    <ArrowUpRight className="w-3 h-3 text-cyan-400" />
                                    <span className="truncate font-mono">{step.current_url}</span>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}

                  {/* Final Response Content */}
                  {msg.text && (
                    <div className="bg-[#141722] border border-[#1e2230] p-3 text-xs text-slate-200 leading-relaxed font-sans">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          p: ({ ...props }) => <p className="mb-2 last:mb-0 leading-relaxed text-slate-200 text-xs" {...props} />,
                          ul: ({ ...props }) => <ul className="list-disc pl-4 mb-2 space-y-1 text-xs text-slate-300" {...props} />,
                          ol: ({ ...props }) => <ol className="list-decimal pl-4 mb-2 space-y-1 text-xs text-slate-300" {...props} />,
                          li: ({ ...props }) => <li className="leading-relaxed" {...props} />,
                          code: ({ className, children, ...props }) => {
                            const match = /language-(\w+)/.exec(className || '')
                            return match ? (
                              <code className="block bg-[#090a0f] p-2 overflow-x-auto text-cyan-400 font-mono text-[11px] my-2 border border-[#1e2230]" {...props}>
                                {children}
                              </code>
                            ) : (
                              <code className="bg-[#090a0f] px-1 py-0.5 text-cyan-400 font-mono text-[11px] border border-[#1e2230]" {...props}>
                                {children}
                              </code>
                            )
                          },
                          a: ({ ...props }) => <a className="text-cyan-400 hover:underline" target="_blank" rel="noopener noreferrer" {...props} />,
                        }}
                      >
                        {msg.text}
                      </ReactMarkdown>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Chat Input Bar */}
      <form onSubmit={handleSubmit} className="p-3 border-t border-[#1e2230] bg-[#10121a] shrink-0">
        <div className="relative flex items-center">
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            disabled={isBusy || !isReady}
            placeholder={
              !isReady
                ? "ATTACH_WAIT: Target view synchronizing..."
                : isBusy
                ? "AGENT_BUSY: Reasoning & navigating DOM..."
                : "Type instruction or query for the agent..."
            }
            className="w-full bg-[#090a0f] border border-[#1e2230] focus:border-cyan-400 text-xs font-mono text-slate-200 pl-3.5 pr-20 py-2.5 outline-none transition disabled:opacity-50"
          />

          <div className="absolute right-1.5 flex items-center gap-1">
            {isBusy ? (
              <button
                type="button"
                onClick={onStopAgent}
                className="px-2 py-1 bg-rose-950/40 hover:bg-rose-900/50 text-rose-300 border border-rose-800/60 font-mono text-[11px] transition cursor-pointer"
                title="Interrupt task execution"
              >
                ABORT
              </button>
            ) : (
              <button
                type="submit"
                disabled={!inputText.trim() || !isReady}
                className="p-1.5 text-cyan-400 hover:text-white hover:bg-slate-800 border border-transparent hover:border-[#1e2230] transition disabled:opacity-30 disabled:pointer-events-none cursor-pointer"
                title="Send command"
              >
                <Send className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>
      </form>
    </div>
  )
}
