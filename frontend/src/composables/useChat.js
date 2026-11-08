// 对话状态编排（架构 §7.2）：会话列表、当前会话、消息流、流式状态与中断控制器。
// MVP 不引入 Pinia；本 composable 在 ChatView 内使用，切换会话时重置消息缓冲（§7.4）。
import { reactive, shallowRef } from 'vue'
import { listSessions, createSession, deleteSession, listRecords } from '../api/chat'
import { streamChat } from '../api/sse'

export function useChat() {
  const state = reactive({
    sessions: [],
    currentSessionId: null,
    messages: [],          // [{role:'user'|'assistant', content, toolCalls:[], sources:[]}]
    streaming: false,
    toolStatus: null       // 当前进行中的工具提示 {name,label,status,round}
  })
  const client = shallowRef(null) // { abort }

  async function loadSessions() {
    // TODO(M4): const data = await listSessions(); state.sessions = data.list || []
    state.sessions = []
  }

  async function createNewSession() {
    // TODO(M4): const s = await createSession(); state.currentSessionId = s.session_id; 清空 messages
    return null
  }

  async function selectSession(sessionId) {
    // 先清空再拉取历史回填，避免串会话（§7.4）
    state.currentSessionId = sessionId
    state.messages = []
    // TODO(M4): const data = await listRecords(sessionId); 按时间正序回填 messages
  }

  async function removeSession(sessionId) {
    // TODO(M4): await deleteSession(sessionId); 刷新列表；若删除当前会话则重置
  }

  function sendMessage(query) {
    if (state.streaming || !query.trim()) return
    if (!state.currentSessionId) state.currentSessionId = null // TODO(M4): 未建会话先自动建

    state.messages.push({ role: 'user', content: query })
    const assistant = reactive({ role: 'assistant', content: '', toolCalls: [], sources: [] })
    state.messages.push(assistant)
    state.streaming = true

    // TODO(M3/M4): 接通 streamChat，回调更新 assistant.content / toolCalls / sources
    // onTool  -> state.toolStatus / assistant.toolCalls
    // onToken -> assistant.content += text
    // onSource-> assistant.sources = sources
    // onDone  -> state.streaming=false; 清空 toolStatus
    // onError -> 气泡内展示 msg；onAbort -> 复位 streaming
    client.value = { abort() {} }
    state.streaming = false // 骨架占位：实际由事件回调收尾
  }

  function stop() {
    // 中断 SSE；finally 复位 streaming、恢复输入框（§3.5）
    client.value?.abort?.()
    state.streaming = false
    state.toolStatus = null
  }

  return { state, loadSessions, createNewSession, selectSession, removeSession, sendMessage, stop }
}
