import { apiClient } from './client'
import type { Paper } from '@/types'
type PaperReaderChatTurn = { role: string; content: string }
const PAPER_READER_CHAT_REQUEST_MS = 240000
export async function postPaperReaderOpening(paperId: number, signal?: AbortSignal): Promise<{ success: boolean; opening: string; pdf_parsing?: boolean }> {
  const response = await apiClient.post('/api/ai/paper-reader/opening', { paper_id: paperId }, { signal })
  return response.data
}
export async function postPaperReaderChat(body: {
  paper_id: number; messages: PaperReaderChatTurn[]; user_message: string
}, signal?: AbortSignal): Promise<{
  success: boolean; reply: string; pdf_parsing?: boolean; related_papers?: Paper[]; related_hints?: any[]; kg_edges?: any[]
}> {
  const response = await apiClient.post('/api/ai/paper-reader/chat', body, {
    timeout: PAPER_READER_CHAT_REQUEST_MS,
    signal,
  })
  return response.data
}
export async function getPaperReaderHistory(paperId: number, limit = 200, signal?: AbortSignal): Promise<{
  success: boolean; paper_id: number; turns: { role: string; content: string; created_at: number }[]
}> {
  const response = await apiClient.get('/api/ai/paper-reader/history', { params: { paper_id: paperId, limit }, signal })
  return response.data
}
