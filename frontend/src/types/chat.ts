export interface AgentAction {
  raw?: string
  click_element?: { index?: number; xpath?: string }
  input_text?: { index?: number; text?: string }
  scroll_down?: { amount?: number }
  navigate?: { url?: string }
  [key: string]: unknown
}

export interface AgentStep {
  step_number: number
  max_steps: number
  thinking: string
  next_goal: string
  evaluation?: string
  actions: AgentAction[]
  current_url?: string
  current_title?: string
}
export interface ChatAttachment {
  id: string
  name: string
  content: string
  lineCount: number
  charCount: number
  size: string
}

export interface ChatMessage {
  id: string
  sender: 'user' | 'agent' | 'system'
  text?: string
  attachments?: ChatAttachment[]
  steps?: AgentStep[]
  status?: 'thinking' | 'acting' | 'done' | 'error'
  isStreaming?: boolean
  interrupted?: boolean
  interruptedReason?: string
  timestamp: string
  targetUrl?: string
}
export type WebSocketStatus = 'connecting' | 'connected' | 'disconnected'
