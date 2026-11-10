// 会话与历史记录 REST 接口；流式问答见 sse.js
import http from './http'

export function createSession(sessionName = '新会话') {
  return http.post('/chat/session', { session_name: sessionName })
}

export function listSessions() {
  return http.get('/chat/session/list')
}

// PATCH /api/chat/session/{id} —— 重命名本人会话（越权后端返回 40402）
export function renameSession(sessionId, sessionName) {
  return http.patch(`/chat/session/${sessionId}`, { session_name: sessionName })
}

export function deleteSession(sessionId) {
  return http.delete(`/chat/session/${sessionId}`)
}

export function listRecords(sessionId) {
  return http.get(`/chat/record/${sessionId}`)
}
