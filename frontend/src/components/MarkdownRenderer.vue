<script setup>
// Markdown 渲染：marked 解析 + DOMPurify 消毒 + 按需代码高亮；流式更新做节流。
import { computed, nextTick, ref, watch } from 'vue'
import { useStreamRender } from '../composables/useStreamRender'

const props = defineProps({
  content: { type: String, default: '' },
  streaming: { type: Boolean, default: false },
  sources: { type: Array, default: () => [] }
})
const emit = defineEmits(['cite'])

const rootRef = ref(null)
const html = ref('')
let markedPromise = null
let purifyPromise = null
let hljsPromise = null

const ALLOWED_URI = /^(?:https?:|mailto:)/i

function loadMarked() {
  if (!markedPromise) {
    markedPromise = import('marked').then((mod) => {
      const marked = mod.default || mod
      marked.setOptions({ gfm: true, breaks: false, html: false })
      return marked
    })
  }
  return markedPromise
}

function loadPurify() {
  if (!purifyPromise) {
    purifyPromise = import('dompurify').then((mod) => mod.default || mod)
  }
  return purifyPromise
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[char]))
}

async function sanitize(markdown) {
  try {
    const [marked, DOMPurify] = await Promise.all([loadMarked(), loadPurify()])
    const raw = marked.parse(markdown, { async: false })
    return DOMPurify.sanitize(raw, {
      ADD_ATTR: ['target', 'rel'],
      ALLOWED_URI_REGEXP: ALLOWED_URI
    })
  } catch {
    return escapeHtml(markdown)
  }
}

function enhanceAnchors(root) {
  root.querySelectorAll('a').forEach((anchor) => {
    const href = anchor.getAttribute('href') || ''
    if (ALLOWED_URI.test(href)) {
      anchor.setAttribute('target', '_blank')
      anchor.setAttribute('rel', 'noopener noreferrer')
    } else {
      anchor.removeAttribute('href')
    }
  })
}

function wrapCitations(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes = []
  while (walker.nextNode()) nodes.push(walker.currentNode)
  nodes.forEach((node) => {
    const text = node.nodeValue || ''
    if (!/\[\d+\]/.test(text)) return
    const fragment = document.createDocumentFragment()
    text.split(/(\[\d+\])/g).forEach((part) => {
      const match = part.match(/^\[(\d+)\]$/)
      if (!match) {
        if (part) fragment.appendChild(document.createTextNode(part))
        return
      }
      const index = Number(match[1]) - 1
      if (index < 0 || index >= props.sources.length) {
        fragment.appendChild(document.createTextNode(part))
        return
      }
      const source = props.sources[index] || {}
      const tipTitle = source.doc_title || source.doc_name || `来源 ${match[1]}`
      const tipSummary = (source.summary || source.snippet || '').slice(0, 40)
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'cite-ref'
      button.dataset.index = String(index)
      const tip = tipSummary ? `${tipTitle}：${tipSummary}` : tipTitle
      button.dataset.tip = tip
      button.textContent = part
      button.title = tip
      button.setAttribute('aria-label', `查看来源 ${match[1]}`)
      fragment.appendChild(button)
    })
    node.replaceWith(fragment)
  })
}

function bindCiteClick(root) {
  root.addEventListener('click', (event) => {
    const button = event.target.closest?.('.cite-ref')
    if (!button) return
    emit('cite', Number(button.dataset.index))
  })
}

async function loadHljs() {
  if (!hljsPromise) {
    hljsPromise = import('highlight.js/lib/core').then(async (core) => {
      const hljs = core.default || core
      const [python, sql, javascript, typescript, bash, json, yaml, java] = await Promise.all([
        import('highlight.js/lib/languages/python'),
        import('highlight.js/lib/languages/sql'),
        import('highlight.js/lib/languages/javascript'),
        import('highlight.js/lib/languages/typescript'),
        import('highlight.js/lib/languages/bash'),
        import('highlight.js/lib/languages/json'),
        import('highlight.js/lib/languages/yaml'),
        import('highlight.js/lib/languages/java')
      ])
      hljs.registerLanguage('python', python.default || python)
      hljs.registerLanguage('sql', sql.default || sql)
      hljs.registerLanguage('javascript', javascript.default || javascript)
      hljs.registerLanguage('typescript', typescript.default || typescript)
      hljs.registerLanguage('bash', bash.default || bash)
      hljs.registerLanguage('json', json.default || json)
      hljs.registerLanguage('yaml', yaml.default || yaml)
      hljs.registerLanguage('java', java.default || java)
      return hljs
    })
  }
  return hljsPromise
}

function addCopyButton(pre) {
  if (pre.querySelector('.markdown-copy-code')) return
  const code = pre.querySelector('code')
  if (!code) return
  const button = document.createElement('button')
  button.type = 'button'
  button.className = 'markdown-copy-code'
  button.textContent = '复制'
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(code.innerText)
      button.textContent = '已复制'
      setTimeout(() => { button.textContent = '复制' }, 1500)
    } catch {
      button.textContent = '复制失败'
    }
  })
  pre.appendChild(button)
}

// 代码块语言 → 下载文件扩展名
const EXT_MAP = {
  python: 'py',
  py: 'py',
  sql: 'sql',
  javascript: 'js',
  js: 'js',
  typescript: 'ts',
  ts: 'ts',
  bash: 'sh',
  shell: 'sh',
  json: 'json',
  yaml: 'yml',
  yml: 'yml',
  java: 'java',
  go: 'go',
  rust: 'rs',
  cpp: 'cpp',
  'c++': 'cpp',
  html: 'html',
  css: 'css',
  markdown: 'md'
}

function downloadCode(block, code, lang) {
  const ext = EXT_MAP[(lang || '').toLowerCase()] || 'txt'
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
  const blob = new Blob([code.textContent || ''], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `snippet-${stamp}.${ext}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function ensureCodeChrome(pre, code, lang) {
  if (pre.dataset.enhanced === '1') return
  pre.dataset.enhanced = '1'

  const lineCount = (code.textContent || '').replace(/\n$/, '').split('\n').length
  const block = document.createElement('div')
  block.className = 'code-block'
  pre.parentNode.insertBefore(block, pre)

  const toolbar = document.createElement('div')
  toolbar.className = 'code-toolbar'

  const langLabel = document.createElement('span')
  langLabel.className = 'markdown-lang'
  langLabel.textContent = lang || 'text'
  toolbar.appendChild(langLabel)

  const spacer = document.createElement('span')
  spacer.className = 'code-spacer'
  toolbar.appendChild(spacer)

  const mkButton = (text, title) => {
    const b = document.createElement('button')
    b.type = 'button'
    b.className = 'code-action'
    b.textContent = text
    b.title = title
    return b
  }

  // 行号开关：默认关，打开后左侧显示行号槽（复制/下载始终取纯代码，不含行号）
  const linesBtn = mkButton('行号', '显示 / 隐藏代码行号')
  linesBtn.setAttribute('aria-pressed', 'false')
  linesBtn.addEventListener('click', () => {
    const on = block.classList.toggle('show-lines')
    linesBtn.setAttribute('aria-pressed', String(on))
    linesBtn.classList.toggle('active', on)
  })
  toolbar.appendChild(linesBtn)

  const copyBtn = mkButton('复制', '复制完整代码')
  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(code.textContent || '')
      copyBtn.textContent = '已复制'
      setTimeout(() => { copyBtn.textContent = '复制' }, 1500)
    } catch {
      copyBtn.textContent = '复制失败'
    }
  })
  toolbar.appendChild(copyBtn)

  const dlBtn = mkButton('下载', `下载为 .${EXT_MAP[(lang || '').toLowerCase()] || 'txt'} 文件`)
  dlBtn.addEventListener('click', () => downloadCode(block, code, lang))
  toolbar.appendChild(dlBtn)

  block.appendChild(toolbar)

  // 行号槽 + 代码区并排；行号槽不参与代码复制/下载
  const main = document.createElement('div')
  main.className = 'code-main'
  const gutter = document.createElement('div')
  gutter.className = 'code-gutter'
  gutter.setAttribute('aria-hidden', 'true')
  gutter.textContent = Array.from({ length: lineCount }, (_, i) => i + 1).join('\n')
  main.appendChild(gutter)
  block.appendChild(main)
  main.appendChild(pre)

  // 超过 40 行默认折叠，展开后不再自动收起；流式过程中不折叠，避免阅读中断
  if (lineCount > 40 && !props.streaming) {
    block.classList.add('collapsed')
    const foldBtn = mkButton(`展开全部（${lineCount} 行）`, '展开完整代码')
    foldBtn.className = 'code-fold'
    foldBtn.addEventListener('click', () => {
      const collapsed = block.classList.toggle('collapsed')
      foldBtn.textContent = collapsed ? `展开全部（${lineCount} 行）` : '收起'
    })
    block.appendChild(foldBtn)
  }
}

async function enhanceCodeBlocks(root) {
  const blocks = Array.from(root.querySelectorAll('pre code[class*="language-"]'))
  if (!blocks.length) return
  try {
    const hljs = await loadHljs()
    blocks.forEach((block) => {
      if (!block.dataset.highlighted) {
        hljs.highlightElement(block)
        block.dataset.highlighted = '1'
      }
      const lang = (block.className.match(/language-([\w-]+)/) || [])[1]
      const pre = block.closest('pre')
      if (pre && !pre.dataset.enhanced) {
        ensureCodeChrome(pre, block, lang)
      }
    })
  } catch {
    // 高亮库加载失败时保留纯文本代码块，不影响正文阅读
  }
}

async function update() {
  html.value = await sanitize(props.content || '')
  await nextTick()
  const root = rootRef.value
  if (!root) return
  enhanceAnchors(root)
  wrapCitations(root)
  if (!root.dataset.citeBound) {
    bindCiteClick(root)
    root.dataset.citeBound = '1'
  }
  await enhanceCodeBlocks(root)
}

const { schedule } = useStreamRender(update, () => props.streaming)

watch(
  () => props.content,
  () => schedule(),
  { immediate: true }
)

const hasContent = computed(() => Boolean(props.content))
</script>

<template>
  <div
    v-if="hasContent"
    ref="rootRef"
    class="markdown-body"
    :aria-busy="streaming"
    aria-live="off"
    v-html="html"
  />
  <pre v-else class="markdown-body plain-text" aria-hidden="true" />
</template>

<style scoped>
/* v3 / WP2：代码块工具条 + 行号 + 折叠 + 下载。
   v-html 生成的节点没有 scoped 属性，必须用 :deep() 才能命中。 */
.markdown-body :deep(.code-block) {
  margin: 10px 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  overflow: hidden;
  /* 代码块沿用全局深色代码底色（--code-surface），文字色与高亮主题才匹配 */
  background: var(--code-surface, #282c34);
}

.markdown-body :deep(.code-toolbar) {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  background: rgba(255, 255, 255, 0.06);
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.markdown-body :deep(.markdown-lang) {
  /* 覆盖全局绝对定位（旧版把语言标签贴到 pre 左上角），改为工具条内联标签 */
  position: static;
  top: auto;
  left: auto;
  font-size: 11px;
  line-height: 18px;
  padding: 0 6px;
  border-radius: 4px;
  color: #9ec1ff;
  background: rgba(47, 107, 255, 0.18);
  font-family: var(--font-mono, Consolas, monospace);
  text-transform: lowercase;
  pointer-events: auto;
}

.markdown-body :deep(.code-spacer) {
  flex: 1 1 auto;
}

.markdown-body :deep(.code-action) {
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.72);
  font-size: 11px;
  line-height: 16px;
  padding: 1px 6px;
  cursor: pointer;
}

.markdown-body :deep(.code-action:hover) {
  border-color: rgba(255, 255, 255, 0.45);
  color: #fff;
}

.markdown-body :deep(.code-action.active) {
  color: #9ec1ff;
  border-color: #6f9bff;
}

.markdown-body :deep(.code-main) {
  display: flex;
  align-items: flex-start;
  max-height: none;
}

.markdown-body :deep(.code-main pre) {
  flex: 1 1 auto;
  margin: 0;
  padding: 10px 12px;
  border: none;
  border-radius: 0;
  overflow-x: auto;
  background: transparent;
}

/* 旧版为绝对定位的语言标签预留了顶部内边距，这里取消，避免代码上方空一行 */
.markdown-body :deep(.code-main pre code) {
  padding: 0;
  font-size: 13px;
  line-height: 1.6;
}

/* 行号槽：默认隐藏，打开「行号」后显示；与代码同字体同字号保证逐行对齐 */
.markdown-body :deep(.code-gutter) {
  display: none;
  flex: 0 0 auto;
  min-width: 32px;
  padding: 10px 6px 10px 10px;
  text-align: right;
  font-family: var(--font-mono, Consolas, monospace);
  font-size: 13px;
  line-height: 1.6;
  color: rgba(255, 255, 255, 0.35);
  background: rgba(255, 255, 255, 0.04);
  border-right: 1px solid rgba(255, 255, 255, 0.1);
  white-space: pre;
  user-select: none;
  overflow: hidden;
}

.markdown-body :deep(.code-block.show-lines .code-gutter) {
  display: block;
}

/* >40 行折叠：只裁剪可视高度，复制/下载仍取完整代码 */
.markdown-body :deep(.code-block.collapsed .code-main) {
  max-height: 320px;
  overflow: hidden;
  position: relative;
}

.markdown-body :deep(.code-block.collapsed .code-main::after) {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 32px;
  background: linear-gradient(transparent, var(--code-surface, #282c34));
  pointer-events: none;
}

.markdown-body :deep(.code-fold) {
  display: block;
  width: 100%;
  border: none;
  border-top: 1px dashed rgba(255, 255, 255, 0.18);
  background: rgba(255, 255, 255, 0.03);
  color: rgba(255, 255, 255, 0.7);
  font-size: 12px;
  padding: 6px 8px;
  cursor: pointer;
}

.markdown-body :deep(.code-fold:hover) {
  color: #fff;
}

@media (max-width: 768px) {
  .markdown-body :deep(.code-toolbar) {
    flex-wrap: wrap;
  }
}
</style>
