// 管理员运行时配置接口：读取 / 保存 / 恢复默认
// 后端 GET/PUT /api/admin/config、POST /api/admin/config/reset（仅 admin）
import http from './http'

export function fetchAppConfig() {
  return http.get('/admin/config')
}

// values 只需传改动项：{ TOP_K: 8, LLM_TEMPERATURE: 0.6 }
export function saveAppConfig(values) {
  return http.put('/admin/config', { values })
}

// keys 或 group 二选一；都不传表示全部恢复默认
export function resetAppConfig({ keys, group } = {}) {
  const payload = {}
  if (keys?.length) payload.keys = keys
  if (group) payload.group = group
  return http.post('/admin/config/reset', payload)
}
