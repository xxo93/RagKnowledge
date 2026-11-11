// 管理员用户管理接口：列表、创建、启禁用、重置密码
import http from './http'

// GET /api/admin/users?page=&size=&keyword=&role=&status= —— 用户列表（不含密码哈希）
export function listUsers(page = 1, size = 20, filters = {}) {
  const params = { page, size }
  if (filters.keyword) params.keyword = filters.keyword
  if (filters.role && filters.role !== 'all') params.role = filters.role
  if (filters.status && filters.status !== 'all') params.status = filters.status
  return http.get('/admin/users', { params })
}

// POST /api/admin/users —— 创建 member/admin 账号
export function createUser(payload) {
  return http.post('/admin/users', payload)
}

// PATCH /api/admin/users/{id}/status —— 启用(1)/禁用(0)；禁用会同步吊销其登录态
export function updateUserStatus(userId, status) {
  return http.patch(`/admin/users/${userId}/status`, { status })
}

// POST /api/admin/users/{id}/reset-password —— 重置他人密码并吊销其登录态
export function resetUserPassword(userId, newPassword) {
  return http.post(`/admin/users/${userId}/reset-password`, { new_password: newPassword })
}
