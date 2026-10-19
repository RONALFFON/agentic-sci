import { ref, watch, onScopeDispose, type Ref } from 'vue'
import { getPaperProcessing, retryPaperProcessing, type PaperProcessingJob } from '@/services/api/papers'
import type { Paper } from '@/types'

export function usePaperProcessing(papers: Ref<Paper[]>, onCompleted: () => void) {
  const jobs = ref<Record<number, PaperProcessingJob>>({})
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  let disposed = false
  const ids = () => papers.value.map(p => p.id).filter((id): id is number => typeof id === 'number' && id > 0).slice(0, 100)
  async function refresh() {
    clearTimeout(timer)
    controller?.abort()
    const request = new AbortController()
    controller = request
    const visible = ids()
    if (!visible.length || disposed) { jobs.value = {}; return }
    try {
      const data = await getPaperProcessing(visible, request.signal)
      if (request.signal.aborted || disposed) return
      const completed = data.some(job => ['completed', 'failed'].includes(job.status) &&
        ['queued', 'running'].includes(jobs.value[job.paper_id]?.status))
      jobs.value = Object.fromEntries(data.map(job => [job.paper_id, job]))
      if (completed) onCompleted()
      if (data.some(job => ['queued', 'running'].includes(job.status))) timer = setTimeout(refresh, 3000)
    } catch {
      if (!request.signal.aborted && !disposed) timer = setTimeout(refresh, 10000)
    }
  }
  async function retry(id: number) {
    await retryPaperProcessing(id)
    await refresh()
  }
  watch(() => ids().join(','), refresh, { immediate: true })
  onScopeDispose(() => { disposed = true; clearTimeout(timer); controller?.abort() })
  return { jobs, retry }
}
