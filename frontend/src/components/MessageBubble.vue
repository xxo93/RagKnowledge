<script setup>
// 消息气泡：主客头像、工具状态、Markdown 正文、引用来源、错误卡片与消息级操作。
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ToolChip from './ToolChip.vue'
import ErrorState from './ErrorState.vue'
import SourceList from './SourceList.vue'
import DataTableCard from './DataTableCard.vue'
import MarkdownRenderer from './MarkdownRenderer.vue'
import MessageAvatar from './MessageAvatar.vue'
import { AGENT_NAME } from '../config/brand.js'
import { useTypewriter } from '../composables/useTypewriter'

const props = defineProps({
  message: { type: Object, required: true },
  currentUser: { type: Object, default: null },
  onRetry: { type: Function, default: null }
})

const role = computed(() => props.message.role || 'assistant')
const userName = computed(() => props.currentUser?.display_name || props.currentUser?.username || '')

// 流式正文按打字机节奏播放；历史消息与已完成的回答直接整段显示
const typedContent = useTypewriter(
  () => (role.value === 'assistant' ? props.message.content : ''),
  () => Boolean(props.message.streaming)
)
const showStageHint = computed(() => role.value === 'assistant' && props.message.streaming && !props.message.toolCalls?.length)

const copied = ref(false)
const sourceListRef = ref(null)
const sourceCount = computed(() => props.message.source_count ?? props.message.sources?.length ?? 0)
const visibleTools = computed(() => (props.message.toolCalls || []).slice(0, 3))
const extraToolCount = computed(() => Math.max(0, (props.message.toolCalls || []).length - 3))

// 能力链路标签：让用户一眼看出这条回答是不是内部知识库给的
const modeInfo = computed(() => {
  const mode = props.message.answerMode
  if (mode === 'web') return { label: '联网检索 · 非内部知识', type: 'warning' }
  if (mode === 'fallback_contact' || mode === 'fallback_guide') return { label: '未命中内部知识库', type: 'info' }
  if (mode === 'tool') return { label: '工具计算', type: 'success' }
  if (mode === 'biz_query') return { label: '业务库查询 · 已脱敏', type: 'success' }
  if (mode === 'chat') return { label: '通用对话', type: 'info' }
  return null
})

const noticeText = computed(() => {
  if (props.message.notice?.msg) return props.message.notice.msg
  // 历史回放没有 notice 帧，按 answer_mode 还原同一句提示
  if (props.message.answerMode === 'web') {
    return '本条回答来自联网检索，不属于企业内部知识库能力，仅供参考。'
  }
  return ''
})
const noticeLevel = computed(() => props.message.notice?.level || 'info')

// 早期业务库回答没有落库 table_payload，回放时表格与下载链接无法还原：给出可操作的补救提示
const legacyBizHint = computed(
  () => props.message.answerMode === 'biz_query' && !props.message.table && !props.message.streaming
)

function formatElapsed(ms) {
  if (ms === null || ms === undefined) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

async function copyContent(text) {
  const value = text || ''
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    copied.value = true
    ElMessage.success('已复制 Markdown 原文')
    setTimeout(() => { copied.value = false }, 1500)
  } catch {
    ElMessage.warning('当前浏览器不支持自动复制')
  }
}

function retry() {
  if (props.message.streaming) return
  props.onRetry?.(props.message)
}

function onCite(index) {
  sourceListRef.value?.focusSource(index)
}
</script>

<template>
  <div class="bubble-row" :class="[role, { error: message.error }]">
    <MessageAvatar v-if="role === 'assistant'" role="assistant" :size="36" />

    <div class="bubble-col">
      <div v-if="role === 'assistant'" class="name-row">{{ AGENT_NAME }}</div>
      <div class="bubble">
        <div v-if="message.toolCalls?.length" class="tools" aria-live="polite">
          <ToolChip v-for="(t, i) in visibleTools" :key="`${t.name}-${t.round}-${i}`" :tool="t" />
          <el-tag v-if="extraToolCount" size="small" type="info" effect="plain">
            另有 {{ extraToolCount }} 步
          </el-tag>
        </div>

        <div v-if="showStageHint" class="stage-hint">正在理解问题…</div>

        <div v-if="modeInfo || noticeText" class="mode-row">
          <el-tag v-if="modeInfo" size="small" :type="modeInfo.type" effect="plain">{{ modeInfo.label }}</el-tag>
        </div>
        <div v-if="noticeText" class="notice mode" :class="noticeLevel">{{ noticeText }}</div>

        <MarkdownRenderer
          v-if="role === 'assistant'"
          :content="typedContent"
          :streaming="message.streaming"
          :sources="message.sources"
          @cite="onCite"
        />
        <div v-else class="plain-content">{{ message.content }}</div>

        <DataTableCard v-if="message.table" :table="message.table" />
        <div v-else-if="legacyBizHint" class="notice mode info">
          这是早期业务库回答，当时未保存表格明细；可点下方「重试」重新查询获取表格与 CSV 下载链接。
        </div>

        <div v-if="message.aborted" class="notice aborted">已中断回答，以下内容未完成，可重试或继续提问。</div>
        <div v-if="message.saved === false" class="notice warn">本次回答未保存到历史记录。</div>

        <ErrorState v-if="message.error" :message="message" :on-retry="props.onRetry" />

        <SourceList
          v-if="message.sources?.length"
          ref="sourceListRef"
          :sources="message.sources"
          :query="message.query"
        />

        <div v-if="role === 'assistant'" class="message-footer">
          <div class="meta">
            <span v-if="formatElapsed(message.elapsed_ms)">耗时 {{ formatElapsed(message.elapsed_ms) }}</span>
            <span v-if="sourceCount > 0">引用 {{ sourceCount }} 条</span>
          </div>
          <div class="actions">
            <el-button link size="small" @click="copyContent(message.content)">
              {{ copied ? '已复制 ✓' : '复制' }}
            </el-button>
            <el-button
              v-if="message.query"
              link
              size="small"
              :disabled="message.streaming"
              title="将以原问题在当前会话追加一轮对话"
              @click="retry"
            >重试</el-button>
          </div>
        </div>

        <div v-if="role === 'user'" class="message-footer user-footer">
          <el-button link size="small" @click="copyContent(message.content)">复制</el-button>
        </div>
      </div>
    </div>

    <MessageAvatar
      v-if="role === 'user'"
      role="user"
      :name="userName"
      :user-id="currentUser?.user_id"
      :size="36"
    />
  </div>
</template>

<style scoped>
.bubble-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 20px;
}

.bubble-row.user {
  justify-content: flex-end;
}

.bubble-col {
  display: flex;
  flex-direction: column;
  max-width: 76%;
  min-width: 0;
}

.bubble-row.assistant .bubble-col {
  max-width: 78%;
}

.name-row {
  font-size: 12px;
  color: var(--text-secondary);
  margin: 0 0 4px 2px;
}

.bubble {
  padding: 10px 14px;
  border-radius: var(--radius-lg);
  background: var(--bubble-assistant);
  min-width: 0;
}

.user .bubble {
  background: var(--bubble-user);
}

.bubble-row.error .bubble {
  background: var(--danger-bg);
  border: 1px solid var(--danger-light);
}

.tools {
  margin-bottom: 6px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

/* 窄屏时工具标签（如「已查询业务库 · 近 N 天订单列表 100 行（共 300 行，已截断）」）允许换行，
   避免 el-tag 默认 nowrap 把气泡撑出视口被裁切 */
.tools :deep(.el-tag) {
  max-width: 100%;
  height: auto;
  white-space: normal;
  word-break: break-word;
  line-height: 1.6;
}

.mode-row :deep(.el-tag) {
  max-width: 100%;
  height: auto;
  white-space: normal;
  word-break: break-word;
}

.stage-hint {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 6px;
}

.plain-content {
  white-space: pre-wrap;
  word-break: break-word;
}

.notice {
  margin-top: 8px;
  font-size: 12px;
  border-radius: var(--radius-sm);
  padding: 6px 8px;
}

.notice.aborted {
  color: var(--warning);
  background: var(--warning-light);
}

.notice.warn {
  color: var(--text-muted);
  background: var(--primary-light);
}

/* 能力边界提示条：联网/兜底时展示在正文上方，与"已中断/未保存"提示区分色板 */
.mode-row {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}

.notice.mode {
  margin-top: 0;
  margin-bottom: 8px;
  color: var(--text-secondary);
  background: var(--surface-muted);
  border-left: 3px solid var(--primary);
}

.notice.mode.warning {
  color: var(--warning);
  background: var(--warning-light);
  border-left-color: var(--warning);
}

.message-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.user-footer {
  justify-content: flex-end;
}

.meta {
  display: flex;
  gap: 10px;
  font-size: 11px;
  color: var(--text-secondary);
}

.actions {
  display: flex;
  gap: 4px;
}

@media (max-width: 768px) {
  .bubble-col,
  .bubble-row.assistant .bubble-col {
    max-width: 92%;
  }

  .message-avatar {
    --avatar-size: 28px;
  }

  .bubble-row {
    gap: 8px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .bubble-row,
  .message-footer {
    transition: none;
  }
}
</style>
