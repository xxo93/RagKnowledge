// 会话记忆接口（v3 / WP1）；路径与后端 /api/sessions/{id}/memory* 对齐
import http from './http'

export function getSessionMemory(sessionId) {
  return http.get(`/sessions/${sessionId}/memory`)
}

export function clearSessionMemory(sessionId) {
  return http.delete(`/sessions/${sessionId}/memory`)
}

export function deleteMemoryFact(sessionId, memId) {
  return http.delete(`/sessions/${sessionId}/memory/facts/${memId}`)
}

export function getFactHistory(sessionId, memId) {
  return http.get(`/sessions/${sessionId}/memory/${memId}/history`)
}

export function updateMemorySettings(sessionId, memoryEnabled) {
  return http.put(`/sessions/${sessionId}/memory/settings`, { memory_enabled: memoryEnabled })
}
