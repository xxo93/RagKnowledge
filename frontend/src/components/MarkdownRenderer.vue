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
      if (lang && pre && !pre.querySelector('.markdown-lang')) {
        const label = document.createElement('span')
        label.className = 'markdown-lang'
        label.textContent = lang
        pre.prepend(label)
      }
    })
  } catch {
    // 高亮库加载失败时保留纯文本代码块，不影响正文阅读
  }
  root.querySelectorAll('pre').forEach(addCopyButton)
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
