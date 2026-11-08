// 会话与历史记录接口（需求 §7.2 / §7.3 REST 部分；流式见 sse.js）
import http from './http'

export function createSession(sessionName = '新会话') {
  return http.post('/chat/session', { session_name: sessionName })
}

export function listSessions() {
  return http.get('/chat/session/list')
}

export function deleteSession(sessionId) {
  return http.delete(`/chat/session/${sessionId}`)
}

export function listRecords(sessionId) {
  return http.get(`/chat/record/${sessionId}`)
}
