<script setup>
// 单条会话事实：类型徽标 + 置信度 + 删除；删除只影响本条 active 记录（后端软失效）
import { computed } from 'vue'

const KIND_META = {
  constraint: { label: '约束', type: 'warning' },
  fact: { label: '事实', type: 'primary' },
  entity: { label: '实体', type: 'success' },
  todo: { label: '待办', type: 'danger' }
}

const props = defineProps({
  item: { type: Object, required: true }
})
const emit = defineEmits(['remove', 'history'])

const meta = computed(() => KIND_META[props.item.kind] || { label: props.item.kind, type: 'info' })
const confidence = computed(() => {
  const c = Number(props.item.confidence)
  return Number.isFinite(c) ? Math.round(c * 100) : null
})

// M8 / WP5-8：同 key 被改写时，后端带回上一版值；面板显式提示「口径已变更：旧 → 新」
const previousValue = computed(() => {
  const v = props.item.previous_value
  return v == null || v === '' ? null : String(v)
})
const changeText = computed(
  () => (previousValue.value ? `口径已变更：${previousValue.value} → ${props.item.fact_value}` : '')
)
</script>

<template>
  <div class="memory-item">
    <div class="row">
      <el-tag size="small" :type="meta.type" effect="light" round>{{ meta.label }}</el-tag>
      <span class="key" :title="item.fact_key">{{ item.fact_key }}</span>
      <span v-if="confidence != null" class="confidence" :title="`置信度 ${confidence}%`">
        {{ confidence }}%
      </span>
      <el-button
        class="remove"
        link
        size="small"
        type="danger"
        :aria-label="`删除记忆 ${item.fact_key}`"
        @click="emit('remove', item)"
      >删除</el-button>
    </div>
    <div class="value">{{ item.fact_value }}</div>
    <div v-if="previousValue" class="change-note" :title="changeText">
      <el-tag
        size="small"
        type="warning"
        effect="plain"
        class="history-tag"
        role="button"
        tabindex="0"
        @click="emit('history', item)"
        @keydown.enter.prevent="emit('history', item)"
      >口径已变更 · 查看历史</el-tag>
      <span class="change-text" :title="changeText">{{ item.previous_value }} → {{ item.fact_value }}</span>
    </div>
  </div>
</template>

<style scoped>
.memory-item {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--panel);
}

.memory-item + .memory-item {
  margin-top: 8px;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.key {
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.confidence {
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.remove {
  opacity: 0;
  transition: opacity 0.15s ease;
}

.memory-item:hover .remove,
.memory-item:focus-within .remove {
  opacity: 1;
}

.value {
  margin-top: 4px;
  font-size: 13px;
  line-height: 19px;
  color: var(--text);
  word-break: break-word;
}

.change-note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  background: var(--warning-light, #fff7e6);
  border-left: 3px solid var(--warning);
}

.change-text {
  min-width: 0;
  font-size: 12px;
  color: var(--warning);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-tag {
  cursor: pointer;
}
</style>
