// 对话状态编排：会话列表、当前会话、消息缓冲、流式状态与中断控制器
// 切换会话时重置本地消息缓冲；历史记录由后端按当前用户归属返回
import { reactive, shallowRef } from 'vue'
import { listSessions, createSession, deleteSession, renameSession as renameSessionApi, listRecords } from '../api/chat'
import { streamChat } from '../api/sse'

export function useChat() {
  const state = reactive({
    sessions: [],
    currentSessionId: null,
    messages: [], // [{role, content, toolCalls:[{name,label,status,round}], sources:[{doc_name,chunk_index,score,snippet}], streaming, error}]
    streaming: false,
    toolStatus: null // 当前进行中的工具提示 {name,label,status,round}
  })
  const client = shallowRef(null) // { abort }

  async function loadSessions() {
    try {
      const data = await listSessions()
      state.sessions = data?.list || []
    } catch (_) {
      state.sessions = []
    }
  }

  async function createNewSession() {
    try {
      const s = await createSession('新会话')
      state.currentSessionId = s.session_id
      state.messages = []
      state.toolStatus = null
      await loadSessions()
      return s.session_id
    } catch (e) {
      state.messages.push({
        role: 'assistant', content: `会话创建失败：${e.message || '请确认后端已启动'}`,
        toolCalls: [], sources: [], error: true
      })
      return null
    }
  }

  async function selectSession(sessionId) {
    if (state.streaming) stop() // 流式切换先中断，避免两套流写同一缓冲
    state.currentSessionId = sessionId
    state.messages = []
    state.toolStatus = null
    try {
      const data = await listRecords(sessionId)
      const msgs = []
      for (const r of (data?.list || [])) {
        msgs.push({ role: 'user', content: r.query, toolCalls: [], sources: [] })
        msgs.push({
          role: 'assistant',
          content: r.answer || '',
          toolCalls: r.tool_call_info || [],
          sources: r.source_ref || []
        })
      }
      state.messages = msgs
    } catch (e) {
      // 会话不存在或无权限时清空选中态并刷新列表，避免保留失效选中项
      state.currentSessionId = null
      state.messages = []
      state.toolStatus = null
      await loadSessions()
      if (e.bizCode !== 40101 && e.httpStatus !== 401) {
        state.messages.push({
          role: 'assistant', content: `历史记录加载失败：${e.message || ''}`,
          toolCalls: [], sources: [], error: true
        })
      }
    }
  }

  async function renameSession(sessionId, name) {
    const clean = (name || '').trim()
    if (!clean) return false
    try {
      await renameSessionApi(sessionId, clean)
      await loadSessions()
      return true
    } catch (_) {
      // 失败提示由 http 拦截器统一处理；返回 false 让调用方保持编辑态
      return false
    }
  }

  async function removeSession(sessionId) {
    if (state.streaming) stop() // 删除前先中断进行中的流，避免继续写入已删除会话
    try {
      await deleteSession(sessionId)
      if (state.currentSessionId === sessionId) {
        state.currentSessionId = null
        state.messages = []
        state.toolStatus = null
      }
      await loadSessions()
    } catch (_) { /* http 拦截器统一提示，这里保持静默不阻塞列表 */ }
  }

  async function sendMessage(query) {
    const q = (query || '').trim()
    if (state.streaming || !q) return

    // 后端要求会话存在；无当前会话时先自动新建
    let sid = state.currentSessionId
    if (!sid) {
      sid = await createNewSession()
      if (!sid) return
    }

    state.messages.push({ role: 'user', content: q, toolCalls: [], sources: [] })
    const assistant = reactive({
      role: 'assistant', content: '', toolCalls: [], sources: [],
      streaming: true, error: false
    })
    state.messages.push(assistant)
    state.streaming = true
    state.toolStatus = null

    client.value = streamChat({ session_id: sid, query: q }, {
      onTool(t) {
        assistant.toolCalls.push({ ...t })
        state.toolStatus = t
      },
      onToken({ text }) {
        assistant.content += text || ''
      },
      onSource({ sources }) {
        assistant.sources = sources || []
      },
      // 事件 done 帧与读取结束都可能触发，保持幂等复位
      onDone(data) {
        assistant.streaming = false
        state.streaming = false
        state.toolStatus = null
        client.value = null
        // saved=false 表示本次回答未落库（会话失效等）；轻提示但不标记为错误
        if (data && data.saved === false) {
          assistant.content = assistant.content
            ? assistant.content + '\n（本次回答未保存到历史：会话已失效）'
            : '（本次回答未保存到历史：会话已失效）'
        }
        loadSessions().catch(() => {}) // 刷新会话记录数；失败静默
      },
      onError({ code, msg }) {
        assistant.error = true
        assistant.streaming = false
        assistant.content =
          (assistant.content ? assistant.content + '\n' : '') + `⚠ ${msg || '请求失败'}${code ? `（${code}）` : ''}`
        state.streaming = false
        state.toolStatus = null
        client.value = null
      },
      onAbort() {
        assistant.streaming = false
        assistant.content = assistant.content
          ? assistant.content + '\n（已中断回答）'
          : '（已中断回答）'
        state.streaming = false
        state.toolStatus = null
        client.value = null
      }
    })
  }

  function stop() {
    client.value?.abort?.()
    // 最终复位由 onAbort/onError 完成；此处兜底，避免异常情况下输入框永久禁用
    state.streaming = false
    state.toolStatus = null
  }

  return { state, loadSessions, createNewSession, selectSession, removeSession, renameSession, sendMessage, stop }
}
