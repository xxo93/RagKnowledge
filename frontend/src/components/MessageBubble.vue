<script setup>
// 消息气泡：主客头像、工具状态、Markdown 正文、引用来源、错误卡片与消息级操作。
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import ToolChip from './ToolChip.vue'
import ErrorState from './ErrorState.vue'
import SourceList from './SourceList.vue'
import DataTableCard from './DataTableCard.vue'
import PlanCard from './PlanCard.vue'
import MarkdownRenderer from './MarkdownRenderer.vue'
import MessageAvatar from './MessageAvatar.vue'
import ModeBadge from './ModeBadge.vue'
import EvidenceBadge from './EvidenceBadge.vue'
import TraceDrawer from './TraceDrawer.vue'
import { submitFeedback } from '../api/chat'
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
const traceCopied = ref(false)
const detailVisible = ref(false)
// v3 / WP5-3：回答反馈（赞/踩 + 原因），提交后进入评测集
const feedbackRating = ref(props.message.feedbackRating ?? null)
const feedbackVisible = ref(false)
const feedbackReason = ref('inaccurate')
const feedbackText = ref('')
const feedbackSaving = ref(false)
const FEEDBACK_REASONS = [
  { code: 'inaccurate', label: '内容不准确' },
  { code: 'insufficient', label: '资料不足' },
  { code: 'irrelevant', label: '答非所问' },
  { code: 'unclear', label: '表述不清' },
  { code: 'other', label: '其他原因' }
]
const sourceListRef = ref(null)
const sourceCount = computed(() => props.message.source_count ?? props.message.sources?.length ?? 0)
const visibleTools = computed(() => (props.message.toolCalls || []).slice(0, 3))
const extraToolCount = computed(() => Math.max(0, (props.message.toolCalls || []).length - 3))

// 能力链路徽标：颜色 + hover 说明，一眼看出这条回答是不是内部知识库给的
const showBadge = computed(() => Boolean(props.message.answerMode || props.message.intent))
// v3 / M8 WP5-10：依据强度与能力链路并列展示，先回答「该不该信」
const showEvidence = computed(
  () => role.value === 'assistant' && !props.message.streaming && Boolean(props.message.evidence?.level)
)

// 通用回答的顶部淡提示：可关闭，关闭状态存 localStorage（不进会话记忆）
const HINT_KEY = 'rk.generalHintDismissed'
const hintDismissed = ref(false)
try {
  hintDismissed.value = localStorage.getItem(HINT_KEY) === '1'
} catch { /* 隐私模式不可用 */ }
const showGeneralHint = computed(
  () => role.value === 'assistant' && props.message.answerMode === 'general' && !hintDismissed.value
)
function dismissGeneralHint() {
  hintDismissed.value = true
  try {
    localStorage.setItem(HINT_KEY, '1')
  } catch { /* 忽略存储失败 */ }
}

const noticeText = computed(() => {
  // 通用回答的"未使用内部资料"由可关闭的顶部提示承载，避免与 notice 重复
  if (props.message.notice?.code === 'GENERAL_NO_INTERNAL') return ''
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

watch(
  () => props.message.feedbackRating,
  (v) => { feedbackRating.value = v ?? null }
)

async function sendFeedback(rating, reasonCode = null) {
  if (feedbackSaving.value || !props.message.recordId) return
  if (rating === feedbackRating.value) return
  feedbackSaving.value = true
  try {
    await submitFeedback({
      record_id: props.message.recordId,
      rating,
      reason_code: reasonCode,
      reason_text: (feedbackText.value || '').trim() || null
    })
    feedbackRating.value = rating
    feedbackVisible.value = false
    feedbackText.value = ''
    ElMessage.success(rating > 0 ? '已记录「有帮助」' : '已记录「需改进」，感谢反馈')
  } catch {
    // 统一错误提示已由 http 拦截器弹出
  } finally {
    feedbackSaving.value = false
  }
}

function openNegative() {
  feedbackReason.value = 'inaccurate'
  feedbackText.value = ''
  feedbackVisible.value = true
}

function onCite(index) {
  sourceListRef.value?.focusSource(index)
}

// v3 / WP3 §5.6：消息详情（链路 / 耗时 / 引用数 / trace_id）与排障信息复制
const detailRows = computed(() => {
  const m = props.message
  const rows = [
    { label: '意图', value: m.intent || '—' },
    { label: '回答链路', value: m.answerMode || '—' },
    { label: '依据强度', value: m.evidence ? `${m.evidence.label}（${m.evidence.reason || '—'}）` : '—' },
    { label: '耗时', value: formatElapsed(m.elapsed_ms) || '—' },
    { label: '首字延迟', value: formatElapsed(m.first_token_ms) || '—' },
    { label: '引用数', value: sourceCount.value ? `${sourceCount.value} 条` : '0 条' },
    { label: '回答字数', value: m.content ? `${m.content.length} 字` : '—' }
  ]
  if (m.toolCalls?.length) rows.push({ label: '工具步骤', value: `${m.toolCalls.length} 步` })
  return rows
})

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN', { hour12: false })
}

async function copyDiagnostics() {
  const m = props.message
  const user = props.currentUser
  const text = [
    `trace_id: ${m.traceId || '—'}`,
    `时间: ${fmtTime(m.createdAt)}`,
    `用户: ${user?.display_name || user?.username || '—'}（${user?.user_id ?? '—'}）`,
    `意图/链路: ${m.intent || '—'} / ${m.answerMode || '—'}`,
    `耗时/首字: ${formatElapsed(m.elapsed_ms) || '—'} / ${formatElapsed(m.first_token_ms) || '—'}`,
    `问题: ${m.query || '—'}`
  ].join('\n')
  try {
    await navigator.clipboard.writeText(text)
    traceCopied.value = true
    ElMessage.success('排障信息已复制')
    setTimeout(() => { traceCopied.value = false }, 1500)
  } catch {
    ElMessage.warning('当前浏览器不支持自动复制')
  }
}

async function copyTraceOnly() {
  try {
    await navigator.clipboard.writeText(props.message.traceId || '')
    ElMessage.success('trace_id 已复制')
  } catch {
    ElMessage.warning('当前浏览器不支持自动复制')
  }
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

        <div v-if="showEvidence || showBadge || noticeText" class="mode-row">
          <EvidenceBadge v-if="showEvidence" :evidence="message.evidence" />
          <ModeBadge v-if="showBadge" :mode="message.answerMode || ''" :intent="message.intent || ''" />
          <span v-if="message.degradedReason" class="mode-debug" :title="`通用意图已降级为知识库：${message.degradedReason}`">
            已按内部资料优先处理
          </span>
        </div>
        <div v-if="showGeneralHint" class="notice mode general-hint">
          <span class="hint-text">本条回答未使用企业内部资料 · 涉及内部制度、金额或人名请切换知识库模式</span>
          <button type="button" class="hint-close" aria-label="不再提示" @click="dismissGeneralHint">×</button>
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
        <PlanCard v-if="message.workflowTask" :task="message.workflowTask" />
        <div v-else-if="legacyBizHint" class="notice mode info">
          这是早期业务库回答，当时未保存表格明细；可点下方「重试」重新查询获取表格与 CSV 下载链接。
        </div>

        <div v-if="message.aborted" class="notice aborted">已中断回答，以下内容未完成，可重试或继续提问。</div>
        <div v-if="message.saved === false" class="notice warn">本次回答未保存到历史记录。</div>

        <ErrorState v-if="message.error" :message="message" :on-retry="props.onRetry" :current-user="currentUser" />

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
            <template v-if="message.recordId && !message.streaming">
              <el-button
                link
                size="small"
                class="feedback-btn"
                :class="{ active: feedbackRating === 1 }"
                :loading="feedbackSaving && feedbackRating === 1"
                title="这条回答有帮助"
                @click="sendFeedback(1, 'helpful')"
              >{{ feedbackRating === 1 ? '已赞' : '有帮助' }}</el-button>
              <el-button
                link
                size="small"
                class="feedback-btn negative"
                :class="{ active: feedbackRating === -1 }"
                title="这条回答需要改进"
                @click="openNegative"
              >{{ feedbackRating === -1 ? '已反馈' : '需改进' }}</el-button>
            </template>
            <el-button link size="small" @click="copyContent(message.content)">
              {{ copied ? '已复制 ✓' : '复制' }}
            </el-button>
            <el-button
              v-if="message.traceId || message.answerMode"
              link
              size="small"
              class="trace-trigger"
              @click="detailVisible = true"
            >
              链路
            </el-button>
            <TraceDrawer
              v-model="detailVisible"
              :trace-id="message.traceId || ''"
              :fallback="message"
            />
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

        <el-dialog
          v-model="feedbackVisible"
          title="这条回答哪里需要改进？"
          width="min(420px, 92vw)"
          append-to-body
          class="feedback-dialog"
        >
          <div class="feedback-reasons">
            <button
              v-for="r in FEEDBACK_REASONS"
              :key="r.code"
              type="button"
              class="feedback-reason"
              :class="{ active: feedbackReason === r.code }"
              @click="feedbackReason = r.code"
            >{{ r.label }}</button>
          </div>
          <el-input
            v-model="feedbackText"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="可以补充具体问题，例如引用不准、结论与制度不符（选填）"
          />
          <template #footer>
            <el-button @click="feedbackVisible = false">取消</el-button>
            <el-button type="primary" :loading="feedbackSaving" @click="sendFeedback(-1, feedbackReason)">
              提交反馈
            </el-button>
          </template>
        </el-dialog>
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

/* 通用回答的可关闭淡提示：低对比度、不抢正文 */
.general-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
  background: var(--surface-muted);
  border-left-color: var(--border-strong, #c8ced8);
}

.hint-text {
  flex: 1 1 auto;
  line-height: 1.6;
}

.hint-close {
  flex: 0 0 auto;
  border: none;
  background: transparent;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
  cursor: pointer;
}

.hint-close:hover {
  color: var(--text);
}

.mode-debug {
  font-size: 11px;
  color: var(--text-muted);
  cursor: help;
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

.feedback-btn.active {
  color: var(--primary);
  font-weight: 600;
}

.feedback-btn.negative.active {
  color: var(--warning);
}

.feedback-reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.feedback-reason {
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 5px 12px;
  font-size: 12px;
  cursor: pointer;
}

.feedback-reason.active {
  border-color: var(--primary);
  background: var(--primary-light);
  color: var(--primary);
}

/* ---- 消息详情浮层（v3 / WP3）---- */
.detail-pop {
  font-size: 12px;
}

.detail-pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

.detail-pop-title {
  font-weight: 600;
  font-size: 13px;
}

.detail-pop-trace {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  color: var(--text-secondary);
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--surface-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 160px;
  white-space: nowrap;
}

.detail-pop-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 0;
  color: var(--text-secondary);
}

.detail-pop-row b {
  font-weight: 500;
  color: var(--text);
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
}

.detail-pop-actions {
  display: flex;
  gap: 8px;
  margin-top: 4px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
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
