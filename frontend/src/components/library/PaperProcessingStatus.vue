<script setup lang="ts">
import { computed } from 'vue'
import type { PaperProcessingJob } from '@/services/api/papers'
const props = defineProps<{ job?: PaperProcessingJob }>()
const emit = defineEmits<{ retry: [paperId: number] }>()
const label = computed(() => {
  if (props.job?.status === 'running') return ({ classify: '分类中', pdf: '下载中', graph: '构建图谱' } as Record<string, string>)[props.job.phase] || '处理中'
  return ({ queued: '等待处理', failed: '处理失败', completed: '已完成' } as Record<string, string>)[props.job?.status || ''] || '—'
})
</script>

<template>
  <span @click.stop>
    <a-tooltip :title="job?.error || undefined"><span>{{ label }}</span></a-tooltip>
    <a-button v-if="job?.status === 'failed'" type="link" size="small" @click="emit('retry', job.paper_id)">重试</a-button>
  </span>
</template>
