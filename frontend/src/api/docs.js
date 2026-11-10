// 知识库管理接口；仅管理员可调用，后端会强制校验权限
import http from './http'

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

// POST /api/docs/{doc_id}/reparse —— 重读原件并事务替换切片向量
export function reparseDoc(docId) {
  return http.post(`/docs/${docId}/reparse`)
}

// POST /api/docs/reindex —— 重建 HNSW 向量索引
export function rebuildIndex() {
  return http.post('/docs/reindex')
}

// GET /api/docs/list?page=&size=
export function listDocs(page = 1, size = 10) {
  return http.get('/docs/list', { params: { page, size } })
}

// DELETE /api/docs/{doc_id}
export function deleteDoc(docId) {
  return http.delete(`/docs/${docId}`)
}

// GET /api/system/info
export function fetchSystemInfo() {
  return http.get('/system/info')
}
