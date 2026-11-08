// 知识库管理接口（需求 §7.1）。骨架阶段固定契约，后端 M1 接通后即可直接调用。
import http from './http'

// POST /api/docs/upload —— multipart 上传，同步处理后返回 {doc_id, chunk_count, status}
export function uploadDoc(file, onUploadProgress) {
  const form = new FormData()
  form.append('file', file)
  return http.post('/docs/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress
  })
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
