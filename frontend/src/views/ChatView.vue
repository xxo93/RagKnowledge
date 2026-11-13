<script setup>
// 对话页：左侧会话列表，右侧消息流、智能滚动与输入框。
import { ref, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useChat } from '../composables/useChat'
import SessionList from '../components/SessionList.vue'
import SessionMemoryPanel from '../components/SessionMemoryPanel.vue'
import MessageBubble from '../components/MessageBubble.vue'
import ChatInput from '../components/ChatInput.vue'
import EmptyState from '../components/EmptyState.vue'
import { exportSession } from '../api/chat'

const props = defineProps({
  currentUser: { type: Object, default: null }
})

const {
  state,
  loadSessions,
  loadMoreSessions,
  searchSessions,
  loadSuggestions,
  createNewSession,
  selectSession,
  loadEarlierRecords,
  removeSession,
  renameSession,
  toggleArchived,
  togglePin,
  archiveSession,
  restoreSession,
  batchSessionAction,
  setDraft,
  setMode,
  sendMessage,
  retryMessage,
  stop
} = useChat()

const listRef = ref(null)
const follow = ref(true)
const showBack = ref(false)
const sessionDrawerVisible = ref(false)
const memoryVisible = ref(false)
const memoryPanelRef = ref(null)
const exporting = ref(false)
// 窄屏抽屉占满宽度，避免 400px 固定宽度在小屏横向裁切
const memoryDrawerSize = ref(window.innerWidth < 480 ? '100%' : '400px')
function syncMemoryDrawerSize() {
  memoryDrawerSize.value = window.innerWidth < 480 ? '100%' : '400px'
}

// N11：桌面端会话栏宽度可拖拽，宽度持久化；窄屏继续用抽屉
const SESSION_WIDTH_KEY = 'ragknowledge:session-width'
const SESSION_WIDTH_DEFAULT = 300
const SESSION_WIDTH_MIN = 220
const SESSION_WIDTH_MAX = 520
const SESSION_WIDTH_GAP = 360
const layoutRef = ref(null)
const sessionWidth = ref(SESSION_WIDTH_DEFAULT)
const resizing = ref(false)
let resizeStartX = 0
let resizeStartWidth = 0

function readStoredWidth() {
  try {
    const raw = Number(window.localStorage.getItem(SESSION_WIDTH_KEY))
    return Number.isFinite(raw) && raw > 0 ? raw : SESSION_WIDTH_DEFAULT
  } catch {
    return SESSION_WIDTH_DEFAULT
  }
}

function widthUpperBound() {
  const total = layoutRef.value?.clientWidth || window.innerWidth
  return Math.max(
    SESSION_WIDTH_MIN,
    Math.min(SESSION_WIDTH_MAX, Math.floor(total * 0.5), total - SESSION_WIDTH_GAP)
  )
}

function clampWidth(value) {
  return Math.min(widthUpperBound(), Math.max(SESSION_WIDTH_MIN, Math.round(value)))
}

function persistWidth() {
  try {
    window.localStorage.setItem(SESSION_WIDTH_KEY, String(sessionWidth.value))
  } catch {
    // 隐私模式或存储禁用时忽略，不影响本次使用
  }
}

function syncLayoutWidth() {
  sessionWidth.value = clampWidth(sessionWidth.value)
}

function onResizeMove(event) {
  if (!resizing.value) return
  sessionWidth.value = clampWidth(resizeStartWidth + event.clientX - resizeStartX)
}

function stopResize() {
  if (!resizing.value) return
  resizing.value = false
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
  window.removeEventListener('mousemove', onResizeMove)
  window.removeEventListener('mouseup', stopResize)
  persistWidth()
}

function startResize(event) {
  if (window.innerWidth <= 992) return
  resizing.value = true
  resizeStartX = event.clientX
  resizeStartWidth = sessionWidth.value
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'col-resize'
  window.addEventListener('mousemove', onResizeMove)
  window.addEventListener('mouseup', stopResize)
  event.preventDefault()
}

function resetSessionWidth() {
  sessionWidth.value = clampWidth(SESSION_WIDTH_DEFAULT)
  persistWidth()
}

function onResizerKeydown(event) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  event.preventDefault()
  const delta = event.key === 'ArrowLeft' ? -16 : 16
  sessionWidth.value = clampWidth(sessionWidth.value + delta)
  persistWidth()
}

async function scrollToBottom(smooth = false) {
  await nextTick()
  const el = listRef.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
}

function onScroll() {
  const el = listRef.value
  if (!el) return
  const distance = el.scrollHeight - el.scrollTop - el.clientHeight
  const atBottom = distance < 80
  follow.value = atBottom
  showBack.value = !atBottom
}

function backToBottom() {
  follow.value = true
  showBack.value = false
  scrollToBottom(true)
}

function resetFollow() {
  follow.value = true
  showBack.value = false
}

function onSend(query) {
  resetFollow()
  sendMessage(query)
}

function onRetry(message) {
  resetFollow()
  retryMessage(message)
}

function onSelectSession(id) {
  sessionDrawerVisible.value = false
  selectSession(id)
}

async function onNewSession() {
  // 新建会切换 currentSessionId；流式进行中先中断，避免回答继续写入已切走的会话缓冲区
  if (state.streaming) stop()
  sessionDrawerVisible.value = false
  await createNewSession()
}

async function onLoadEarlier() {
  const el = listRef.value
  const prevHeight = el?.scrollHeight || 0
  const loaded = await loadEarlierRecords()
  if (loaded > 0) {
    await nextTick()
    if (el) el.scrollTop = Math.max(0, el.scrollHeight - prevHeight)
  }
}

watch(
  () => state.messages,
  async () => {
    if (follow.value) await scrollToBottom()
  },
  { deep: true }
)

watch(
  () => state.currentSessionId,
  () => {
    follow.value = true
    showBack.value = false
    memoryVisible.value = false
  }
)

// 每轮回答结束后若面板开着，刷新摘要/事实/用量（记忆维护在 done 之后异步完成，稍等一拍）
watch(
  () => [state.streaming, state.messages.length],
  ([streaming]) => {
    if (!streaming && memoryVisible.value) {
      setTimeout(() => memoryPanelRef.value?.refresh?.(), 600)
    }
  }
)

async function onRename(session) {
  let name
  try {
    const result = await ElMessageBox.prompt('请输入新的会话名称', '重命名会话', {
      inputValue: session.session_name,
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
      confirmButtonText: '保存',
      cancelButtonText: '取消'
    })
    name = result.value
  } catch (_) {
    return
  }
  const ok = await renameSession(session.session_id, name)
  if (ok) ElMessage.success('会话已重命名')
}

async function onPin(session, pinned) {
  const done = await togglePin(session.session_id, pinned)
  if (done) ElMessage.success(pinned ? '会话已置顶' : '已取消置顶')
}

async function onArchive(session) {
  try {
    await ElMessageBox.confirm(
      `归档会话“${session.session_name}”？归档后从主列表移入「已归档」，可随时恢复。`,
      '归档会话',
      { type: 'info', confirmButtonText: '归档', cancelButtonText: '取消' }
    )
  } catch (_) {
    return
  }
  if (await archiveSession(session.session_id)) ElMessage.success('会话已归档')
}

async function onRestore(session) {
  if (await restoreSession(session.session_id, true)) ElMessage.success('会话已恢复')
}

async function onToggleArchived(visible) {
  await toggleArchived(visible)
}

async function onBatch({ action, ids }) {
  const data = await batchSessionAction(action, ids)
  const succeeded = data?.succeeded?.length || 0
  const failed = data?.failed?.length || 0
  if (succeeded) ElMessage.success(`批量${BATCH_LABELS[action]}成功 ${succeeded} 个`)
  if (failed) ElMessage.warning(`批量${BATCH_LABELS[action]}失败 ${failed} 个（不存在或无权限）`)
}

const BATCH_LABELS = { pin: '置顶', unpin: '取消置顶', archive: '归档', restore: '恢复' }

async function onExport(format) {
  if (!state.currentSessionId || exporting.value) return
  exporting.value = true
  try {
    await exportSession(state.currentSessionId, format)
    ElMessage.success(format === 'json' ? 'JSON 会话已开始下载' : 'Markdown 会话已开始下载')
  } catch {
    // 统一错误提示已由 http 拦截器弹出
  } finally {
    exporting.value = false
  }
}

onMounted(async () => {
  sessionWidth.value = clampWidth(readStoredWidth())
  syncMemoryDrawerSize()
  window.addEventListener('resize', syncMemoryDrawerSize)
  window.addEventListener('resize', syncLayoutWidth)
  loadSuggestions().catch(() => {})
  await loadSessions()
  if (state.sessions.length) {
    await selectSession(state.sessions[0].session_id)
  }
})

onUnmounted(() => {
  window.removeEventListener('resize', syncMemoryDrawerSize)
  window.removeEventListener('resize', syncLayoutWidth)
  stopResize()
})
</script>

<template>
  <div
    ref="layoutRef"
    class="chat-layout"
    :class="{ 'is-resizing': resizing }"
  >
    <aside class="desktop-sessions" :style="{ width: `${sessionWidth}px` }">
      <SessionList
        :sessions="state.sessions"
        :current-id="state.currentSessionId"
        :loading="state.sessionsLoading"
        :pending-title-id="state.pendingTitleSessionId"
        :total="state.sessionsTotal"
        :loading-more="state.sessionsLoadingMore"
        :keyword="state.sessionKeyword"
        :archived-sessions="state.archivedSessions"
        :archived-total="state.archivedTotal"
        :archived-loading="state.archivedLoading"
        :archived-visible="state.archivedVisible"
        @new="onNewSession"
        @select="onSelectSession"
        @rename="onRename"
        @remove="removeSession"
        @load-more="loadMoreSessions"
        @search="searchSessions"
        @pin="onPin"
        @archive="onArchive"
        @restore="onRestore"
        @toggle-archived="onToggleArchived"
        @batch="onBatch"
      />
    </aside>

    <div
      class="layout-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整会话栏宽度，双击恢复默认"
      tabindex="0"
      title="拖动调整会话栏宽度，双击恢复默认；也可用左右方向键微调"
      @mousedown.prevent="startResize"
      @dblclick="resetSessionWidth"
      @keydown="onResizerKeydown"
    ></div>

    <el-drawer
      v-model="sessionDrawerVisible"
      title="会话列表"
      direction="ltr"
      size="280px"
      append-to-body
      class="session-drawer"
    >
      <SessionList
        :sessions="state.sessions"
        :current-id="state.currentSessionId"
        :loading="state.sessionsLoading"
        :pending-title-id="state.pendingTitleSessionId"
        :total="state.sessionsTotal"
        :loading-more="state.sessionsLoadingMore"
        :keyword="state.sessionKeyword"
        :archived-sessions="state.archivedSessions"
        :archived-total="state.archivedTotal"
        :archived-loading="state.archivedLoading"
        :archived-visible="state.archivedVisible"
        @new="onNewSession"
        @select="onSelectSession"
        @rename="onRename"
        @remove="removeSession"
        @load-more="loadMoreSessions"
        @search="searchSessions"
        @pin="onPin"
        @archive="onArchive"
        @restore="onRestore"
        @toggle-archived="onToggleArchived"
        @batch="onBatch"
      />
    </el-drawer>

    <el-drawer
      v-model="memoryVisible"
      title="会话记忆"
      direction="rtl"
      :size="memoryDrawerSize"
      append-to-body
      class="memory-drawer"
    >
      <SessionMemoryPanel ref="memoryPanelRef" :session-id="state.currentSessionId" />
    </el-drawer>

    <div class="chat-main">
      <div class="chat-panel">
        <div class="chat-toolbar">
          <button
            type="button"
            class="session-drawer-button"
            aria-label="打开会话列表"
            @click="sessionDrawerVisible = true"
          >会话列表</button>
          <span class="toolbar-spacer"></span>
          <el-dropdown trigger="click" :disabled="!state.currentSessionId || exporting" @command="onExport">
            <button
              type="button"
              class="memory-button export-button"
              :disabled="!state.currentSessionId || exporting"
              :aria-label="'导出当前会话'"
            >{{ exporting ? '导出中…' : '导出' }}</button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="md">Markdown（含来源与工具记录）</el-dropdown-item>
                <el-dropdown-item command="json">JSON（便于回放与二次处理）</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <button
            type="button"
            class="memory-button"
            :disabled="!state.currentSessionId"
            :aria-label="state.memoryEnabled === false ? '当前会话已关闭长期记忆' : '查看本会话记忆'"
            :class="{ off: state.memoryEnabled === false }"
            @click="memoryVisible = true"
          >
            <span class="memory-dot"></span>
            记忆
          </button>
        </div>
        <div
          ref="listRef"
          class="msg-list"
          role="log"
          aria-live="polite"
          aria-label="对话记录"
          @scroll="onScroll"
        >
          <div v-if="state.canLoadEarlier" class="earlier-wrap">
            <el-button
              link
              :loading="state.recordsLoadingEarlier"
              :disabled="state.recordsLoadingEarlier"
              @click="onLoadEarlier"
            >
              加载更早消息
            </el-button>
          </div>
          <EmptyState v-if="!state.messages.length" :questions="state.suggestions" @send="onSend" />
          <MessageBubble
            v-for="(m, i) in state.messages"
            :key="i"
            :message="m"
            :current-user="props.currentUser"
            :on-retry="onRetry"
          />
        </div>

        <transition name="back-fade">
          <button v-if="showBack" type="button" class="back-bottom" @click="backToBottom">
            ↓ {{ state.streaming ? '正在生成…' : '新消息' }}
          </button>
        </transition>

        <div class="sr-only" role="status" aria-live="polite">
          {{ state.streaming ? '正在生成回答' : '回答已完成' }}
        </div>

        <ChatInput
          :streaming="state.streaming"
          :session-id="state.currentSessionId"
          :draft="state.drafts[state.currentSessionId] || ''"
          :mode="state.mode"
          @send="onSend"
          @stop="stop"
          @update:mode="setMode"
          @update:draft="setDraft(state.currentSessionId, $event)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 高度完全交给外层（.app-main.chat-mode 已锁死剩余高度）：
   对话区内部滚动，输入框固定贴在底部，不再随消息变长把输入框推出首屏。 */
.chat-layout {
  display: flex;
  align-items: stretch;
  height: 100%;
  min-height: 0;
  margin: 0;
  position: relative;
}

.desktop-sessions {
  flex: 0 0 auto;
  min-width: 220px;
  max-width: 520px;
  height: 100%;
  min-height: 0;
}

.chat-main {
  flex: 1 1 0;
  min-width: 0;
  height: 100%;
  min-height: 0;
}

.layout-resizer {
  flex: 0 0 8px;
  align-self: stretch;
  position: relative;
  cursor: col-resize;
  outline: none;
  touch-action: none;
}

.layout-resizer::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 2px;
  border-radius: 2px;
  background: transparent;
  transition: background-color 0.15s ease;
}

.layout-resizer:hover::before,
.layout-resizer:focus-visible::before,
.chat-layout.is-resizing .layout-resizer::before {
  background: var(--primary);
}

.layout-resizer:focus-visible {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 35%, transparent);
}

.chat-panel {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--panel);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.msg-list {
  flex: 1 1 auto;
  min-height: 0; /* flex 子项必须允许收缩，否则消息多了会把输入框顶出去 */
  overflow-y: auto;
  padding: 16px;
}

.earlier-wrap {
  display: flex;
  justify-content: center;
  margin-bottom: 12px;
}

.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px 0;
  min-height: 34px;
}

.toolbar-spacer {
  flex: 1;
}

.memory-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text-secondary);
  border-radius: 16px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease, background-color 0.15s ease;
}

.memory-button:hover:not(:disabled) {
  color: var(--primary);
  border-color: var(--primary);
  background: color-mix(in srgb, var(--primary) 6%, var(--panel));
}

.memory-button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.memory-button .memory-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--primary);
}

.memory-button.off {
  color: var(--text-secondary);
  border-style: dashed;
}

.memory-button.off .memory-dot {
  background: var(--el-color-warning);
}

.session-drawer-button {
  display: none;
  align-self: flex-start;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  border-radius: var(--radius-sm);
  padding: 6px 12px;
  cursor: pointer;
}

.back-bottom {
  position: absolute;
  right: 24px;
  bottom: 88px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text-muted);
  border-radius: 18px;
  padding: 6px 12px;
  box-shadow: var(--shadow-card);
  cursor: pointer;
}

.back-fade-enter-active,
.back-fade-leave-active {
  transition: opacity 0.15s ease;
}

.back-fade-enter-from,
.back-fade-leave-to {
  opacity: 0;
}

@media (max-width: 992px) {
  /* 窄屏同样保持固定布局：不再改成 auto，否则输入框会被消息流推出首屏 */
  .chat-layout {
    height: 100%;
  }

  .desktop-sessions {
    display: none;
  }

  .layout-resizer {
    display: none;
  }

  .chat-main {
    flex: 1 1 100%;
    width: 100%;
  }

  .session-drawer-button {
    display: inline-flex;
  }
}

@media (max-width: 768px) {
  .msg-list {
    padding: 10px;
  }
}
</style>
