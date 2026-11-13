// 会话与历史记录 REST 接口；流式问答见 sse.js
import http from './http'

export function createSession(sessionName = '新会话') {
  return http.post('/chat/session', { session_name: sessionName })
}

export function listSessions(params = {}) {
  return http.get('/chat/session/list', { params })
}

// PATCH /api/chat/session/{id} —— 重命名本人会话（越权后端返回 40402）
export function renameSession(sessionId, sessionName) {
  return http.patch(`/chat/session/${sessionId}`, { session_name: sessionName })
}

export function deleteSession(sessionId) {
  return http.delete(`/chat/session/${sessionId}`)
}

// M8 / WP5-9：会话置顶与归档（归档后从主列表移入「已归档」，恢复后回到主列表）
export function pinSession(sessionId, pinned = true) {
  return http.patch(`/chat/session/${sessionId}/pin`, { pinned })
}

export function archiveSession(sessionId, archived = true) {
  return http.patch(`/chat/session/${sessionId}/archive`, { archived })
}

// POST /api/chat/session/batch —— 批量置顶/归档/恢复（不含删除）
export function batchSessions(action, sessionIds) {
  return http.post('/chat/session/batch', { action, session_ids: sessionIds })
}

export function listRecords(sessionId, params = {}) {
  return http.get(`/chat/record/${sessionId}`, { params })
}

// POST /api/chat/feedback —— 赞/踩 + 原因，进入评测集（v3 / WP5-3）
export function submitFeedback(payload) {
  return http.post('/chat/feedback', payload)
}

// GET /api/chat/session/{id}/export?format=md|json —— 含来源、工具记录与 trace
export async function exportSession(sessionId, format = 'md') {
  const resp = await http.get(`/chat/session/${sessionId}/export`, {
    params: { format },
    responseType: 'blob'
  })
  const url = URL.createObjectURL(resp)
  const a = document.createElement('a')
  a.href = url
  a.download = `session-${sessionId}-${new Date().toISOString().slice(0, 10)}.${format === 'json' ? 'json' : 'md'}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}

// GET /api/chat/suggestions —— 登录用户只读示例问题；失败时前端回退静态配置
export function fetchSuggestions() {
  return http.get('/chat/suggestions')
}
