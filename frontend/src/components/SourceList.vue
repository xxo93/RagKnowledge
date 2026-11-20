<script setup>
// 引用来源容器：区头统计、全部展开/收起，以及多条来源的折叠管理。
import { computed, nextTick, ref } from 'vue'
import SourceCard from './SourceCard.vue'
import DocPreviewDialog from './DocPreviewDialog.vue'

const props = defineProps({
  sources: { type: Array, default: () => [] },
  query: { type: String, default: '' }
})

const expandedIds = ref(new Set())
const rootRef = ref(null)
const previewVisible = ref(false)
const previewDoc = ref(null)

const sourceKey = (source, index) => source.chunk_id ?? `${source.doc_id ?? source.doc_name}-${source.chunk_index}-${index}`

const isLowRelevance = (source) => {
  const score = Number(source.score || 0)
  return score > 0 && score < 0.5
}

// 低相关条目排到末尾；展示序号保留原始命中顺序，便于与正文角标对应
const orderedSources = computed(() => {
  const entries = props.sources.map((source, index) => ({ source, index }))
  const high = entries.filter(({ source }) => !isLowRelevance(source))
  const low = entries.filter(({ source }) => isLowRelevance(source))
  return [...high, ...low]
})

const allExpanded = computed(() => props.sources.length > 0 && expandedIds.value.size === props.sources.length)

// 联网兜底时来源全是网页，标题口径随之切换，避免叫成"引用文档"
const allWeb = computed(() => props.sources.every((s) => s.source_type === 'web'))
const headLabel = computed(() => (allWeb.value ? `🌐 联网来源 · ${props.sources.length}` : `📄 引用来源 · ${props.sources.length}`))

function toggleSource(entry) {
  const key = sourceKey(entry.source, entry.index)
  const next = new Set(expandedIds.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedIds.value = next
}

function openPreview(source) {
  if (!source?.doc_id) return
  previewDoc.value = { doc_id: source.doc_id, doc_name: source.doc_name }
  previewVisible.value = true
}

function toggleAll() {
  if (allExpanded.value) {
    expandedIds.value = new Set()
  } else {
    expandedIds.value = new Set(orderedSources.value.map((e) => sourceKey(e.source, e.index)))
  }
}

async function focusSource(originalIndex) {
  const entry = orderedSources.value.find((e) => e.index === originalIndex)
  if (!entry) return
  const key = sourceKey(entry.source, entry.index)
  const next = new Set(expandedIds.value)
  next.add(key)
  expandedIds.value = next
  await nextTick()
  const el = rootRef.value?.querySelector(`[data-original-index="${originalIndex}"]`)
  el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  el?.classList.add('cite-focus')
  setTimeout(() => el?.classList.remove('cite-focus'), 1600)
}

defineExpose({ focusSource })
</script>

<template>
  <div ref="rootRef" class="source-list">
    <div class="source-list-head">
      <span class="source-title">{{ headLabel }}</span>
      <el-button link size="small" @click="toggleAll">
        {{ allExpanded ? '全部收起' : '全部展开' }}
      </el-button>
    </div>
    <div class="source-items">
      <SourceCard
        v-for="entry in orderedSources"
        :key="sourceKey(entry.source, entry.index)"
        :source="entry.source"
        :index="entry.index"
        :query="props.query"
        :data-original-index="entry.index"
        :expanded="expandedIds.has(sourceKey(entry.source, entry.index))"
        @toggle="toggleSource(entry)"
        @preview="openPreview"
      />
    </div>

    <DocPreviewDialog v-model="previewVisible" :doc="previewDoc || {}" />
  </div>
</template>

<style scoped>
.source-list {
  margin-top: 8px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}

.source-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.source-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-muted);
}

.source-items {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.source-card.cite-focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-light);
}
</style>
