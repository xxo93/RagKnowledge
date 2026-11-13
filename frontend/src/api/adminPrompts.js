// 管理员提示词版本接口（v3 / WP5-7）
import http from './http'

export function fetchPrompts() {
  return http.get('/admin/prompts')
}

export function createPromptVersion(name, content, grayPercent) {
  return http.post(`/admin/prompts/${name}/versions`, {
    content,
    gray_percent: grayPercent
  })
}

export function updatePromptGray(name, grayPercent) {
  return http.patch(`/admin/prompts/${name}/gray`, { gray_percent: grayPercent })
}

export function activatePromptVersion(name, version, grayPercent = 100) {
  return http.post(`/admin/prompts/${name}/activate`, {
    version,
    gray_percent: grayPercent
  })
}
