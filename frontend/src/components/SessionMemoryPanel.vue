<script setup>
// 会话记忆面板：摘要、事实、上下文用量、临时会话开关、清空记忆。
// 数据按会话隔离；所有写操作失败时保留原状态并提示，不做乐观更新。
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  clearSessionMemory,
  deleteMemoryFact,
  getFactHistory,
  getSessionMemory,
  updateMemorySettings
} from '../api/sessionMemory'
import MemoryItem from './MemoryItem.vue'

const props = defineProps({
  sessionId: { type: [Number, null], default: null }
})

const loading = ref(false)
const saving = ref(false)
const clearing = ref(false)
const memory = ref(null)
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyItems = ref([])
const historyFact = ref(null)

const usage = computed(() => memory.value?.context_usage || {})
const facts = computed(() => memory.value?.facts || [])
const enabled = computed(() => memory.value?.memory_enabled !== false)
const percent = computed(() => {
  const budget = Number(usage.value.budget_tokens) || 0
  const total = Number(usage.value.total_tokens) || 0
  if (!budget) return 0
  return Math.min(100, Math.round((total / budget) * 100))
})
const breakdown = computed(() => [
  { label: '事实', value: usage.value.facts_tokens || 0, color: 'var(--el-color-warning)' },
  { label: '摘要', value: usage.value.summary_tokens || 0, color: 'var(--el-color-primary)' },
  { label: '原文', value: usage.value.history_tokens || 0, color: 'var(--el-color-success)' }
])

async function load() {
  if (!props.sessionId) {
    memory.value = null
    return
  }
  loading.value = true
  try {
    memory.value = await getSessionMemory(props.sessionId)
  } catch (_) {
    memory.value = null
  } finally {
    loading.value = false
  }
}

async function onToggle(value) {
  if (!props.sessionId) return
  saving.value = true
  try {
    const updated = await updateMemorySettings(props.sessionId, value)
    memory.value = { ...(memory.value || {}), ...updated }
    ElMessage.success(value ? '已恢复本会话的长期记忆' : '已切换为临时会话（不再记忆）')
    await load()
  } catch (_) {
    ElMessage.error('记忆开关保存失败，请稍后重试')
  } finally {
    saving.value = false
  }
}

async function onRemoveFact(item) {
  try {
    await ElMessageBox.confirm(
      `确认删除记忆「${item.fact_key}」？删除后该信息不再参与后续问答。`,
      '删除记忆确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch (_) {
    return
  }
  try {
    await deleteMemoryFact(props.sessionId, item.mem_id)
    ElMessage.success('记忆已删除')
    await load()
  } catch (_) {
    ElMessage.error('删除失败，请稍后重试')
  }
}

async function onHistory(item) {
  if (!props.sessionId || !item?.mem_id) return
  historyFact.value = item
  historyItems.value = []
  historyVisible.value = true
  historyLoading.value = true
  try {
    const data = await getFactHistory(props.sessionId, item.mem_id)
    historyItems.value = data?.history || []
  } catch (_) {
    ElMessage.error('历史版本加载失败，请稍后重试')
  } finally {
    historyLoading.value = false
  }
}

function formatTime(value) {
  if (!value) return '—'
  const d = new Date(String(value).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return String(value)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

async function onClear() {
  try {
    await ElMessageBox.confirm(
      '确认清空本会话的全部长期记忆？摘要与会话事实都会被清除，历史消息不受影响。',
      '清空会话记忆',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' }
    )
  } catch (_) {
    return
  }
  clearing.value = true
  try {
    await clearSessionMemory(props.sessionId)
    ElMessage.success('会话记忆已清空')
    await load()
  } catch (_) {
    ElMessage.error('清空失败，请稍后重试')
  } finally {
    clearing.value = false
  }
}

watch(() => props.sessionId, load, { immediate: true })

defineExpose({ refresh: load })
</script>

<template>
  <div class="memory-panel" v-loading="loading">
    <div class="panel-head">
      <div>
        <div class="title">会话记忆</div>
        <div class="subtitle">长期记忆只在本会话内生效，不影响其他会话</div>
      </div>
      <div class="head-actions">
        <el-switch
          :model-value="enabled"
          :loading="saving"
          :disabled="!sessionId"
          active-text="记忆"
          inactive-text="临时"
          inline-prompt
          @update:model-value="onToggle"
        />
        <el-button
          link
          type="danger"
          size="small"
          :disabled="!sessionId || clearing"
          :loading="clearing"
          @click="onClear"
        >清空</el-button>
      </div>
    </div>

    <el-empty v-if="!sessionId" description="先选择一个会话" :image-size="56" />
    <template v-else>
      <div class="usage-card">
        <div class="usage-head">
          <span>上下文用量</span>
          <span class="usage-num">
            {{ usage.total_tokens || 0 }} / {{ usage.budget_tokens || 0 }} tokens
          </span>
        </div>
        <el-progress :percentage="percent" :show-text="false" :stroke-width="6" />
        <div class="breakdown">
          <span v-for="b in breakdown" :key="b.label" class="breakdown-item">
            <i class="dot" :style="{ background: b.color }"></i>{{ b.label }} {{ b.value }}
          </span>
          <span class="breakdown-item muted">原文 {{ usage.history_rounds || 0 }}/{{ usage.window_rounds || 0 }} 轮</span>
        </div>
      </div>

      <div v-if="!enabled" class="temp-tip">
        当前是临时会话：不注入历史事实与摘要，也不再自动抽取记忆。
      </div>

      <div class="section">
        <div class="section-title">滚动摘要</div>
        <div v-if="memory?.summary" class="summary">{{ memory.summary }}</div>
        <div v-else class="empty-line">还没有摘要；对话达到触发轮数后自动生成。</div>
      </div>

      <div class="section">
        <div class="section-title">
          会话事实
          <span class="count">{{ facts.length }}</span>
        </div>
        <div v-if="facts.length" class="fact-list">
          <MemoryItem
            v-for="item in facts"
            :key="item.mem_id"
            :item="item"
            @remove="onRemoveFact"
            @history="onHistory"
          />
        </div>
        <div v-else class="empty-line">还没有沉淀事实；明确表达的口径、项目名、待办会被记住。</div>
      </div>
    </template>

    <el-dialog
      v-model="historyVisible"
      :title="`口径版本历史 · ${historyFact?.fact_key || ''}`"
      width="92%"
      style="max-width: 520px"
      append-to-body
      class="memory-history-dialog"
    >
      <div v-loading="historyLoading" class="history-body">
        <el-empty v-if="!historyLoading && !historyItems.length" description="暂无历史版本" :image-size="52" />
        <ol v-else class="history-list">
          <li v-for="row in historyItems" :key="row.history_id" class="history-item">
            <div class="history-time">{{ formatTime(row.changed_at) }}</div>
            <div class="history-values">
              <span class="old-value">{{ row.old_value }}</span>
              <span class="arrow">→</span>
              <span class="new-value">{{ row.new_value }}</span>
            </div>
          </li>
        </ol>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.memory-panel {
  padding: 4px 2px 16px;
  min-height: 200px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}

.subtitle {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-secondary);
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.usage-card {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--primary) 4%, var(--panel));
}

.usage-head {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.usage-num {
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-secondary);
}

.breakdown-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.breakdown-item.muted {
  opacity: 0.85;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}

.temp-tip {
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  font-size: 12px;
  line-height: 18px;
}

.section {
  margin-top: 16px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 8px;
}

.count {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-secondary);
  background: var(--border);
  border-radius: 9px;
  padding: 1px 7px;
}

.summary {
  padding: 10px 12px;
  border-left: 3px solid var(--primary);
  border-radius: var(--radius-sm);
  background: var(--primary-light);
  color: var(--text);
  font-size: 13px;
  line-height: 20px;
  white-space: pre-wrap;
  word-break: break-word;
}

.empty-line {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 18px;
}

.history-body {
  min-height: 80px;
}

.history-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.history-item {
  padding: 10px 0;
  border-bottom: 1px dashed var(--border);
}

.history-item:last-child {
  border-bottom: none;
}

.history-time {
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.history-values {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 13px;
  line-height: 19px;
  word-break: break-word;
}

.old-value {
  color: var(--text-secondary);
  text-decoration: line-through;
}

.arrow {
  color: var(--text-muted);
}

.new-value {
  color: var(--text);
}
</style>
