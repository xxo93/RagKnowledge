// 知识库管理接口；仅管理员可调用，后端会强制校验权限
import http, { notifyError } from './http'

// POST /api/docs/upload —— multipart 单文件上传，同步处理后返回 {doc_id, chunk_count, status}
export function uploadDoc(file, onUploadProgress) {
  const form = new FormData()
  form.append('file', file)
  return http.post('/docs/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress
  })
}

// POST /api/docs/upload/batch —— 服务端顺序处理多文件，返回 {success:[], failed:[], success_count, failed_count}
export function uploadDocsBatch(files) {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  return http.post('/docs/upload/batch', form, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

// 生成分块上传会话 id（优先 randomUUID，旧浏览器退化为手写 UUID v4）
export function makeUploadId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

/**
 * 分块上传单个文件（大文件不一次性读进内存，也不会触发单请求体积上限）
 *
 * @param {File} file 待上传文件
 * @param {object} options
 * @param {number} [options.chunkSize=4MB] 每片字节数，由调用方按 MB 换算
 * @param {number} [options.concurrency=3] 同时在传的分片数
 * @param {(p:{loaded:number,total:number,percent:number,chunkCount:number})=>void} [options.onProgress] 进度回调
 * @returns {Promise<{doc_id:number, chunk_count:number, status:number}>}
 */
export async function uploadDocChunked(file, options = {}) {
  const chunkSize = Math.max(1, options.chunkSize || 4 * 1024 * 1024)
  const concurrency = Math.max(1, options.concurrency || 3)
  const onProgress = options.onProgress
  const uploadId = makeUploadId()
  const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize))

  let loaded = 0
  let nextIndex = 0
  const report = () => onProgress?.({
    loaded,
    total: file.size,
    percent: file.size ? Math.min(100, Math.round((loaded / file.size) * 100)) : 100,
    chunkCount: totalChunks
  })

  // 并发池：固定 worker 数量，从共享游标取分片序号，避免一次性发起 N 个请求
  async function worker() {
    while (true) {
      const index = nextIndex++
      if (index >= totalChunks) return
      const start = index * chunkSize
      const blob = file.slice(start, Math.min(start + chunkSize, file.size))
      const form = new FormData()
      form.append('upload_id', uploadId)
      form.append('chunk_index', String(index))
      form.append('total_chunks', String(totalChunks))
      form.append('file', blob, `${file.name}.part${index}`)
      await http.post('/docs/upload/chunk', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 300000 // 单个分片的超时放宽到 5 分钟，避免弱网误判
      })
      loaded += blob.size
      report()
    }
  }

  try {
    await Promise.all(Array.from({ length: Math.min(concurrency, totalChunks) }, worker))
    const result = await http.post('/docs/upload/complete', {
      upload_id: uploadId,
      file_name: file.name,
      total_chunks: totalChunks
    }, { timeout: 300000 })
    onProgress?.({ loaded: file.size, total: file.size, percent: 100, chunkCount: totalChunks })
    return result
  } catch (e) {
    // 失败时清理服务端已落盘的分片，避免遗留临时文件
    http.delete('/docs/upload/chunk', { params: { upload_id: uploadId } }).catch(() => {})
    throw e
  }
}

// POST /api/docs/{doc_id}/reparse —— 重读原件并事务替换切片向量
export function reparseDoc(docId) {
  return http.post(`/docs/${docId}/reparse`)
}

// POST /api/docs/reindex —— 重建 HNSW 向量索引
export function rebuildIndex() {
  return http.post('/docs/reindex')
}

// GET /api/docs/list?page=&size=&keyword=&status=&start_date=&end_date=
export function listDocs(page = 1, size = 10, filters = {}) {
  const params = { page, size }
  if (filters.keyword) params.keyword = filters.keyword
  if (filters.status && filters.status !== 'all') params.status = filters.status
  if (filters.start_date) params.start_date = filters.start_date
  if (filters.end_date) params.end_date = filters.end_date
  return http.get('/docs/list', { params })
}

// DELETE /api/docs/{doc_id}
export function deleteDoc(docId) {
  return http.delete(`/docs/${docId}`)
}

/** 读取错误响应：下载/预览用原生 fetch，错误可能是 JSON，也可能是 Blob。 */
async function readDocError(res) {
  let body = {}
  try {
    const type = res.headers.get('content-type') || ''
    body = type.includes('application/json') ? await res.json() : {}
  } catch (_) {
    body = {}
  }
  const err = new Error(body.msg || `请求失败（HTTP ${res.status}）`)
  err.bizCode = body.code
  err.httpStatus = res.status
  if (res.status === 401) notifyError(err)
  return err
}

// GET /api/docs/{doc_id}/preview —— 已登录即可；PDF 返回 Blob，其他返回 JSON 可读内容
export async function fetchDocPreview(docId) {
  const res = await fetch(`/api/docs/${docId}/preview`, { credentials: 'include' })
  if (!res.ok) throw await readDocError(res)
  const type = res.headers.get('content-type') || ''
  if (type.includes('application/pdf')) {
    return { kind: 'pdf', blob: await res.blob() }
  }
  const body = await res.json()
  if (body?.code !== 0) {
    const err = new Error(body?.msg || '预览失败')
    err.bizCode = body?.code
    throw err
  }
  return body.data
}

// GET /api/docs/{doc_id}/download —— 仅管理员；返回原文件 Blob，由调用方触发保存
export async function downloadDocFile(docId) {
  const res = await fetch(`/api/docs/${docId}/download`, { credentials: 'include' })
  if (!res.ok) throw await readDocError(res)
  return res.blob()
}

// GET /api/system/info
export function fetchSystemInfo() {
  return http.get('/system/info')
}
