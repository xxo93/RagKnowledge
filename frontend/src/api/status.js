/** 顶栏能力状态（登录用户可见的轻量接口，不像 /system/info 那样仅限管理员）。 */
import http from './http'

export function fetchSystemStatus() {
  return http.get('/system/status')
}
