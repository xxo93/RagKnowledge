// 对话状态编排：会话列表、当前会话、消息缓冲、流式状态与中断控制器
// 切换会话时重置本地消息缓冲；历史记录由后端按当前用户归属返回
import { reactive, shallowRef } from 'vue'
import { listSessions, createSession, deleteSession, renameSession as renameSessionApi, listRecords, fetchSuggestions, pinSession as pinSessionApi, archiveSession as archiveSessionApi, batchSessions as batchSessionsApi } from '../api/chat'
import { streamChat } from '../api/sse'
import { fetchWorkflowTask } from '../api/workflow'
import { readDrafts, persistDrafts, clearDraftsStorage } from './draftStore'
import { EXAMPLE_QUESTIONS } from '../config/brand.js'

// v3 / WP2：回答模式选择器（默认「自动」），按浏览器会话保留而不是写进会话记忆
const MODE_KEY = 'rk.chatMode'
const MODE_VALUES = ['auto', 'knowledge', 'general', 'biz', 'workflow']

function readMode() {
  try {
    const value = sessionStorage.getItem(MODE_KEY)
    return MODE_VALUES.includes(value) ? value : 'auto'
  } catch {
    return 'auto'
  }
}

function persistMode(value) {
  try {
    sessionStorage.setItem(MODE_KEY, MODE_VALUES.includes(value) ? value : 'auto')
  } catch { /* 隐私模式下 sessionStorage 不可用，仅内存生效 */ }
}

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
    sessionsNextCursor: null,
    sessionsHasMore: false,
    sessionKeyword: '',
    // M8 / WP5-9：归档区按需加载，避免 200+ 会话一次性塞进侧栏
    archivedSessions: [],
    archivedTotal: 0,
    archivedLoading: false,
    archivedVisible: false,
    archivedPage: 1,
    archivedPageSize: 50,
    recordsTotal: 0,
    recordsOffset: 0,
    // 正文每页条数与会话列表每页条数是两种语义，各自独立，避免调整其中一个时影响另一个
    recordsLimit: 20,
    sessionsPageSize: 50,
    recordsLoadingEarlier: false,
    canLoadEarlier: false,
    // 当前会话是否开启长期记忆（来自 done 帧 context_usage；仅用于顶栏提示）
    memoryEnabled: true,
    // v3 / WP2：当前回答模式（auto / knowledge / general / biz / workflow）
    mode: readMode(),
    suggestions: [...EXAMPLE_QUESTIONS]
  })
  const client = shallowRef(null) // { abort }
  // v4 / N7：只用最后一次会话列表请求的结果，丢弃快速切换关键词时的过期响应。
  let sessionsSeq = 0

  function setMode(value) {
    state.mode = MODE_VALUES.includes(value) ? value : 'auto'
    persistMode(state.mode)
  }

  async function loadSessions(page = 1, append = false, keyword = state.sessionKeyword) {
    const targetPage = Math.max(1, page)
    const seq = ++sessionsSeq
    state.sessionKeyword = (keyword || '').trim()
    if (append) state.sessionsLoadingMore = true
    else {
      state.sessionsLoading = true
      state.sessionsNextCursor = null
    }
    try {
      const params = {}
      // v4 / N1：追加优先走 keyset；老后端没有 next_cursor 时退回 page/size
      if (append && state.sessionsNextCursor) {
        params.cursor_last_active_at = state.sessionsNextCursor.last_active_at
        params.cursor_session_id = state.sessionsNextCursor.session_id
        params.cursor_is_pinned = state.sessionsNextCursor.is_pinned
      } else {
        params.page = targetPage
      }
      if (state.sessionKeyword) params.keyword = state.sessionKeyword
      const data = await listSessions(params)
      if (seq !== sessionsSeq) return
      const list = data?.list || []
      state.sessionsTotal = data?.total ?? list.length
      state.archivedTotal = data?.archived_total ?? state.archivedTotal
      state.sessionsNextCursor = data?.next_cursor || null
      if (append) {
        const known = new Set(state.sessions.map((s) => s.session_id))
        state.sessions = [...state.sessions, ...list.filter((s) => !known.has(s.session_id))]
      } else {
        state.sessions = list
      }
      state.sessionsHasMore = data?.has_more ?? Boolean(state.sessionsNextCursor)
      if (state.sessionsNextCursor == null && !state.sessionsHasMore) {
        state.sessionsHasMore = state.sessions.length < state.sessionsTotal
      }
      state.sessionsPage = targetPage
    } catch (_) {
      if (seq !== sessionsSeq) return
      if (!append) state.sessions = []
      state.sessionsTotal = state.sessions.length
      state.sessionsNextCursor = null
      state.sessionsHasMore = false
    } finally {
      if (seq === sessionsSeq) {
        state.sessionsLoading = false
        state.sessionsLoadingMore = false
      }
    }
  }

  function loadMoreSessions() {
    if (state.sessionsLoadingMore || state.sessionsLoading || !state.sessionsHasMore) return
    if (state.sessionsNextCursor) return loadSessions(1, true)
    return loadSessions(state.sessionsPage + 1, true)
  }

  function searchSessions(keyword) {
    state.sessionsNextCursor = null
    return loadSessions(1, false, keyword)
  }

  async function loadArchivedSessions() {
    if (state.archivedLoading) return
    state.archivedLoading = true
    try {
      const data = await listSessions({
        page: 1,
        size: state.archivedPageSize,
        archived: true,
        keyword: state.sessionKeyword || undefined
      })
      state.archivedSessions = data?.list || []
      state.archivedTotal = data?.archived_total ?? state.archivedSessions.length
      state.archivedPage = 1
    } catch (_) {
      state.archivedSessions = []
    } finally {
      state.archivedLoading = false
    }
  }

  async function toggleArchived(visible) {
    state.archivedVisible = visible
    if (visible) await loadArchivedSessions()
  }

  async function togglePin(sessionId, pinned) {
    try {
      await pinSessionApi(sessionId, pinned)
      await loadSessions()
      return true
    } catch (_) {
      return false
    }
  }

  async function archiveSession(sessionId) {
    if (state.streaming && state.currentSessionId === sessionId) stop()
    try {
      await archiveSessionApi(sessionId, true)
      if (state.currentSessionId === sessionId) {
        state.currentSessionId = null
        state.messages = []
        state.toolStatus = null
      }
      await loadSessions()
      if (state.archivedVisible) await loadArchivedSessions()
      return true
    } catch (_) {
      return false
    }
  }

  async function restoreSession(sessionId, selectAfter = false) {
    try {
      await archiveSessionApi(sessionId, false)
      await loadSessions()
      if (state.archivedVisible) await loadArchivedSessions()
      if (selectAfter) await selectSession(sessionId)
      return true
    } catch (_) {
      return false
    }
  }

  async function batchSessionAction(action, sessionIds) {
    if (!Array.isArray(sessionIds) || !sessionIds.length) {
      return { succeeded: [], failed: [] }
    }
    try {
      const data = await batchSessionsApi(action, sessionIds)
      // 批量归档若包含当前会话，先退出当前会话，避免正文停留在已归档项
      if (action === 'archive' && sessionIds.includes(state.currentSessionId)) {
        state.currentSessionId = null
        state.messages = []
        state.toolStatus = null
      }
      await loadSessions()
      if (state.archivedVisible) await loadArchivedSessions()
      return data
    } catch (_) {
      return { succeeded: [], failed: sessionIds.map((id) => ({ session_id: id, code: 50001, msg: '批量操作失败' })) }
    }
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
        sources: r.source_ref || [],
        // 历史回放同样带上能力链路，前端才能标出"这条不是内部知识库答的"
        answerMode: r.answer_mode || 'knowledge',
        // v3：历史记录也回放意图，徽标/模式提示与当轮一致
        intent: r.intent || null,
        // v3 / WP5：历史消息也保留 trace_id，刷新后仍可打开「链路」时间线
        traceId: r.trace_id || null,
        // v3 / WP5-3：关联记录与已提交的反馈状态，刷新后仍显示“已反馈”
        recordId: r.record_id || null,
        feedbackRating: r.feedback_rating ?? null,
        // v3 / WP5-10：依据强度，后端按 answer_mode + 来源数统一判定，历史回放同样带回
        evidence: r.evidence || null,
        notice: null,
        // 业务查询的表格帧已随记录落库（table_payload），刷新/切页后仍能还原表格卡与下载链接
        table: r.table_payload || null,
        // v3 / WP4：任务卡关联的任务 id；详情在会话加载后异步补齐
        workflowTaskId: r.workflow_task_id || null,
        workflowTask: null
      })
    }
    return msgs
  }

  // v3 / WP4：历史消息只存 workflow_task_id，进入会话后异步拉取任务快照，PlanCard 可在历史回放中展开
  async function hydrateWorkflowTasks() {
    const ids = [...new Set(state.messages.map((m) => m.workflowTaskId).filter(Boolean))]
    if (!ids.length) return
    await Promise.all(ids.map(async (id) => {
      try {
        const task = await fetchWorkflowTask(id)
        for (const m of state.messages) {
          if (m.workflowTaskId === id) m.workflowTask = task
        }
      } catch {
        // 任务已删除或无权访问：保留任务 id，气泡不渲染卡片
      }
    }))
  }

  async function selectSession(sessionId) {
    if (state.streaming) stop() // 流式切换先中断，避免两套流写同一缓冲
    state.currentSessionId = sessionId
    state.memoryEnabled = true // 未知时按默认开启；打开记忆面板会以接口实际值为准
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
      hydrateWorkflowTasks()
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
      hydrateWorkflowTasks()
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
      errorCode: null, errorMsg: '', elapsed_ms: null, source_count: null,
      answerMode: null, notice: null, table: null, intent: null, degradedReason: null,
      evidence: null,
      effectiveMode: state.mode, traceId: null, createdAt: Date.now(), first_token_ms: null,
      workflowTask: null, workflowTaskId: null
    })
    state.messages.push(assistant)
    state.streaming = true
    state.toolStatus = null

    client.value = streamChat({ session_id: sid, query: q, mode: state.mode }, {
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
      // 提示帧：联网兜底使用中 / 联网不可用；先于 token 到达，正文上方展示
      onNotice(data) {
        if (data?.msg) assistant.notice = { code: data.code || '', level: data.level || 'info', msg: data.msg }
      },
      // 表格帧（M4）：业务库查询结果；服务端已完成脱敏与行数限制
      onTable(data) {
        assistant.table = data || null
      },
      // 工作流计划帧（v3 / WP4）：任务快照交给 PlanCard，实时进度走任务事件流
      onWorkflow(data) {
        assistant.workflowTask = data?.task || null
        assistant.workflowTaskId = data?.task?.task_id || null
      },
      // 事件 done 帧与读取结束都可能触发，保持幂等复位
      onDone(data) {
        assistant.streaming = false
        state.streaming = false
        state.toolStatus = null
        client.value = null
        assistant.traceId = data?.trace_id || assistant.traceId
        assistant.recordId = data?.record_id ?? assistant.recordId
        assistant.first_token_ms = data?.first_token_ms ?? null
        assistant.elapsed_ms = data?.elapsed_ms ?? null
        assistant.source_count = data?.source_count ?? assistant.sources.length
        assistant.evidence = data?.evidence_strength || data?.context_usage?.evidence || null
        if (data?.answer_mode) assistant.answerMode = data.answer_mode
        // v3：done 帧携带意图与降级原因，前端徽标与调试信息都以此为准
        if (data?.intent) assistant.intent = data.intent
        if (data?.workflow_task_id) {
          assistant.workflowTaskId = data.workflow_task_id
          if (!assistant.workflowTask) {
            fetchWorkflowTask(data.workflow_task_id)
              .then((task) => { assistant.workflowTask = task })
              .catch(() => {})
          }
        }
        assistant.degradedReason = data?.degraded_reason || null
        assistant.effectiveMode = data?.mode || null
        // v3：done 帧携带上下文用量与记忆开关，供顶栏与记忆面板展示
        assistant.contextUsage = data?.context_usage || null
        if (data?.context_usage?.memory_enabled != null) {
          state.memoryEnabled = Boolean(data.context_usage.memory_enabled)
        }
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
      onError({ code, msg, traceId }) {
        assistant.error = true
        assistant.errorCode = code || null
        assistant.errorMsg = msg || '请求失败'
        assistant.traceId = traceId || client.value?.traceId || assistant.traceId
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
    // 前端先生成 trace_id 并随请求头发送；后续 done/error 帧会带回同一值
    assistant.traceId = client.value?.traceId || null
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
    loadArchivedSessions,
    toggleArchived,
    togglePin,
    archiveSession,
    restoreSession,
    batchSessionAction,
    setDraft,
    clearDrafts,
    setMode,
    sendMessage,
    retryMessage,
    stop
  }
}
