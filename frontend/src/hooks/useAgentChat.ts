import { useState, useEffect, useRef, useCallback } from 'react'
import type { ChatMessage, AgentStep, WebSocketStatus, ChatAttachment } from '../types/chat'

interface UseAgentChatProps {
  onUrlChanged?: (newUrl: string) => void
  onIframeStatus?: (ready: boolean) => void
  authToken?: string
}
export function useAgentChat({ onUrlChanged, onIframeStatus, authToken }: UseAgentChatProps = {}) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome-1',
      sender: 'system',
      text: "Web-Frame Agent workspace initialized. Inspect the target view on the right or input an action instruction below.",
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    },
  ])
  const [isBusy, setIsBusy] = useState<boolean>(false)
  const [wsStatus, setWsStatus] = useState<WebSocketStatus>('connecting')
  const [sessionId] = useState<string>(() => 'session-' + Date.now() + '-' + Math.random().toString(36).substring(2, 9))
  const sessionIdRef = useRef<string>(sessionId)
  const wsRef = useRef<WebSocket | null>(null)
  const activeAgentMsgIdRef = useRef<string | null>(null)
  const activeTaskIdRef = useRef<string | null>(null)
  const onUrlChangedRef = useRef(onUrlChanged)
  const onIframeStatusRef = useRef(onIframeStatus)
  const authTokenRef = useRef(authToken)

  useEffect(() => {
    onUrlChangedRef.current = onUrlChanged
    onIframeStatusRef.current = onIframeStatus
    authTokenRef.current = authToken
  }, [onUrlChanged, onIframeStatus, authToken])
  const handleWebSocketMessage = useCallback((data: Record<string, unknown>) => {
    const type = typeof data.type === 'string' ? data.type : ''

    if (type === 'task_accepted') {
      if (typeof data.task_id === 'string') {
        activeTaskIdRef.current = data.task_id
      }
    } else if (type === 'agent_started') {
      setIsBusy(true)
      const activeId = activeAgentMsgIdRef.current
      if (activeId) {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === activeId ? { ...msg, status: 'thinking' } : msg
          )
        )
      }
    } else if (type === 'agent_step') {
      const stepTaskId = typeof data.task_id === 'string' ? data.task_id : null
      if (stepTaskId && activeTaskIdRef.current && stepTaskId !== activeTaskIdRef.current) {
        return
      }
      const rawActions = Array.isArray(data.actions) ? data.actions : []
      const step: AgentStep = {
        step_number: typeof data.step_number === 'number' ? data.step_number : 0,
        max_steps: typeof data.max_steps === 'number' ? data.max_steps : 15,
        thinking: typeof data.thinking === 'string' ? data.thinking : '',
        next_goal: typeof data.next_goal === 'string' ? data.next_goal : '',
        evaluation: typeof data.evaluation === 'string' ? data.evaluation : '',
        actions: rawActions as AgentStep['actions'],
        current_url: typeof data.current_url === 'string' ? data.current_url : '',
        current_title: typeof data.current_title === 'string' ? data.current_title : '',
      }

      const activeId = activeAgentMsgIdRef.current
      if (activeId) {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id !== activeId) return msg
            const existingSteps = msg.steps || []
            const existingIndex = existingSteps.findIndex((s) => s.step_number === step.step_number)
            const updatedSteps = existingIndex >= 0
              ? existingSteps.map((s, idx) => (idx === existingIndex ? step : s))
              : [...existingSteps, step]

            return {
              ...msg,
              status: 'acting',
              steps: updatedSteps,
            }
          })
        )
      }

      if (step.current_url && onUrlChangedRef.current) {
        onUrlChangedRef.current(step.current_url)
      }
    } else if (type === 'agent_stream_chunk') {
      const chunkTaskId = typeof data.task_id === 'string' ? data.task_id : null
      if (chunkTaskId && activeTaskIdRef.current && chunkTaskId !== activeTaskIdRef.current) {
        return
      }
      const chunk = typeof data.chunk === 'string' ? data.chunk : ''
      if (chunk) {
        setMessages((prev) => {
          const targetId =
            activeAgentMsgIdRef.current ||
            [...prev].reverse().find((m) => m.sender === 'agent')?.id
          if (!targetId) return prev
          return prev.map((msg) =>
            msg.id === targetId
              ? {
                  ...msg,
                  isStreaming: true,
                  text: (msg.text || '') + chunk,
                }
              : msg
          )
        })
      }
    } else if (type === 'agent_finished') {
      const finishedTaskId = typeof data.task_id === 'string' ? data.task_id : null
      if (finishedTaskId && activeTaskIdRef.current && finishedTaskId !== activeTaskIdRef.current) {
        console.warn('Ignoring stale agent_finished event for cancelled task:', finishedTaskId)
        return
      }
      setIsBusy(false)
      activeTaskIdRef.current = null
      const resultText = typeof data.result === 'string' ? data.result : 'Task completed.'
      setMessages((prev) => {
        const targetId =
          activeAgentMsgIdRef.current ||
          [...prev].reverse().find((m) => m.sender === 'agent')?.id
        if (!targetId) return prev
        return prev.map((msg) =>
          msg.id === targetId
            ? {
                ...msg,
                status: 'done',
                isStreaming: false,
                text: resultText || msg.text,
              }
            : msg
        )
      })
      activeAgentMsgIdRef.current = null
    } else if (type === 'task_stopped') {
      setIsBusy(false)
      activeTaskIdRef.current = null
      activeAgentMsgIdRef.current = null
    } else if (type === 'agent_error') {
      setIsBusy(false)
      const activeId = activeAgentMsgIdRef.current
      if (activeId) {
        const errorText = typeof data.error === 'string' ? data.error : 'Unknown error'
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === activeId
              ? {
                  ...msg,
                  status: 'error',
                  text: `Error: ${errorText}`,
                }
              : msg
          )
        )
      }
      activeAgentMsgIdRef.current = null
    } else if (type === 'iframe_status') {
      const ready = Boolean(data.iframe_ready)
      if (onIframeStatusRef.current) {
        onIframeStatusRef.current(ready)
      }
    }
  }, [])

  const handleMessageRef = useRef(handleWebSocketMessage)
  useEffect(() => {
    handleMessageRef.current = handleWebSocketMessage
  }, [handleWebSocketMessage])

  useEffect(() => {
    let ws: WebSocket | null = null
    let reconnectTimeout: number | undefined
    let isDisposed = false

    const connectWS = () => {
      if (isDisposed) return

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const token = authTokenRef.current
      const tokenQuery = token ? `?token=${encodeURIComponent(token)}` : ''
      const wsUrl = `${protocol}//${window.location.host}/api/v1/ws/chat${tokenQuery}`

      setWsStatus('connecting')
      ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        if (!isDisposed) {
          setWsStatus('connected')
          try {
            ws?.send(JSON.stringify({ type: 'get_status' }))
          } catch {}
        }
      }

      ws.onmessage = (event: MessageEvent) => {
        if (isDisposed) return
        try {
          const data = JSON.parse(event.data) as Record<string, unknown>
          handleMessageRef.current(data)
        } catch (err) {
          console.error('Failed to parse WebSocket message:', err)
        }
      }

      ws.onerror = () => {
        if (!isDisposed) {
          setWsStatus('disconnected')
          setIsBusy(false)
        }
      }

      ws.onclose = () => {
        if (!isDisposed) {
          setWsStatus('disconnected')
          setIsBusy(false)
          reconnectTimeout = window.setTimeout(connectWS, 2000)
        }
      }
    }

    connectWS()

    return () => {
      isDisposed = true
      clearTimeout(reconnectTimeout)
      if (ws) {
        ws.close()
      }
      wsRef.current = null
    }
  }, [authToken])

  const sendMessage = useCallback((prompt: string, targetUrl: string, attachments?: ChatAttachment[]) => {
    const trimmedPrompt = prompt.trim()
    const hasAttachments = Boolean(attachments && attachments.length > 0)
    if (!trimmedPrompt && !hasAttachments) return

    const userMsgId = 'user-' + Date.now()
    const agentMsgId = 'agent-' + (Date.now() + 1)
    activeAgentMsgIdRef.current = agentMsgId

    const timeStr = new Date().toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    })

    const userMsg: ChatMessage = {
      id: userMsgId,
      sender: 'user',
      text: trimmedPrompt || (hasAttachments ? '(Attached content)' : ''),
      attachments: hasAttachments ? attachments : undefined,
      timestamp: timeStr,
    }

    const agentPlaceholderMsg: ChatMessage = {
      id: agentMsgId,
      sender: 'agent',
      text: '',
      timestamp: timeStr,
      status: 'thinking',
      steps: [],
    }

    setMessages((prev) => [...prev, userMsg, agentPlaceholderMsg])
    setIsBusy(true)

    // Build the payload for the agent including any attached content
    let fullTask = trimmedPrompt
    if (hasAttachments && attachments) {
      const formatted = attachments.map((att) =>
        `\n\n--- [ATTACHMENT: ${att.name} (${att.lineCount} lines, ${att.size})] ---\n${att.content}\n--- [END ATTACHMENT] ---`
      ).join('')
      fullTask = fullTask ? `${fullTask}${formatted}` : formatted.trim()
    }

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: 'start_task',
          task: fullTask,
          url: targetUrl,
          session_id: sessionIdRef.current,
          max_steps: 15,
        })
      )
    } else {
      setTimeout(() => {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === agentMsgId
              ? {
                  ...msg,
                  status: 'error',
                  text: 'WebSocket not connected. Please refresh the page.',
                }
              : msg
          )
        )
        setIsBusy(false)
        activeAgentMsgIdRef.current = null
      }, 500)
    }
  }, [])

  const stopAgent = useCallback(async () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: 'stop_task',
        })
      )
    }
    setIsBusy(false)
    activeAgentMsgIdRef.current = null
  }, [])

  const clearChat = useCallback(() => {
    const newSessionId = 'session-' + Date.now() + '-' + Math.random().toString(36).substring(2, 9)
    sessionIdRef.current = newSessionId
    activeTaskIdRef.current = null
    activeAgentMsgIdRef.current = null
    setIsBusy(false)

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({ type: 'clear_session' }))
      } catch {}
    }

    setMessages([
      {
        id: 'welcome-' + Date.now(),
        sender: 'system',
        text: 'Chat history cleared. New session started.',
        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      },
    ])
  }, [])

  return {
    messages,
    isBusy,
    wsStatus,
    sendMessage,
    stopAgent,
    clearChat,
  }
}
