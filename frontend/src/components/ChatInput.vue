<script setup>
// 输入框：自适应高度、字数限制、快捷键发送；流式中允许继续输入，仅禁用发送。
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  streaming: { type: Boolean, default: false },
  sessionId: { type: [Number, String, null], default: null },
  draft: { type: String, default: '' }
})

const emit = defineEmits(['send', 'stop', 'update:draft'])
const MAX_LEN = 500
const SOFT_LEN = 400
const text = ref(props.draft || '')
const inputRef = ref(null)

watch(
  () => props.draft,
  (value) => {
    if (value !== text.value) text.value = value
  }
)

watch(text, (value) => emit('update:draft', value))

const overLimit = computed(() => text.value.length > MAX_LEN)
const nearLimit = computed(() => !overLimit.value && text.value.length >= SOFT_LEN)
const sendDisabled = computed(() => props.streaming || !text.value.trim() || overLimit.value)

async function submit() {
  if (sendDisabled.value) return
  const query = text.value.trim()
  emit('send', query)
  text.value = ''
  emit('update:draft', '')
  await nextTick()
  inputRef.value?.focus?.()
}

function onKeydown(e) {
  const isEnter = e.key === 'Enter'
  const isModifier = e.ctrlKey || e.metaKey
  if (isEnter && (isModifier || !e.shiftKey)) {
    e.preventDefault()
    submit()
  }
}

function onPaste(e) {
  const pasted = e.clipboardData?.getData('text') || ''
  if (pasted.length > MAX_LEN) {
    ElMessage.warning(`粘贴内容超过 ${MAX_LEN} 字，超出部分不会发送`)
  } else if (pasted.length > 200) {
    ElMessage.info('长文本已保留在输入框，可继续编辑后发送')
  }
}
</script>

<template>
  <div class="input-bar">
    <el-input
      ref="inputRef"
      v-model="text"
      type="textarea"
      :autosize="{ minRows: 1, maxRows: 6 }"
      resize="none"
      placeholder="输入问题，Enter 发送，Shift+Enter 换行"
      :maxlength="MAX_LEN"
      :aria-label="'问题输入框'"
      @keydown="onKeydown"
      @paste="onPaste"
    />
    <div class="input-footer">
      <span
        class="char-count"
        :class="{ over: overLimit, near: nearLimit }"
        :title="nearLimit ? `已接近 ${MAX_LEN} 字上限，请精简问题` : ''"
      >{{ text.length }} / {{ MAX_LEN }}</span>
      <div class="actions">
        <el-button v-if="streaming" type="danger" aria-label="停止生成" @click="emit('stop')">停止生成</el-button>
        <el-button v-else type="primary" :disabled="sendDisabled" aria-label="发送问题" @click="submit">发送</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.input-bar {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}

.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}

.char-count {
  font-size: 12px;
  color: var(--text-secondary);
}

.char-count.near {
  color: var(--warning);
}

.char-count.over {
  color: var(--danger);
}

.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
