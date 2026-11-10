// 认证接口：登录、登出、当前用户、修改密码；Cookie 由浏览器自动携带，前端不保存密码。
import http from './http'

export function login(payload) {
  return http.post('/auth/login', payload)
}

export function logout() {
  return http.post('/auth/logout')
}

export function fetchMe() {
  return http.get('/auth/me')
}

// POST /api/auth/password —— 本人改密；成功后其他设备登录态被后端吊销
export function changePassword(payload) {
  return http.post('/auth/password', payload)
}

// GET /api/auth/demo-hint —— 演示账号提示；后端开关关闭时返回 {enabled:false}
export function fetchDemoHint() {
  return http.get('/auth/demo-hint')
}
