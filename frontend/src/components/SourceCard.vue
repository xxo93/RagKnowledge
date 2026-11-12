<script setup>
// 单条引用来源：折叠态展示标题/摘要/分数，展开态展示相似片段。
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { extractHighlightTerms, highlightText } from '../utils/highlight.js'

const props = defineProps({
  source: { type: Object, default: () => ({}) },
  index: { type: Number, default: 0 },
  expanded: { type: Boolean, default: false },
  query: { type: String, default: '' }
})

const emit = defineEmits(['toggle'])

function formatDocName(name) {
  return (name || '未命名文档').replace(/\.[A-Za-z0-9]+$/, '').replace(/_/g, ' ').trim() || '未命名文档'
}

const docTitle = computed(() => props.source.doc_title ?? formatDocName(props.source.doc_name))
const docSummary = computed(() => props.source.doc_summary ?? '')
const summary = computed(() => props.source.summary ?? props.source.snippet ?? '')
const excerpt = computed(() => props.source.excerpt ?? props.source.snippet ?? '')
const highlightTerms = computed(() => extractHighlightTerms(props.query))
const docSummaryHtml = computed(() => highlightText(docSummary.value, highlightTerms.value))
const summaryHtml = computed(() => highlightText(summary.value, highlightTerms.value))
const excerptHtml = computed(() => highlightText(excerpt.value, highlightTerms.value))
const score = computed(() => Number(props.source.score || 0))
// 联网来源没有相似度分数，用展示标签代替，避免出现"相似度 0.00"的误导
const isWeb = computed(() => props.source.source_type === 'web')
const isLowRelevance = computed(() => !isWeb.value && score.value > 0 && score.value < 0.5)

const detailId = computed(() => `source-detail-${props.index}`)
const scoreText = computed(() => score.value.toFixed(2))
const scoreType = computed(() => {
  if (score.value >= 0.8) return 'success'
  if (score.value >= 0.6) return 'primary'
  return 'info'
})

async function copyExcerpt() {
  try {
    await navigator.clipboard.writeText(excerpt.value)
    ElMessage.success('片段已复制')
  } catch {
    ElMessage.warning('当前浏览器不支持自动复制')
  }
}

async function copyLink() {
  try {
    await navigator.clipboard.writeText(props.source.url || '')
    ElMessage.success('链接已复制')
  } catch {
    ElMessage.warning('当前浏览器不支持自动复制')
  }
}
</script>

<template>
  <div class="source-card" :class="{ expanded, 'low-relevance': isLowRelevance }">
    <button
      type="button"
      class="source-head"
      :aria-expanded="expanded"
      :aria-controls="detailId"
      @click="emit('toggle')"
    >
      <span class="source-index">{{ index + 1 }}</span>
      <span class="source-title" :title="docTitle">{{ docTitle }}</span>
      <el-tag v-if="isWeb" size="small" type="warning" effect="plain">联网</el-tag>
      <el-tag v-else-if="isLowRelevance" size="small" type="info">低相关</el-tag>
      <span class="source-scores">
        <el-tag v-if="source.rerank_score != null" size="small" type="success">
          重排 {{ Number(source.rerank_score).toFixed(2) }}
        </el-tag>
        <el-tag v-if="!isWeb" size="small" :type="scoreType">相似度 {{ scoreText }}</el-tag>
        <span class="expand-icon">{{ expanded ? '▴' : '▸' }}</span>
      </span>
    </button>

    <div v-if="!expanded" class="source-summary-wrap">
      <div v-if="docSummary" class="source-summary doc-summary">文档：<span v-html="docSummaryHtml"></span></div>
      <div v-if="summary" class="source-summary">
        {{ isWeb ? '摘要：' : '片段：' }}<span v-html="summaryHtml"></span>
      </div>
    </div>

    <div v-if="expanded" :id="detailId" class="source-detail">
      <div v-if="isWeb && source.url" class="source-url">
        链接：<a :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.url }}</a>
      </div>
      <div v-if="docSummary" class="doc-summary-line">文档摘要：<span v-html="docSummaryHtml"></span></div>
      <div v-if="!isWeb" class="source-meta">
        片段 #{{ source.chunk_index ?? '-' }}
        <span v-if="source.chunk_total"> · 共 {{ source.chunk_total }} 片</span>
      </div>
      <div class="source-excerpt" v-html="excerptHtml"></div>
      <div class="source-actions">
        <el-button link size="small" @click="copyExcerpt">复制{{ isWeb ? '摘要' : '片段' }}</el-button>
        <el-button v-if="isWeb && source.url" link size="small" @click="copyLink">复制链接</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.source-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
  padding: 8px 10px;
}

.source-card.low-relevance {
  opacity: 0.82;
}

.source-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  border: none;
  background: transparent;
  padding: 0;
  cursor: pointer;
  text-align: left;
}

.source-index {
  flex: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--primary-light);
  color: var(--primary);
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.source-title {
  flex: 1;
  min-width: 0;
  font-weight: 600;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-scores {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
}

.expand-icon {
  color: var(--text-secondary);
}

.source-summary {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.source-detail {
  margin-top: 6px;
}

.doc-summary-line {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.source-meta {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.source-url {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 4px;
  word-break: break-all;
}

.source-url a {
  color: var(--primary);
  text-decoration: none;
}

.source-url a:hover {
  text-decoration: underline;
}

.source-excerpt {
  font-size: 12px;
  color: var(--text-muted);
  white-space: pre-wrap;
  background: var(--surface-muted);
  border-radius: var(--radius-sm);
  padding: 8px;
  max-height: 180px;
  overflow-y: auto;
}

.source-actions {
  margin-top: 4px;
}

.hit-mark {
  background: var(--primary-light);
  color: inherit;
  border-radius: 2px;
  padding: 0 1px;
}
</style>
