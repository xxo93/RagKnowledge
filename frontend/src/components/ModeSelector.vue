<script setup>
// 输入框左侧模式选择器（v3 / WP2）：默认「自动」，选择结果存 sessionStorage。
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: 'auto' },
  disabled: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue'])

const MODE_OPTIONS = [
  { value: 'auto', label: '自动', tip: '按问题意图自动路由：知识库 / 业务库 / 工具 / 通用', badge: '' },
  { value: 'knowledge', label: '仅知识库', tip: '只查企业内部知识库；不生成代码、不做通用创作', badge: 'knowledge' },
  { value: 'general', label: '仅通用', tip: '只用通用模型回答，不检索内部资料，并明确声明未使用内部资料', badge: 'general' },
  { value: 'biz', label: '业务库', tip: '直接查业务库（订单/库存/客户/考勤/薪资），结果脱敏', badge: 'biz' },
  { value: 'workflow', label: '任务', tip: '多步骤任务编排：先生成计划，确认后逐步执行，可暂停/取消', badge: 'workflow' }
]

const current = computed(() => MODE_OPTIONS.find((x) => x.value === props.modelValue) || MODE_OPTIONS[0])

function onChange(value) {
  emit('update:modelValue', value)
}
</script>

<template>
  <el-dropdown trigger="click" :disabled="disabled" class="mode-selector" @command="onChange">
    <button type="button" class="mode-trigger" :disabled="disabled" aria-label="选择回答模式">
      <span class="mode-dot" :class="`tone-${current.badge || 'auto'}`"></span>
      {{ current.label }}
      <span class="caret" aria-hidden="true">▾</span>
    </button>
    <template #dropdown>
      <el-dropdown-menu class="mode-menu">
        <el-dropdown-item
          v-for="item in MODE_OPTIONS"
          :key="item.value"
          :command="item.value"
          :class="{ selected: item.value === modelValue }"
        >
          <span class="opt-row">
            <span class="opt-label">{{ item.label }}</span>
            <span class="opt-tip">{{ item.tip }}</span>
          </span>
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>
<style scoped>
.mode-trigger {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 8px);
  background: var(--panel);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 18px;
  cursor: pointer;
}

.mode-trigger:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--text);
}

.mode-trigger:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.mode-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #9aa3b2;
}

.mode-dot.tone-knowledge { background: #2f6bff; }
.mode-dot.tone-general { background: #6b7280; }
.mode-dot.tone-biz { background: #0f9d58; }
.mode-dot.tone-workflow { background: #7c3aed; }

.caret {
  font-size: 10px;
  transform: translateY(-1px);
}

.mode-menu {
  min-width: 250px;
}

.opt-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px 0;
}

.opt-label {
  font-size: 13px;
  color: var(--text);
}

.opt-tip {
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: normal;
}

.selected .opt-label {
  color: var(--primary);
  font-weight: 600;
}
</style>
