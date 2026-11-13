<script setup>
// 输入框：多行自适应（最多 8 行）、字数限制、快捷键发送、`/` 命令面板。
// 流式中允许继续输入，仅禁用发送。
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { filterCommands } from '../config/chatCommands'
import ModeSelector from './ModeSelector.vue'

const props = defineProps({
  streaming: { type: Boolean, default: false },
  sessionId: { type: [Number, String, null], default: null },
  draft: { type: String, default: '' },
  // v3 / WP2：回答模式（auto / knowledge / general / biz / workflow）
  mode: { type: String, default: 'auto' }
})

const emit = defineEmits(['send', 'stop', 'update:draft', 'update:mode'])
const MAX_LEN = 500
const SOFT_LEN = 400
// 8 行：业务库查询模板本身就带日期区间和筛选条件，6 行容易滚动到看不见开头
const MAX_ROWS = 8
const text = ref(props.draft || '')
const inputRef = ref(null)
// Esc 关闭面板后先不再弹出，等输入内容再次变化才恢复
const dismissed = ref(false)

watch(
  () => props.draft,
  (value) => {
    if (value !== text.value) text.value = value
  }
)

watch(text, (value) => {
  emit('update:draft', value)
  // 文本变了就解除 Esc 的"暂时关闭"，让用户继续输入时面板能重新出现
  dismissed.value = false
})

const overLimit = computed(() => text.value.length > MAX_LEN)
const nearLimit = computed(() => !overLimit.value && text.value.length >= SOFT_LEN)
const sendDisabled = computed(() => props.streaming || !text.value.trim() || overLimit.value)

// ---- `/` 命令面板 ----
// 只有当整段输入就是命令词（以 / 开头、还没输入空格/换行）时才弹出，
// 避免正常正文里的斜杠（如 "1/2"、"a/b"）误触发。
const commandKeyword = computed(() => {
  const v = text.value
  if (!v.startsWith('/')) return null
  if (/\s/.test(v)) return null
  return v.slice(1)
})
const activeIndex = ref(0)
const matches = computed(() => (commandKeyword.value === null ? [] : filterCommands(commandKeyword.value)))
const panelVisible = computed(
  () => commandKeyword.value !== null && !dismissed.value && matches.value.length > 0
)

watch(matches, () => {
  activeIndex.value = 0
})

function choose(index = activeIndex.value) {
  const item = matches.value[index]
  if (!item) return
  text.value = item.template
  dismissed.value = true
  activeIndex.value = 0
  nextTick(() => {
    const ta = inputRef.value?.textarea
    if (!ta) return
    ta.focus()
    // 光标停在第一个占位符内部，用户可以立刻开始替换
    const at = item.template.indexOf('{')
    const pos = at >= 0 ? at + 1 : item.template.length
    ta.setSelectionRange(pos, pos)
  })
}

/** 点底部「/」按钮：把光标拉进输入框并插入一个斜杠，从而唤起面板。 */
async function openCommands() {
  if (!text.value.startsWith('/')) text.value = '/'
  dismissed.value = false
  await nextTick()
  const ta = inputRef.value?.textarea
  ta?.focus()
  ta?.setSelectionRange(1, 1)
}

async function submit() {
  if (sendDisabled.value) return
  const query = text.value.trim()
  emit('send', query)
  text.value = ''
  emit('update:draft', '')
  dismissed.value = true
  await nextTick()
  inputRef.value?.focus?.()
}

function onKeydown(e) {
  // 面板可见时，方向键 / Enter / Tab / Esc 归面板使用，不触发发送
  if (panelVisible.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      activeIndex.value = (activeIndex.value + 1) % matches.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      activeIndex.value = (activeIndex.value - 1 + matches.value.length) % matches.value.length
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      choose()
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      dismissed.value = true
      return
    }
  }

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
    <div v-if="panelVisible" class="cmd-panel" role="listbox" aria-label="斜杠命令">
      <div class="cmd-head">命令 · ↑↓ 选择 · Enter 插入 · Esc 关闭</div>
      <button
        v-for="(c, i) in matches"
        :key="c.command"
        type="button"
        class="cmd-item"
        :class="{ active: i === activeIndex }"
        role="option"
        :aria-selected="i === activeIndex"
        @mousedown.prevent="choose(i)"
      >
        <span class="cmd-name">{{ c.command }}</span>
        <span class="cmd-title">{{ c.title }}</span>
        <span class="cmd-category">{{ c.category }}</span>
        <span class="cmd-desc">{{ c.desc }}</span>
      </button>
    </div>

    <el-input
      ref="inputRef"
      v-model="text"
      type="textarea"
      :autosize="{ minRows: 1, maxRows: MAX_ROWS }"
      resize="none"
      placeholder="输入问题，Enter 发送，Shift+Enter 换行；输入 / 唤起常用命令"
      :maxlength="MAX_LEN"
      aria-label="问题输入框"
      @keydown="onKeydown"
      @paste="onPaste"
    />
    <div class="input-footer">
      <div class="footer-left">
        <ModeSelector
          :model-value="props.mode"
          :disabled="props.streaming"
          @update:model-value="emit('update:mode', $event)"
        />
        <button type="button" class="cmd-trigger" title="插入斜杠命令" @click="openCommands">/ 命令</button>
        <span
          class="char-count"
          :class="{ over: overLimit, near: nearLimit }"
          :title="nearLimit ? `已接近 ${MAX_LEN} 字上限，请精简问题` : ''"
        >{{ text.length }} / {{ MAX_LEN }}</span>
      </div>
      <div class="actions">
        <el-button v-if="streaming" type="danger" aria-label="停止生成" @click="emit('stop')">停止生成</el-button>
        <el-button v-else type="primary" :disabled="sendDisabled" aria-label="发送问题" @click="submit">发送</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.input-bar {
  position: relative;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}

.cmd-panel {
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 100%;
  margin-bottom: 8px;
  max-height: 288px;
  overflow-y: auto;
  padding: 6px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 10px);
  box-shadow: var(--shadow-card, 0 8px 24px rgba(0, 0, 0, 0.12));
  z-index: 2048;
}

.cmd-head {
  padding: 4px 8px 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.cmd-item {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: var(--radius-sm, 8px);
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
  font: inherit;
}

.cmd-item.active,
.cmd-item:hover {
  background: var(--hover-bg, rgba(64, 128, 255, 0.1));
}

.cmd-name {
  font-weight: 600;
  font-size: 13px;
}

.cmd-title {
  font-size: 13px;
}

.cmd-category {
  font-size: 11px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-secondary);
}

.cmd-desc {
  flex: 1 1 100%;
  font-size: 12px;
  color: var(--text-secondary);
}

.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}

.footer-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.cmd-trigger {
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 8px);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.char-count {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
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
