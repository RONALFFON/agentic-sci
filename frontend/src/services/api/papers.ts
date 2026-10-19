import { apiClient } from './client'
import type { Paper, PapersResponse, LibraryCategoriesResponse, SavePapersResponse } from '@/types'
export async function getLibrary(limit = 50, params?: {
  q?: string; year_from?: number; year_to?: number
  read_status?: string; tags?: string; category?: string; offset?: number
}, signal?: AbortSignal): Promise<PapersResponse> {
  const response = await apiClient.get<PapersResponse>('/api/papers/library', { params: { limit, ...params }, signal })
  return response.data
}
export async function getLibraryCategoryFolders(): Promise<LibraryCategoriesResponse> {
  const response = await apiClient.get<LibraryCategoriesResponse>('/api/papers/library/categories')
  return response.data
}
export function getLibraryPdfHref(paperId: number): string {
  const base = (apiClient.defaults.baseURL || '').replace(/\/$/, '')
  return `${base}/api/papers/${paperId}/library-pdf`
}
export async function getPaper(id: number, signal?: AbortSignal): Promise<Paper> {
  const response = await apiClient.get<Paper>(`/api/papers/${id}`, { signal })
  return response.data
}
export async function savePapers(papers: Paper[], options?: {
  download_pdfs?: boolean; llm_classify?: boolean
}): Promise<SavePapersResponse> {
  const payload = { papers, download_pdfs: options?.download_pdfs ?? true, llm_classify: options?.llm_classify ?? true }
  const response = await apiClient.post<SavePapersResponse>('/api/papers/save', payload, { timeout: 30000 })
  return response.data
}
export async function deletePaper(id: number): Promise<void> {
  await apiClient.delete(`/api/papers/${id}`)
}
export interface PaperProcessingJob {
  paper_id: number
  status: 'queued' | 'running' | 'completed' | 'failed'
  phase: string
  attempts: number
  error?: string | null
}
export async function getPaperProcessing(ids: number[], signal?: AbortSignal): Promise<PaperProcessingJob[]> {
  const res = await apiClient.get('/api/papers/processing/status', { params: { ids: ids.join(',') }, signal })
  return res.data.jobs
}
export async function retryPaperProcessing(id: number): Promise<void> {
  await apiClient.post(`/api/papers/processing/${id}/retry`)
}
export async function postReadingLog(body: {
  paper_id: number; duration_sec: number; client_ts?: number
}): Promise<{ success: boolean }> {
  const response = await apiClient.post('/api/papers/reading/log', body)
  return response.data
}
export async function getReadingCalendar(days = 180): Promise<{
  success: boolean; days: number; items: { date: string; seconds: number; sessions: number }[]
}> {
  const response = await apiClient.get('/api/papers/reading/calendar', { params: { days } })
  return response.data
}
