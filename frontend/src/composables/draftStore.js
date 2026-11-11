// 草稿本地持久化：仅保存未发送问题，不持久化会话历史；退出登录时由根组件统一清理。
const DRAFT_KEY = 'rag-drafts-v1'

export function readDrafts() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

export function persistDrafts(drafts) {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(drafts || {}))
  } catch {
    // 隐私模式或存储不可用时退化为内存草稿，不阻断问答流程
  }
}

export function clearDraftsStorage() {
  try {
    localStorage.removeItem(DRAFT_KEY)
  } catch {
    // 忽略清理失败
  }
}
