<script setup>
// 流式/请求失败卡片：独立展示错误信息、错误码与重试入口。
const props = defineProps({
  message: { type: Object, required: true },
  onRetry: { type: Function, default: null }
})

function retry() {
  if (props.message.streaming) return
  props.onRetry?.(props.message)
}
</script>

<template>
  <div class="error-card" role="alert">
    <div class="error-title">回答生成失败</div>
    <div class="error-msg">{{ message.errorMsg || '请求失败，请稍后重试' }}</div>
    <div v-if="message.errorCode" class="error-code">错误码：{{ message.errorCode }}</div>
    <el-button
      v-if="message.query"
      link
      type="primary"
      size="small"
      :disabled="message.streaming"
      @click="retry"
    >重试原问题</el-button>
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
</style>
