<script setup>
// 流式/请求失败卡片：独立展示错误信息、错误码、trace_id 与重试入口。
// v3 / WP3 §5.6：错误提示必须带 trace_id，并可一键复制排障信息（trace + 时间 + 用户）。
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  message: { type: Object, required: true },
  onRetry: { type: Function, default: null },
  currentUser: { type: Object, default: null }
})

const copied = ref(false)

function retry() {
  if (props.message.streaming) return
  props.onRetry?.(props.message)
}

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN', { hour12: false })
}

async function copyDiagnostics() {
  const user = props.currentUser
  const text = [
    `trace_id: ${props.message.traceId || '—'}`,
    `时间: ${fmtTime(props.message.createdAt)}`,
    `用户: ${user?.display_name || user?.username || '—'}（${user?.user_id ?? '—'}）`,
    `错误: ${props.message.errorCode || '—'} ${props.message.errorMsg || ''}`.trim(),
    `问题: ${props.message.query || '—'}`
  ].join('\n')
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    ElMessage.success('排障信息已复制')
    setTimeout(() => { copied.value = false }, 1500)
  } catch {
    ElMessage.warning('当前浏览器不支持自动复制')
  }
}
</script>

<template>
  <div class="error-card" role="alert">
    <div class="error-title">回答生成失败</div>
    <div class="error-msg">{{ message.errorMsg || '请求失败，请稍后重试' }}</div>
    <div v-if="message.errorCode" class="error-code">错误码：{{ message.errorCode }}</div>
    <div v-if="message.traceId" class="error-trace">
      <span>trace_id：</span>
      <code>{{ message.traceId }}</code>
    </div>
    <div class="error-actions">
      <el-button
        v-if="message.query"
        link
        type="primary"
        size="small"
        :disabled="message.streaming"
        @click="retry"
      >重试原问题</el-button>
      <el-button v-if="message.traceId" link size="small" @click="copyDiagnostics">
        {{ copied ? '已复制 ✓' : '复制排障信息' }}
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.error-card {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--panel);
  border: 1px solid var(--danger-light);
}

.error-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--danger);
}

.error-msg {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text);
}

.error-code {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-secondary);
}

.error-trace {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-secondary);
  min-width: 0;
}

.error-trace code {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--surface-muted);
}

.error-actions {
  display: flex;
  gap: 10px;
  margin-top: 6px;
}
</style>
