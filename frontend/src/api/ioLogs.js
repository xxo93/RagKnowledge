// v3 / WP3：运行日志与用户数据权利接口
import http from './http'

function clean(params = {}) {
  const out = {}
  Object.entries(params).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) out[k] = v
  })
  return out
}

export function listIoLogs(params) {
  return http.get('/admin/io-logs', { params: clean(params) })
}

export function fetchIoStats() {
  return http.get('/admin/io-logs/stats')
}

export function fetchIoDetail(traceId) {
  return http.get(`/admin/io-logs/${encodeURIComponent(traceId)}`)
}

// 用户侧：本人单轮链路详情（消息「链路」抽屉）
export function fetchMyTrace(traceId) {
  return http.get(`/me/io-logs/${encodeURIComponent(traceId)}`)
}

export function exportIoLogs(params) {
  return http.get('/admin/io-logs/export', { params: clean(params) })
}

// 用户侧：导出本人日志（JSON 附件）
export async function exportMyLogs() {
  const resp = await http.get('/me/io-logs/export', { responseType: 'blob' })
  const url = URL.createObjectURL(resp)
  const a = document.createElement('a')
  a.href = url
  a.download = `chat-io-logs-${new Date().toISOString().slice(0, 10)}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}

export function deleteMyLogs() {
  return http.delete('/me/io-logs')
}
