// 对话状态编排：会话列表、当前会话、消息缓冲、流式状态与中断控制器
// 切换会话时重置本地消息缓冲；历史记录由后端按当前用户归属返回
import { reactive, shallowRef } from 'vue'
import { listSessions, createSession, deleteSession, renameSession as renameSessionApi, listRecords, fetchSuggestions } from '../api/chat'
import { streamChat } from '../api/sse'
import { readDrafts, persistDrafts, clearDraftsStorage } from './draftStore'
import { EXAMPLE_QUESTIONS } from '../config/brand.js'

export function useChat() {
  const state = reactive({
    sessions: [],
    sessionsLoading: true,
    currentSessionId: null,
    messages: [], // [{role, content, toolCalls:[{name,label,status,round}], sources:[{doc_name,chunk_index,score,snippet}], streaming, error}]
    streaming: false,
    toolStatus: null, // 当前进行中的工具提示 {name,label,status,round}
    pendingTitleSessionId: null, // 首问落库前的自动命名占位会话
    drafts: readDrafts(), // 按会话保留未发送草稿，刷新后仍在；退出登录时统一清理
    sessionsTotal: 0,
    sessionsPage: 1,
    sessionsLoadingMore: false,
    sessionKeyword: '',
    recordsTotal: 0,
    recordsOffset: 0,
    // 正文每页条数与会话列表每页条数是两种语义，各自独立，避免调整其中一个时影响另一个
    recordsLimit: 20,
    sessionsPageSize: 20,
    recordsLoadingEarlier: false,
    canLoadEarlier: false,
    suggestions: [...EXAMPLE_QUESTIONS]
  })
  const client = shallowRef(null) // { abort }

  async function loadSessions(page = 1, append = false, keyword = state.sessionKeyword) {
    const targetPage = Math.max(1, page)
    state.sessionKeyword = (keyword || '').trim()
    if (append) state.sessionsLoadingMore = true
    else state.sessionsLoading = true
    try {
      const params = { page: targetPage, size: state.sessionsPageSize }
      if (state.sessionKeyword) params.keyword = state.sessionKeyword
      const data = await listSessions(params)
      const list = data?.list || []
      state.sessionsTotal = data?.total ?? list.length
      if (append) {
        const known = new Set(state.sessions.map((s) => s.session_id))
        state.sessions = [...state.sessions, ...list.filter((s) => !known.has(s.session_id))]
      } else {
        state.sessions = list
      }
      state.sessionsPage = targetPage
    } catch (_) {
      if (!append) state.sessions = []
      state.sessionsTotal = state.sessions.length
    } finally {
      state.sessionsLoading = false
      state.sessionsLoadingMore = false
    }
  }

  function loadMoreSessions() {
    if (state.sessionsLoadingMore || state.sessions.length >= state.sessionsTotal) return
    return loadSessions(state.sessionsPage + 1, true)
  }

  function searchSessions(keyword) {
    return loadSessions(1, false, keyword)
  }

  async function loadSuggestions() {
    try {
      const data = await fetchSuggestions()
      const questions = Array.isArray(data?.questions) ? data.questions : []
      if (questions.length) state.suggestions = questions.slice(0, 4)
    } catch (_) {
      // 示例问题属于引导能力，接口失败时保留静态兜底，不阻塞对话页
      state.suggestions = [...EXAMPLE_QUESTIONS]
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

  function recordsToMessages(records) {
    const msgs = []
    let pendingQuery = ''
    for (const r of records) {
      pendingQuery = r.query
      msgs.push({ role: 'user', content: r.query, toolCalls: [], sources: [] })
      msgs.push({
        role: 'assistant',
        query: pendingQuery,
        content: r.answer || '',
        toolCalls: r.tool_call_info || [],
        sources: r.source_ref || []
      })
    }
    return msgs
  }

  async function selectSession(sessionId) {
    if (state.streaming) stop() // 流式切换先中断，避免两套流写同一缓冲
    state.currentSessionId = sessionId
    state.messages = []
    state.toolStatus = null
    state.pendingTitleSessionId = null
    state.recordsTotal = 0
    state.recordsOffset = 0
    state.canLoadEarlier = false
    try {
      const data = await listRecords(sessionId, { limit: state.recordsLimit })
      const records = data?.list || []
      state.recordsTotal = data?.total ?? records.length
      state.recordsOffset = Math.max(0, state.recordsTotal - records.length)
      state.canLoadEarlier = state.recordsOffset > 0
      state.messages = recordsToMessages(records)
    } catch (e) {
      // 会话不存在或无权限时清空选中态并刷新列表，避免保留失效选中项
      state.currentSessionId = null
      state.messages = []
      state.toolStatus = null
      await loadSessions()
      const sessionInvalid = e.bizCode === 40402 || e.httpStatus === 404
      if (e.bizCode !== 40101 && e.httpStatus !== 401) {
        state.messages.push({
          role: 'assistant',
          content: sessionInvalid ? '会话已失效或无权访问，请新建会话。' : `历史记录加载失败：${e.message || ''}`,
          toolCalls: [], sources: [], error: true
        })
      }
    }
  }

  async function loadEarlierRecords() {
    if (!state.currentSessionId || state.recordsLoadingEarlier || !state.canLoadEarlier) return 0
    const nextOffset = Math.max(0, state.recordsOffset - state.recordsLimit)
    state.recordsLoadingEarlier = true
    try {
      const data = await listRecords(state.currentSessionId, {
        limit: state.recordsLimit,
        offset: nextOffset
      })
      const older = data?.list || []
      state.messages = [...recordsToMessages(older), ...state.messages]
      state.recordsOffset = nextOffset
      state.recordsTotal = data?.total ?? state.recordsTotal
      state.canLoadEarlier = nextOffset > 0
      return older.length
    } catch (_) {
      return 0
    } finally {
      state.recordsLoadingEarlier = false
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

  function setDraft(sessionId, text) {
    if (!sessionId) return
    const value = text || ''
    if (value) state.drafts[sessionId] = value
    else delete state.drafts[sessionId]
    persistDrafts(state.drafts)
  }

  function clearDrafts() {
    state.drafts = {}
    clearDraftsStorage()
  }

  async function removeSession(sessionId) {
    if (state.streaming) stop() // 删除前先中断进行中的流，避免继续写入已删除会话
    try {
      await deleteSession(sessionId)
      delete state.drafts[sessionId]
      persistDrafts(state.drafts)
      if (state.currentSessionId === sessionId) {
        state.currentSessionId = null
        state.messages = []
        state.toolStatus = null
        state.pendingTitleSessionId = null
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

    const session = state.sessions.find((x) => x.session_id === sid)
    const currentName = (session?.session_name || '').trim()
    if (!session || ['新会话', ''].includes(currentName)) {
      state.pendingTitleSessionId = sid
    }

    state.messages.push({ role: 'user', content: q, toolCalls: [], sources: [] })
    const assistant = reactive({
      role: 'assistant', query: q, content: '', toolCalls: [], sources: [],
      streaming: true, error: false, aborted: false, saved: true,
      errorCode: null, errorMsg: '', elapsed_ms: null, source_count: null
    })
    state.messages.push(assistant)
    state.streaming = true
    state.toolStatus = null

    client.value = streamChat({ session_id: sid, query: q }, {
      onTool(t) {
        // 同一工具同一轮先收 running、后收 done；按 name+round 原地更新，避免重复标签
        const idx = assistant.toolCalls.findIndex(
          (x) => x.name === t.name && Number(x.round) === Number(t.round)
        )
        if (idx >= 0) assistant.toolCalls[idx] = { ...assistant.toolCalls[idx], ...t }
        else assistant.toolCalls.push({ ...t })
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
        assistant.elapsed_ms = data?.elapsed_ms ?? null
        assistant.source_count = data?.source_count ?? assistant.sources.length
        state.pendingTitleSessionId = null

        // 后端首问自动提炼标题；就地更新当前列表项，避免刷新前闪烁旧标题
        if (data?.session_title) {
          const session = state.sessions.find((x) => x.session_id === sid)
          if (session) session.session_name = data.session_title
        }
        // saved=false 表示本次回答未落库（会话失效等）；轻提示但不标记为系统错误
        assistant.saved = data?.saved !== false
        loadSessions().catch(() => {}) // 刷新会话记录数；失败静默
      },
      onError({ code, msg }) {
        assistant.error = true
        assistant.errorCode = code || null
        assistant.errorMsg = msg || '请求失败'
        assistant.streaming = false
        state.streaming = false
        state.toolStatus = null
        state.pendingTitleSessionId = null
        client.value = null
        setDraft(sid, q)
      },
      onAbort() {
        assistant.aborted = true
        assistant.streaming = false
        state.streaming = false
        state.toolStatus = null
        state.pendingTitleSessionId = null
        client.value = null
        setDraft(sid, q)
      }
    })
  }

  function retryMessage(message) {
    const query = (message?.query || '').trim()
    if (!query || state.streaming) return
    sendMessage(query)
  }

  function stop() {
    client.value?.abort?.()
    // 最终复位由 onAbort/onError 完成；此处兜底，避免异常情况下输入框永久禁用
    state.streaming = false
    state.toolStatus = null
  }

  return {
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
    clearDrafts,
    sendMessage,
    retryMessage,
    stop
  }
}
