import { ref, onScopeDispose, type Ref } from 'vue'
interface SearchConversation<TMessage> {
  id: string
  title: string
  messages: TMessage[]
  timestamp: number
  messageCount: number
}
interface UseSearchConversationsOptions<TMessage> {
  messages: Ref<TMessage[]>
  hasSearched: Ref<boolean>
  userInput: Ref<string>
  titleFromMessages: (messages: TMessage[]) => string
  onStorageError?: (message: string) => void
  stateStorageKey?: string
  conversationsStorageKey?: string
}
export function useSearchConversations<TMessage>({
  messages,
  hasSearched,
  userInput,
  titleFromMessages,
  onStorageError,
  stateStorageKey = 'searchAgentState',
  conversationsStorageKey = 'searchAgentConversations',
}: UseSearchConversationsOptions<TMessage>) {
  const conversations = ref<SearchConversation<TMessage>[]>([])
  const currentConversationId = ref<string | null>(null)
  const PERSIST_DEBOUNCE_MS = 900
  const MAX_HISTORY_BYTES = 2 * 1024 * 1024
  const MAX_STATE_BYTES = 1024 * 1024
  const MAX_CONVERSATIONS = 50
  let warned = false
  function storageWarning(text: string) {
    if (!warned) { warned = true; onStorageError?.(text) }
  }
  function writeBounded(key: string, value: unknown, maxBytes: number) {
    const encoded = JSON.stringify(value)
    if (new Blob([encoded]).size > maxBytes) throw new Error('会话内容超过本地保存容量')
    localStorage.setItem(key, encoded)
  }
  let conversationsPersistTimer: ReturnType<typeof setTimeout> | null = null
  let statePersistTimer: ReturnType<typeof setTimeout> | null = null
  function generateId(): string {
    return Date.now().toString(36) + Math.random().toString(36).slice(2)
  }
  function flushConversationsPersist() {
    if (conversationsPersistTimer) {
      clearTimeout(conversationsPersistTimer)
      conversationsPersistTimer = null
    }
    try {
      writeBounded(conversationsStorageKey, conversations.value, MAX_HISTORY_BYTES)
    } catch (e) {
      console.error('Failed to save conversations:', e)
      storageWarning('会话历史未能保存，请删除不需要的历史记录后重试；当前页面内容仍然保留。')
    }
  }
  function saveConversations(immediate = false) {
    if (immediate) {
      flushConversationsPersist()
      return
    }
    if (conversationsPersistTimer) clearTimeout(conversationsPersistTimer)
    conversationsPersistTimer = setTimeout(flushConversationsPersist, PERSIST_DEBOUNCE_MS)
  }
  function loadConversations() {
    try {
      const saved = localStorage.getItem(conversationsStorageKey)
      if (saved) {
        const parsed = JSON.parse(saved)
        if (!Array.isArray(parsed)) throw new Error('历史记录格式无效')
        conversations.value = parsed.filter(c => c && typeof c.id === 'string' && Array.isArray(c.messages))
      }
    } catch (e) {
      console.error('Failed to load conversations:', e)
      conversations.value = []
    }
  }
  function flushStatePersist() {
    if (statePersistTimer) {
      clearTimeout(statePersistTimer)
      statePersistTimer = null
    }
    try {
      writeBounded(
        stateStorageKey,
        {
          messages: messages.value,
          hasSearched: hasSearched.value,
          currentConversationId: currentConversationId.value,
          timestamp: Date.now(),
        }, MAX_STATE_BYTES
      )
    } catch {
      try { localStorage.removeItem(stateStorageKey) } catch { /* 存储不可用 */ }
      storageWarning('当前会话未能保存，刷新页面可能丢失进度。')
    }
  }
  function saveState(immediate = false) {
    if (immediate) {
      flushStatePersist()
      return
    }
    if (statePersistTimer) clearTimeout(statePersistTimer)
    statePersistTimer = setTimeout(flushStatePersist, PERSIST_DEBOUNCE_MS)
  }
  function loadState() {
    try {
      const saved = localStorage.getItem(stateStorageKey)
      if (!saved) return
      const state = JSON.parse(saved) as {
        messages?: TMessage[]
        hasSearched?: boolean
        currentConversationId?: string | null
        timestamp?: number
      }
      if (!state.timestamp || Date.now() - state.timestamp >= 24 * 60 * 60 * 1000 || !Array.isArray(state.messages)) {
        clearState()
        return
      }
      messages.value = state.messages
      hasSearched.value = !!state.hasSearched
      currentConversationId.value = typeof state.currentConversationId === 'string' ? state.currentConversationId : null
    } catch (e) {
      console.error('Failed to load search state:', e)
      clearState()
    }
  }
  function clearState() {
    if (statePersistTimer) {
      clearTimeout(statePersistTimer)
      statePersistTimer = null
    }
    try { localStorage.removeItem(stateStorageKey) } catch { /* 存储不可用 */ }
  }
  function saveCurrentConversation() {
    if (!hasSearched.value || messages.value.length === 0) return
    const convId = currentConversationId.value || generateId()
    if (!currentConversationId.value) currentConversationId.value = convId
    const next: SearchConversation<TMessage> = {
      id: convId,
      title: titleFromMessages(messages.value),
      messages: JSON.parse(JSON.stringify(messages.value)),
      timestamp: Date.now(),
      messageCount: messages.value.length,
    }
    const existingIndex = conversations.value.findIndex((c) => c.id === convId)
    if (existingIndex >= 0) conversations.value.splice(existingIndex, 1, next)
    else {
      if (conversations.value.length >= MAX_CONVERSATIONS) {
        storageWarning('历史记录已达到 50 条，请删除旧记录后再保存新会话；当前会话仍会单独保存。')
        return
      }
      conversations.value.unshift(next)
    }
    saveConversations()
  }
  function ensureCurrentConversationId(): string {
    if (!currentConversationId.value) currentConversationId.value = generateId()
    return currentConversationId.value
  }
  function loadConversation(id: string) {
    const conversation = conversations.value.find((c) => c.id === id)
    if (!conversation) return
    saveCurrentConversation()
    messages.value = JSON.parse(JSON.stringify(conversation.messages))
    hasSearched.value = true
    currentConversationId.value = id
    saveState()
  }
  function createNewConversation(saveCurrent = true) {
    if (saveCurrent && hasSearched.value && messages.value.length > 0) {
      if (!currentConversationId.value) currentConversationId.value = generateId()
      saveCurrentConversation()
    }
    messages.value = []
    hasSearched.value = false
    userInput.value = ''
    currentConversationId.value = null
    clearState()
  }
  function removeConversation(id: string) {
    const index = conversations.value.findIndex((c) => c.id === id)
    if (index < 0) return
    conversations.value.splice(index, 1)
    saveConversations()
    if (currentConversationId.value === id) createNewConversation(false)
  }
  function persistConversationAndState(immediate = false) {
    if (!hasSearched.value) return
    saveCurrentConversation()
    saveState(immediate)
    if (immediate) saveConversations(true)
  }
  function initFromStorage() {
    loadConversations()
    loadState()
  }
  function formatTime(timestamp: number): string {
    const date = new Date(timestamp)
    const now = new Date()
    if (date.toDateString() === now.toDateString()) {
      return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    }
    return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
  }
  function flushPending() {
    if (conversationsPersistTimer) flushConversationsPersist()
    if (statePersistTimer) flushStatePersist()
  }
  if (typeof window !== 'undefined') window.addEventListener('pagehide', flushPending)
  onScopeDispose(() => {
    flushPending()
    if (typeof window !== 'undefined') window.removeEventListener('pagehide', flushPending)
  })
  return {
    conversations,
    currentConversationId,
    saveCurrentConversation,
    ensureCurrentConversationId,
    saveState,
    persistConversationAndState,
    loadConversation,
    createNewConversation,
    removeConversation,
    initFromStorage,
    formatTime,
  }
}
