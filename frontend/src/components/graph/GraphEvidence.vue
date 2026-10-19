<script setup lang="ts">
import { computed } from 'vue'
import { getLibraryPdfHref } from '@/services/api/papers'
import type { components } from '@/types/openapi'
const props = defineProps<{ evidence?: string; details?: components['schemas']['GraphEvidence'] | null }>()
const kindLabel = computed(() => ({
  user_edited: '人工编辑', text_anchored_model_inference: '附原文引用的模型推断', model_inference: '模型推断',
} as Record<string, string>)[props.details?.kind || ''] || '未记录来源')
const pdfHref = computed(() => {
  const id = props.details?.source_paper_id
  return id ? `${getLibraryPdfHref(id)}#search=${encodeURIComponent(props.details?.quote || '')}` : ''
})
</script>

<template>
  <section v-if="evidence || details" class="graph-evidence">
    <strong>关系依据</strong>
    <a-tag>{{ kindLabel }}</a-tag>
    <p v-if="evidence">{{ evidence }}</p>
    <blockquote v-if="details?.quote">{{ details.quote }}</blockquote>
    <p v-if="details?.location && details.location !== 'unspecified'">原文位置：{{ details.location }}</p>
    <RouterLink v-if="details?.source_paper_id" :to="`/library/read/${details.source_paper_id}`">阅读证据来源论文</RouterLink>
    <a v-if="details?.source_version && pdfHref" :href="pdfHref" target="_blank" rel="noopener noreferrer">打开 PDF 核对引用</a>
  </section>
</template>

<style scoped>
.graph-evidence { overflow-wrap: anywhere; }
.graph-evidence p { white-space: pre-wrap; margin: 8px 0; }
.graph-evidence blockquote { margin: 8px 0; padding-left: 10px; border-left: 3px solid #91caff; white-space: pre-wrap; }
.graph-evidence a { display: block; margin: 8px 0; }
</style>
