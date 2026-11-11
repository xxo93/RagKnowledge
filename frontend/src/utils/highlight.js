// 来源片段命中词高亮：只做精确词/短语匹配，不引入中文分词库。
// 所有展示文本先转义 HTML，再在转义后的文本上包裹 <mark>，避免来源内容注入。

const HTML_ESCAPE_MAP = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => HTML_ESCAPE_MAP[ch])
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function extractHighlightTerms(query) {
  const text = String(query || '').trim()
  if (!text) return []
  const matches = text.match(/[A-Za-z0-9_#+.]{2,}|[一-龥]{2,}/g) || []
  const unique = [...new Set(matches.map((t) => t.trim()).filter(Boolean))]
  return unique.sort((a, b) => b.length - a.length)
}

export function highlightText(text, terms) {
  const safe = escapeHtml(text)
  if (!safe) return ''
  const activeTerms = (terms || []).filter(Boolean)
  if (!activeTerms.length) return safe

  let html = safe
  for (const term of activeTerms) {
    const escapedTerm = escapeRegExp(escapeHtml(term))
    html = html.replace(new RegExp(escapedTerm, 'ig'), '<mark class="hit-mark">$&</mark>')
  }
  return html
}
