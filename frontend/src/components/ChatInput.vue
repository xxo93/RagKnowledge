<script setup>
// 底部输入框（P-02）：Enter 发送、Shift+Enter 换行；请求中禁用发送并显示中断按钮（F-08）。
import { ref } from 'vue'

const props = defineProps({ streaming: { type: Boolean, default: false } })
const emit = defineEmits(['send', 'stop'])
const text = ref('')

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}
function submit() {
  const q = text.value.trim()
  if (!q || props.streaming) return
  emit('send', q)
  text.value = ''
}
</script>

<template>
  <div class="input-bar">
    <el-input
      v-model="text"
      type="textarea"
      :rows="3"
      resize="none"
      placeholder="输入问题，Enter 发送，Shift+Enter 换行"
      :disabled="streaming"
      @keydown="onKeydown"
    />
    <div class="actions">
      <el-button v-if="streaming" type="danger" @click="emit('stop')">中断</el-button>
      <el-button v-else type="primary" :disabled="!text.trim()" @click="submit">发送</el-button>
    </div>
  </div>
</template>

<style scoped>
.input-bar { padding: 12px 16px; border-top: 1px solid #eee; }
.actions { display: flex; justify-content: flex-end; margin-top: 8px; }
</style>
