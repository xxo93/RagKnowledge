<script setup>
// 原文档预览弹窗：PDF 原生、Markdown 渲染、DOCX/DOC 文本、Excel/CSV 表格。
// 知识库管理页与问答来源卡片共用；数据由后端 /api/docs/{id}/preview 返回，预览只读。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownRenderer from './MarkdownRenderer.vue'
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
  revokePdf()
  try {
    const data = await fetchDocPreview(id)
    if (current !== seq) return
    if (data?.kind === 'pdf') {
      pdfUrl.value = URL.createObjectURL(data.blob)
      preview.value = { ...data, kind: 'pdf' }
    } else {
      preview.value = data
      activeSheet.value = data?.sheets?.[0]?.name || ''
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
    else revokePdf()
  }
)

watch(
  () => props.doc?.doc_id,
  () => {
    if (props.modelValue) load()
  }
)

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
    width="min(920px, 96vw)"
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

      <div v-else-if="preview.kind === 'markdown'" class="markdown-wrap">
        <MarkdownRenderer :content="preview.content || ''" />
      </div>

      <pre v-else-if="preview.kind === 'text'" class="text-wrap">{{ preview.content || '（没有可预览的文本内容）' }}</pre>

      <div v-else-if="preview.kind === 'sheet'" class="sheet-wrap">
        <el-tabs v-if="preview.sheets?.length" v-model="activeSheet">
          <el-tab-pane
            v-for="sheet in preview.sheets"
            :key="sheet.name"
            :label="sheet.name"
            :name="sheet.name"
          >
            <div class="sheet-meta">
              共 {{ sheet.total_rows }} 行，预览前 {{ Math.min(sheet.rows.length, 100) }} 行
              <el-tag v-if="sheet.truncated" size="small" type="warning" effect="plain">已截断</el-tag>
            </div>
            <el-table
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
  .markdown-wrap, .text-wrap { max-height: 66vh; }
  .sheet-wrap { max-height: 64vh; }
  .preview-meta { gap: 4px 10px; }
}
</style>
