// 产品品牌常量：界面名称、副标题与头像统一从这里维护，避免散落硬编码。

const BRAND_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="8" fill="#2563eb"/>
  <path d="M9 11h14v9H14l-5 5z" fill="#ffffff"/>
  <circle cx="14" cy="15.5" r="1.2" fill="#2563eb"/>
  <circle cx="19" cy="15.5" r="1.2" fill="#2563eb"/>
</svg>`

export const AGENT_NAME = '知言'
export const AGENT_TAGLINE = '企业文档智能问答助手'
export const AGENT_AVATAR = `data:image/svg+xml,${encodeURIComponent(BRAND_SVG)}`

export const EXAMPLE_QUESTIONS = [
  '知识库里关于索引优化的内容有哪些？',
  '1234 × 56 等于多少？',
  '知识库现在一共有多少份文档？',
  '总结一下最近上传的文档要点'
]
