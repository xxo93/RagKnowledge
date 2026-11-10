// axios 实例与统一响应解包；后端成功包络 code=0 时直接返回 data
// SSE 等非 axios 通道复用 notifyError 处理 401/403 等错误
import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 60000
})

let errorHandler = null
// 允许入口注入统一错误提示（Element Plus Message），保持 api 层不直接依赖 UI 组件
export function setErrorHandler(fn) {
  errorHandler = fn
}

// 供 SSE / 非 axios 通道复用统一错误处理（401 回登录态、403 提示无权限）
export function notifyError(err) {
  if (errorHandler) errorHandler(err)
}

http.interceptors.response.use(
  (resp) => {
    const body = resp.data
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 0) return body.data
      const err = new Error(body.msg || '请求失败')
      err.bizCode = body.code
      if (errorHandler) errorHandler(err)
      return Promise.reject(err)
    }
    return body
  },
  (error) => {
    const msg =
      error.response?.data?.msg || error.message || '网络异常，请稍后重试'
    const err = new Error(msg)
    err.httpStatus = error.response?.status
    err.bizCode = error.response?.data?.code
    if (errorHandler) errorHandler(err)
    return Promise.reject(err)
  }
)

export default http
