<script setup>
// 原文档预览弹窗：PDF 原生、Markdown 渲染、DOCX/DOC 浏览器按页渲染、Excel/CSV 表格视图。
// 知识库管理页与问答来源卡片共用；数据由后端 /api/docs/{id}/preview 返回，预览只读。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownRenderer from './MarkdownRenderer.vue'
import SafeHtml from './SafeHtml.vue'
import { downloadDocFile, fetchDocPreview } from '../api/docs'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  doc: { type: Object, default: () => ({}) },
  // 知识库管理页管理员可下载；问答来源卡片不显示下载
  allowDownload: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const loading = ref(false)
const error = ref('')
const preview = ref(null)
const pdfUrl = ref('')
const downloading = ref(false)
const activeSheet = ref('')
const docxBody = ref(null)
const docxStyle = ref(null)
const docxViewport = ref(null)
const docxPages = ref(0)
const docxZoom = ref(100)
const docxRendering = ref(false)
const sheetZoom = ref(100)
const sheetViewport = ref(null)
let seq = 0

const docTitle = computed(() => props.doc?.doc_name || props.doc?.doc_title || '文档预览')
// 表格行已有 file_size/create_time/chunk_count；PDF 只有 blob，靠 props.doc 兜底元信息
const docInfo = computed(() => ({ ...(props.doc || {}), ...(preview.value || {}) }))
const extLabel = computed(() => {
  const name = props.doc?.doc_name || preview.value?.doc_name || ''
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot + 1).toUpperCase() : '文件'
})

function formatSize(bytes) {
  const n = Number(bytes || 0)
  if (!n) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function formatTime(value) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value).replace('T', ' ').slice(0, 16)
  const pad = (x) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function revokePdf() {
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
  pdfUrl.value = ''
}

function clampZoom(value) {
  return Math.max(30, Math.min(150, Math.round(value)))
}

function zoomDoc(delta) {
  docxZoom.value = clampZoom(docxZoom.value + delta)
}

function zoomSheet(delta) {
  sheetZoom.value = clampZoom(sheetZoom.value + delta)
}

function base64ToBytes(value) {
  const raw = atob(value || '')
  const bytes = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i)
  return bytes
}

// docx-preview 直接生成 DOM；这里只做必要的 URL/事件属性清理，保留页面与表格结构。
function sanitizeDocx(root) {
  if (!root) return
  root.querySelectorAll('script, iframe, object, embed, link, meta, base').forEach((el) => el.remove())
  root.querySelectorAll('*').forEach((el) => {
    for (const attr of Array.from(el.attributes)) {
      if (/^on/i.test(attr.name)) el.removeAttribute(attr.name)
    }
  })
  root.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || ''
    if (!/^(https?:|mailto:|#)/i.test(href)) {
      a.removeAttribute('href')
      return
    }
    a.setAttribute('target', '_blank')
    a.setAttribute('rel', 'noopener noreferrer')
  })
  root.querySelectorAll('img[src]').forEach((img) => {
    const src = img.getAttribute('src') || ''
    if (!/^(data:image\/|blob:)/i.test(src)) img.removeAttribute('src')
  })
}

async function fitDocWidth() {
  if (!docxViewport.value || !docxBody.value) return
  docxZoom.value = 100
  await nextTick()
  const page = docxBody.value.querySelector('section.rag-docx')
  const natural = page?.offsetWidth || 794
  const available = Math.max(180, docxViewport.value.clientWidth - 32)
  docxZoom.value = clampZoom((available / natural) * 100)
}

async function loadHtmlFallback(current) {
  const data = await fetchDocPreview(props.doc?.doc_id, { forceHtml: true })
  if (current !== seq) return
  preview.value = data
  activeSheet.value = ''
}

async function renderDocx(data, current) {
  if (!docxBody.value) {
    await loadHtmlFallback(current)
    return
  }
  docxRendering.value = true
  docxPages.value = 0
  try {
    const { renderAsync } = await import('docx-preview')
    if (current !== seq || !docxBody.value) return
    const blob = new Blob([base64ToBytes(data.docx_base64)], {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    })
    await renderAsync(blob, docxBody.value, docxStyle.value, {
      className: 'rag-docx',
      inWrapper: true,
      breakPages: true,
      ignoreWidth: false,
      ignoreHeight: false,
      ignoreFonts: false,
      ignoreLastRenderedPageBreak: false,
      renderHeaders: true,
      renderFooters: true,
      renderFootnotes: true,
      renderEndnotes: true,
      renderComments: false,
      renderChanges: false,
      renderAltChunks: false,
      useBase64URL: true
    })
    if (current !== seq) return
    sanitizeDocx(docxBody.value)
    docxPages.value = docxBody.value.querySelectorAll('section.rag-docx').length
    await nextTick()
    if (window.innerWidth <= 600) await fitDocWidth()
  } catch (e) {
    console.warn('DOCX 按页渲染失败，切换为语义 HTML 预览', e)
    if (typeof window !== 'undefined') window.__docxPreviewError = String(e?.stack || e)
    await loadHtmlFallback(current)
  } finally {
    docxRendering.value = false
  }
}

async function fitSheetWidth() {
  const index = Math.max(0, preview.value?.sheets?.findIndex((s) => s.name === activeSheet.value) ?? 0)
  const viewport = Array.isArray(sheetViewport.value)
    ? sheetViewport.value[index]
    : sheetViewport.value
  if (!viewport) return
  sheetZoom.value = 100
  await nextTick()
  const table = viewport.querySelector('.sheet-html table')
  const natural = table?.offsetWidth || viewport.scrollWidth || 1
  const available = Math.max(200, viewport.clientWidth - 12)
  sheetZoom.value = clampZoom((available / natural) * 100)
}

async function load() {
  const id = props.doc?.doc_id
  if (!id) {
    error.value = '缺少文档标识，无法预览'
    return
  }
  seq += 1
  const current = seq
  loading.value = true
  error.value = ''
  preview.value = null
  docxPages.value = 0
  docxZoom.value = 100
  sheetZoom.value = 100
  revokePdf()
  try {
    const data = await fetchDocPreview(id)
    if (current !== seq) return
    if (data?.kind === 'pdf') {
      pdfUrl.value = URL.createObjectURL(data.blob)
      preview.value = { ...data, kind: 'pdf' }
      loading.value = false
    } else if (data?.kind === 'docx') {
      preview.value = data
      loading.value = false
      await nextTick()
      await renderDocx(data, current)
    } else {
      preview.value = data
      activeSheet.value = data?.sheets?.[0]?.name || ''
      loading.value = false
      await nextTick()
      if (window.innerWidth <= 600 && data?.kind === 'sheet') await fitSheetWidth()
    }
  } catch (e) {
    if (current !== seq) return
    error.value = e.message || '预览失败，请下载原件'
  } finally {
    if (current === seq) loading.value = false
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) load()
    else {
      revokePdf()
      seq += 1
    }
  }
)

watch(
  () => props.doc?.doc_id,
  () => {
    if (props.modelValue) load()
  }
)

watch(activeSheet, async () => {
  sheetZoom.value = 100
  await nextTick()
  if (window.innerWidth <= 600) await fitSheetWidth()
})

onBeforeUnmount(() => {
  seq += 1
  revokePdf()
})

// Excel/CSV 行数组转 el-table 需要的对象结构；列名只用于展示，不依赖字段名
function tableRows(sheet) {
  return (sheet?.rows || []).map((row) => {
    const item = {}
    row.forEach((value, index) => {
      item[`c${index}`] = value
    })
    return item
  })
}

function colProp(index) {
  return `c${index}`
}

async function download() {
  const id = props.doc?.doc_id
  if (!id) return
  downloading.value = true
  try {
    const blob = await downloadDocFile(id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = props.doc?.doc_name || 'document'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error(e.message || '下载失败，请稍后重试')
  } finally {
    downloading.value = false
  }
}

function close() {
  visible.value = false
}
</script>

<template>
  <el-dialog
    v-model="visible"
    class="doc-preview-dialog"
    :title="docTitle"
    width="min(1080px, 96vw)"
    append-to-body
    destroy-on-close
    :close-on-click-modal="false"
  >
    <div v-if="loading" class="preview-loading">
      <el-skeleton :rows="6" animated />
    </div>

    <el-alert v-else-if="error" type="warning" :closable="false" show-icon :title="error" />

    <template v-else-if="preview">
      <div class="preview-meta">
        <el-tag size="small" effect="plain">{{ extLabel }}</el-tag>
        <span>大小 {{ formatSize(docInfo.file_size) }}</span>
        <span>上传 {{ formatTime(docInfo.create_time) }}</span>
        <span>切片 {{ docInfo.chunk_count ?? '-' }}</span>
        <el-tag v-if="preview.truncated" size="small" type="warning" effect="plain">内容有截断</el-tag>
      </div>

      <div v-if="preview.kind === 'pdf'" class="pdf-wrap">
        <iframe v-if="pdfUrl" :src="pdfUrl" title="PDF 原文档预览" />
        <el-empty v-else description="PDF 加载失败，请下载原件" />
      </div>

      <div v-else-if="preview.kind === 'docx'" class="docx-shell">
        <div class="preview-toolbar">
          <span class="tool-label">页面</span>
          <div class="zoom-control">
            <el-button size="small" text :disabled="docxZoom <= 30" @click="zoomDoc(-10)">−</el-button>
            <span class="zoom-value">{{ docxZoom }}%</span>
            <el-button size="small" text :disabled="docxZoom >= 150" @click="zoomDoc(10)">＋</el-button>
          </div>
          <el-button size="small" @click="fitDocWidth">适合宽度</el-button>
          <span v-if="docxPages" class="tool-hint">共 {{ docxPages }} 页</span>
        </div>
        <div ref="docxViewport" class="docx-viewport">
          <el-skeleton v-if="docxRendering" class="docx-loading" :rows="8" animated />
          <div
            ref="docxBody"
            class="docx-body"
            :style="{ zoom: docxZoom / 100 }"
            v-show="!docxRendering"
          ></div>
          <div ref="docxStyle" class="docx-style"></div>
        </div>
      </div>

      <div v-else-if="preview.kind === 'html'" class="doc-html-wrap">
        <SafeHtml class="doc-html" :html="preview.content || ''" />
      </div>

      <div v-else-if="preview.kind === 'markdown'" class="markdown-wrap">
        <MarkdownRenderer :content="preview.content || ''" />
      </div>

      <pre v-else-if="preview.kind === 'text'" class="text-wrap">{{ preview.content || '（没有可预览的文本内容）' }}</pre>

      <div v-else-if="preview.kind === 'sheet'" class="sheet-wrap">
        <div class="preview-toolbar">
          <span class="tool-label">表格</span>
          <div class="zoom-control">
            <el-button size="small" text :disabled="sheetZoom <= 30" @click="zoomSheet(-10)">−</el-button>
            <span class="zoom-value">{{ sheetZoom }}%</span>
            <el-button size="small" text :disabled="sheetZoom >= 150" @click="zoomSheet(10)">＋</el-button>
          </div>
          <el-button size="small" @click="fitSheetWidth">适合宽度</el-button>
          <span class="tool-hint">列标/行号固定，可横向滚动</span>
        </div>
        <el-tabs v-if="preview.sheets?.length" v-model="activeSheet" tab-position="bottom">
          <el-tab-pane
            v-for="sheet in preview.sheets"
            :key="sheet.name"
            :label="sheet.name"
            :name="sheet.name"
          >
            <div class="sheet-meta">
              共 {{ sheet.total_rows ?? sheet.rows.length }} 行数据，展示前 {{ sheet.rows.length }} 行
              <el-tag v-if="sheet.truncated" size="small" type="warning" effect="plain">已截断</el-tag>
            </div>
            <div v-if="sheet.html" ref="sheetViewport" class="sheet-html-wrap">
              <SafeHtml class="sheet-html" :html="sheet.html" :style="{ zoom: sheetZoom / 100 }" />
            </div>
            <el-table
              v-else
              :data="tableRows(sheet)"
              border
              size="small"
              max-height="420"
              :empty-text="'没有可预览的行'"
            >
              <el-table-column
                v-for="(header, index) in sheet.headers"
                :key="index"
                :prop="colProp(index)"
                :label="header || `列${index + 1}`"
                min-width="120"
                show-overflow-tooltip
              />
            </el-table>
          </el-tab-pane>
        </el-tabs>
        <el-empty v-else description="没有可预览的工作表" />
      </div>

      <el-alert
        v-if="preview.truncated"
        class="truncated-tip"
        type="info"
        :closable="false"
        show-icon
        title="内容超过预览上限，仅展示部分内容；完整内容请下载原件查看。"
      />
    </template>

    <template #footer>
      <el-button @click="close">关闭</el-button>
      <el-button v-if="allowDownload" type="primary" :loading="downloading" @click="download">
        下载原件
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.preview-loading { padding: 8px 0; }
.preview-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--text-secondary);
}
.pdf-wrap {
  height: 64vh;
  min-height: 320px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-muted);
}
.pdf-wrap iframe {
  width: 100%;
  height: 100%;
  border: 0;
}
.markdown-wrap {
  max-height: 64vh;
  overflow-y: auto;
  padding: 2px 4px;
}
.preview-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 8px;
  margin-bottom: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: #f8fafc;
  font-size: 12px;
  color: var(--text-secondary);
}
.tool-label { font-weight: 600; color: var(--text-primary); }
.tool-hint { color: var(--text-secondary); }
.zoom-control {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: #fff;
}
.zoom-control .el-button { padding: 2px 6px; min-width: 24px; }
.zoom-value { min-width: 42px; text-align: center; font-variant-numeric: tabular-nums; }
.docx-shell {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: #fff;
}
.docx-viewport {
  height: 62vh;
  min-height: 320px;
  overflow: auto;
  padding: 16px 8px;
  background: #eef1f4;
}
.docx-loading { padding: 16px; background: #fff; }
.docx-body {
  width: max-content;
  min-width: 100%;
  transform-origin: top left;
}
.docx-body :deep(.rag-docx-wrapper) {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 0;
  background: transparent;
}
.docx-body :deep(.rag-docx) {
  margin: 0 auto;
  background: #fff;
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.18), 0 10px 24px rgba(15, 23, 42, 0.08);
}
.docx-style { display: none; }
.doc-html-wrap {
  max-height: 64vh;
  overflow-y: auto;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 20px 24px;
}
.doc-html {
  color: #1f2937;
  font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif;
  font-size: 14px;
  line-height: 1.75;
}
.doc-html :deep(h1),
.doc-html :deep(h2),
.doc-html :deep(h3),
.doc-html :deep(h4),
.doc-html :deep(h5),
.doc-html :deep(h6) {
  margin: 0.8em 0 0.45em;
  color: #111827;
  line-height: 1.35;
}
.doc-html :deep(h1) { font-size: 1.7em; }
.doc-html :deep(h2) { font-size: 1.4em; }
.doc-html :deep(h3) { font-size: 1.2em; }
.doc-html :deep(p) { margin: 0.45em 0; }
.doc-html :deep(ul),
.doc-html :deep(ol) { padding-left: 1.6em; margin: 0.5em 0; }
.doc-html :deep(blockquote) {
  margin: 0.7em 0;
  padding: 2px 12px;
  border-left: 3px solid var(--primary);
  color: #4b5563;
  background: #f8fafc;
}
.doc-html :deep(table) {
  border-collapse: collapse;
  width: max-content;
  min-width: 40%;
  max-width: 100%;
  margin: 0.7em 0;
}
.doc-html :deep(td),
.doc-html :deep(th) {
  border: 1px solid #dbe1e8;
  padding: 5px 9px;
  vertical-align: middle;
}
.doc-html :deep(img) { max-width: 100%; height: auto; }
.text-wrap {
  max-height: 64vh;
  overflow: auto;
  margin: 0;
  padding: 14px 16px;
  background: var(--surface-muted);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-family: var(--font-mono, 'SFMono-Regular', Consolas, 'Liberation Mono', monospace);
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}
.sheet-wrap { max-height: 66vh; overflow: hidden; }
.sheet-html-wrap {
  max-height: 54vh;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: #fff;
}
.sheet-html { min-width: max-content; padding: 0; transform-origin: top left; }
.sheet-html :deep(table) {
  border-collapse: collapse;
  table-layout: fixed;
  width: max-content;
  min-width: 100%;
  font-size: 13px;
  color: #1f2937;
  background: #fff;
}
.sheet-html :deep(td),
.sheet-html :deep(th) {
  border: 1px solid #dfe5ec;
  padding: 5px 7px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  vertical-align: middle;
}
.sheet-html :deep(thead th),
.sheet-html :deep(.sheet-rowhead),
.sheet-html :deep(.sheet-corner) {
  background: #f3f4f6;
  color: #6b7280;
  font-weight: 500;
  text-align: center;
  padding: 3px 6px;
  font-variant-numeric: tabular-nums;
}
.sheet-html :deep(thead th) {
  position: sticky;
  top: 0;
  z-index: 3;
}
.sheet-html :deep(.sheet-rowhead) {
  position: sticky;
  left: 0;
  z-index: 2;
  min-width: 44px;
}
.sheet-html :deep(.sheet-corner) {
  position: sticky;
  top: 0;
  left: 0;
  z-index: 4;
}
.sheet-html :deep(tr:first-child td) { font-weight: 600; }
.sheet-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.truncated-tip { margin-top: 10px; }

@media (max-width: 600px) {
  :global(.doc-preview-dialog) {
    width: 100% !important;
    height: 100vh;
    margin: 0;
    border-radius: 0;
    display: flex;
    flex-direction: column;
  }
  :global(.doc-preview-dialog .el-dialog__body) {
    flex: 1;
    overflow-y: auto;
    padding-top: 8px;
  }
  .pdf-wrap { height: 70vh; min-height: 260px; }
  .markdown-wrap, .text-wrap, .doc-html-wrap { max-height: 66vh; }
  .doc-html-wrap { padding: 14px 16px; }
  .docx-viewport { height: 60vh; min-height: 260px; padding: 10px 4px; }
  .sheet-wrap { max-height: 64vh; }
  .sheet-html-wrap { max-height: 48vh; }
  .preview-toolbar { gap: 4px 6px; padding: 4px 6px; }
  .tool-hint { display: none; }
  .preview-meta { gap: 4px 10px; }
}
</style>
