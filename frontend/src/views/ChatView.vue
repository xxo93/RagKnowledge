<script setup>
// 对话页：左侧会话列表，右侧消息流、智能滚动与输入框。
import { ref, watch, nextTick, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useChat } from '../composables/useChat'
import SessionList from '../components/SessionList.vue'
import MessageBubble from '../components/MessageBubble.vue'
import ChatInput from '../components/ChatInput.vue'
import EmptyState from '../components/EmptyState.vue'

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
  setDraft,
  sendMessage,
  retryMessage,
  stop
} = useChat()

const listRef = ref(null)
const follow = ref(true)
const showBack = ref(false)
const sessionDrawerVisible = ref(false)

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

onMounted(async () => {
  loadSuggestions().catch(() => {})
  await loadSessions()
  if (state.sessions.length) {
    await selectSession(state.sessions[0].session_id)
  }
})
</script>

<template>
  <el-row :gutter="12" class="chat-layout">
    <el-col :xs="24" :sm="8" :md="6" class="desktop-sessions">
      <SessionList
        :sessions="state.sessions"
        :current-id="state.currentSessionId"
        :loading="state.sessionsLoading"
        :pending-title-id="state.pendingTitleSessionId"
        :total="state.sessionsTotal"
        :loading-more="state.sessionsLoadingMore"
        :keyword="state.sessionKeyword"
        @new="onNewSession"
        @select="onSelectSession"
        @rename="onRename"
        @remove="removeSession"
        @load-more="loadMoreSessions"
        @search="searchSessions"
      />
    </el-col>

    <el-drawer
      v-model="sessionDrawerVisible"
      title="会话列表"
      direction="ltr"
      size="280px"
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
        @new="onNewSession"
        @select="onSelectSession"
        @rename="onRename"
        @remove="removeSession"
        @load-more="loadMoreSessions"
        @search="searchSessions"
      />
    </el-drawer>

    <el-col :xs="24" :sm="16" :md="18">
      <div class="chat-panel">
        <button type="button" class="session-drawer-button" aria-label="打开会话列表" @click="sessionDrawerVisible = true">
          会话列表
        </button>
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
          @send="onSend"
          @stop="stop"
          @update:draft="setDraft(state.currentSessionId, $event)"
        />
      </div>
    </el-col>
  </el-row>
</template>

<style scoped>
/* 高度完全交给外层（.app-main.chat-mode 已锁死剩余高度）：
   对话区内部滚动，输入框固定贴在底部，不再随消息变长把输入框推出首屏。 */
.chat-layout {
  height: 100%;
  margin: 0;
}

/* el-col 是子组件，scoped 需要 :deep 才能命中 */
.chat-layout :deep(.el-col) {
  height: 100%;
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

.session-drawer-button {
  display: none;
  align-self: flex-start;
  margin: 12px 12px 0;
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
