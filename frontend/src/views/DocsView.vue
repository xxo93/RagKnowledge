<script setup>
// 知识库管理页：多格式上传队列、分块上传、时间/关键字筛选、文档列表、删除与重解析
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import FileUploader from '../components/FileUploader.vue'
import UploadQueue from '../components/UploadQueue.vue'
import DocTable from '../components/DocTable.vue'
import DocPreviewDialog from '../components/DocPreviewDialog.vue'
import {
  fetchSystemInfo,
  uploadDoc,
  uploadDocChunked,
  listDocs,
  deleteDoc,
  downloadDocFile,
  reparseDoc,
  rebuildIndex
} from '../api/docs'

const STORAGE_CHUNK_MB = 'rag.upload.chunkMb'
const STORAGE_CONCURRENCY = 'rag.upload.concurrency'

function readStoredNumber(key, fallback) {
  try {
    const raw = Number(localStorage.getItem(key))
    return Number.isFinite(raw) && raw > 0 ? raw : fallback
  } catch (_) {
    return fallback
  }
}

/** 本地是否存过该选择；没存过才跟随服务端默认值（存过说明用户自己选过，不能被覆盖）。 */
function hasStored(key) {
  try {
    return localStorage.getItem(key) !== null
  } catch (_) {
    return false
  }
}

function writeStored(key, value) {
  try {
    localStorage.setItem(key, String(value))
  } catch (_) {
    // 隐私模式/存储被禁用时忽略，配置仅在本次会话生效
  }
}

const sysInfo = ref(null)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const size = 10
const tableLoading = ref(false)
const indexBusy = ref(false)
const processingIds = ref([])
const keyword = ref('')
const statusFilter = ref('all')
const dateRange = ref(null)
const appliedStatus = ref('all')
const appliedDateRange = ref(null)
const loadError = ref(false)
const settingsOpen = ref([])
const queue = ref([])
const queueRunning = ref(false)
const previewVisible = ref(false)
const previewDoc = ref(null)
let queueSeq = 0
let keywordTimer = null
let skipKeywordWatch = false
let requestSeq = 0

// 分块大小（MB）与并发数：优先用用户上次的选择（localStorage），
// 没有选择过则采用配置中心下发的默认值（UPLOAD_DEFAULT_CHUNK_MB / UPLOAD_DEFAULT_CONCURRENCY）。
const chunkMb = ref(readStoredNumber(STORAGE_CHUNK_MB, 4))
const concurrency = ref(readStoredNumber(STORAGE_CONCURRENCY, 3))
// 程序化套用服务端默认值时不要写 localStorage，否则"没选过"会被误判成"选过"
const applyingDefaults = ref(false)

const uploadLimits = computed(() => ({
  singleMb: sysInfo.value?.upload_max_mb ?? 10,
  totalMb: sysInfo.value?.upload_total_max_mb ?? 100,
  chunkMaxMb: sysInfo.value?.upload_chunk_max_mb ?? 32
}))

// 分块大小候选：按服务端单片上限过滤；若默认值不在候选表里则补进去
const chunkOptions = computed(() => {
  const cap = uploadLimits.value.chunkMaxMb
  const base = [1, 2, 4, 8, 16, 32].filter((mb) => mb <= cap)
  const current = chunkMb.value
  return base.includes(current) ? base : [...base, current].sort((a, b) => a - b)
})

const uploading = computed(() =>
  queue.value.some((item) => item.status === 'uploading' || item.status === 'processing')
)

const hasFilters = computed(
  () =>
    Boolean(keyword.value.trim()) ||
    Boolean(appliedDateRange.value?.length) ||
    appliedStatus.value !== 'all'
)

const emptyText = computed(() =>
  hasFilters.value ? '没有匹配的文档，试试清空筛选' : '暂无文档，上传后在此展示'
)

function dateKey(range) {
  return range && range[0] && range[1] ? `${range[0]}~${range[1]}` : ''
}

// 日期/状态改了但还没点【查询】：按钮旁给出“待查询”，避免误以为已生效
const pendingFilter = computed(
  () => dateKey(dateRange.value) !== dateKey(appliedDateRange.value) || statusFilter.value !== appliedStatus.value
)

const dateShortcuts = [
  {
    text: '今天',
    value: () => {
      const today = new Date()
      return [today, today]
    }
  },
  {
    text: '近 7 天',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setDate(start.getDate() - 6)
      return [start, end]
    }
  },
  {
    text: '近 30 天',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setDate(start.getDate() - 29)
      return [start, end]
    }
  }
]

watch(chunkMb, (v) => {
  if (!applyingDefaults.value) writeStored(STORAGE_CHUNK_MB, v)
})
watch(concurrency, (v) => {
  if (!applyingDefaults.value) writeStored(STORAGE_CONCURRENCY, v)
})

/** 用服务端默认值初始化"用户还没选过"的项（配置中心改了默认值，刷新页面即生效）。 */
function applyServerDefaults(info) {
  if (!info) return
  const patchChunk =
    !hasStored(STORAGE_CHUNK_MB) && Number.isFinite(Number(info.upload_default_chunk_mb))
  const patchConcurrency =
    !hasStored(STORAGE_CONCURRENCY) && Number.isFinite(Number(info.upload_default_concurrency))
  if (!patchChunk && !patchConcurrency) return
  applyingDefaults.value = true
  if (patchChunk) chunkMb.value = Number(info.upload_default_chunk_mb)
  if (patchConcurrency) concurrency.value = Number(info.upload_default_concurrency)
  nextTick(() => {
    applyingDefaults.value = false
  })
}

// 服务端上限变化时纠正越界的本地配置
watch(uploadLimits, (limits) => {
  const cap = limits.chunkMaxMb
  if (chunkMb.value > cap) chunkMb.value = cap
  if (concurrency.value > 6) concurrency.value = 6
  if (concurrency.value < 1) concurrency.value = 1
})

function currentFilters() {
  return {
    keyword: keyword.value.trim(),
    status: appliedStatus.value,
    start_date: appliedDateRange.value?.[0] || '',
    end_date: appliedDateRange.value?.[1] || ''
  }
}

async function refresh(p = page.value) {
  const seq = ++requestSeq
  tableLoading.value = true
  try {
    const data = await listDocs(p, size, currentFilters())
    if (seq !== requestSeq) return // 旧请求晚到，丢弃
    rows.value = data?.list || []
    total.value = data?.total || 0
    page.value = p
    loadError.value = false
  } catch (e) {
    if (seq !== requestSeq) return
    loadError.value = true
    ElMessage.error(e.message || '文档列表加载失败')
  } finally {
    if (seq === requestSeq) tableLoading.value = false
  }
}

// 关键字自动筛选：停止输入 300ms 再请求，避免每敲一个字打一次后端
watch(keyword, () => {
  if (skipKeywordWatch) {
    skipKeywordWatch = false
    return
  }
  clearTimeout(keywordTimer)
  keywordTimer = setTimeout(() => refresh(1), 300)
})

function onQuery() {
  clearTimeout(keywordTimer)
  appliedStatus.value = statusFilter.value
  appliedDateRange.value = dateRange.value ? [...dateRange.value] : null
  refresh(1)
}

function onReset() {
  clearTimeout(keywordTimer)
  // 只有关键字原本非空时 watch 才会触发；避免空关键字重置后把下一次输入误吞掉
  skipKeywordWatch = Boolean(keyword.value)
  keyword.value = ''
  dateRange.value = null
  statusFilter.value = 'all'
  appliedStatus.value = 'all'
  appliedDateRange.value = null
  refresh(1)
}

function addQueueItem(file, extra = {}) {
  queue.value.push({
    id: ++queueSeq,
    file,
    name: file?.name || extra.name || '未命名文件',
    size: Number(file?.size || extra.size || 0),
    status: 'queued',
    percent: 0,
    loaded: 0,
    total: Number(file?.size || extra.size || 0),
    chunkCount: 0,
    chunkCountResult: 0,
    error: '',
    ...extra
  })
}

function onFiles(files) {
  if (!files?.length) return
  for (const file of files) addQueueItem(file)
  processQueue()
}

function onInvalidFiles(skipped) {
  for (const item of skipped || []) {
    addQueueItem(null, {
      name: item.name,
      size: item.size,
      status: 'skipped',
      percent: 100,
      error: item.reason || '已跳过'
    })
  }
}

async function uploadItem(item) {
  item.status = 'uploading'
  item.error = ''
  item.percent = 0
  item.loaded = 0
  item.total = item.size
  const chunkBytes = chunkMb.value * 1024 * 1024

  try {
    let result
    if (item.size > chunkBytes) {
      result = await uploadDocChunked(item.file, {
        chunkSize: chunkBytes,
        concurrency: concurrency.value,
        onProgress: (p) => {
          item.percent = Math.max(0, Math.min(100, Number(p.percent || 0)))
          item.loaded = p.loaded
          item.total = p.total
          item.chunkCount = p.chunkCount
          if (item.percent >= 100 && item.status === 'uploading') item.status = 'processing'
        }
      })
    } else {
      result = await uploadDoc(item.file, (evt) => {
        const totalBytes = evt.total || item.size
        item.percent = totalBytes ? Math.round((evt.loaded / totalBytes) * 100) : 0
        if (item.percent >= 100 && item.status === 'uploading') item.status = 'processing'
      })
    }
    item.status = 'success'
    item.percent = 100
    item.docId = result?.doc_id
    item.chunkCountResult = Number(result?.chunk_count || 0)
    return { ok: true, chunks: item.chunkCountResult }
  } catch (e) {
    item.status = 'failed'
    item.error = e.message || '上传失败，请查看后端日志'
    return { ok: false, chunks: 0 }
  }
}

async function processQueue() {
  if (queueRunning.value) return
  queueRunning.value = true
  let success = 0
  let failed = 0
  let chunks = 0
  try {
    // 逐文件串行：避免多个文件同时解析/向量化抢 CPU 与 Embedding 配额
    while (true) {
      const item = queue.value.find((entry) => entry.status === 'queued')
      if (!item) break
      const result = await uploadItem(item)
      if (result.ok) {
        success += 1
        chunks += result.chunks
      } else {
        failed += 1
      }
    }
  } finally {
    queueRunning.value = false
  }
  if (success) ElMessage.success(`上传完成：${success} 个文件，共 ${chunks} 个切片`)
  if (failed) ElMessage.warning(`有 ${failed} 个文件上传失败，可在队列中重试`)
  if (success) await refresh(1)
}

function retryItem(id) {
  const item = queue.value.find((entry) => entry.id === id)
  if (!item || item.status !== 'failed') return
  item.status = 'queued'
  item.error = ''
  item.percent = 0
  processQueue()
}

function retryAllFailed() {
  for (const item of queue.value) {
    if (item.status === 'failed') {
      item.status = 'queued'
      item.error = ''
      item.percent = 0
    }
  }
  processQueue()
}

function removeItem(id) {
  const item = queue.value.find((entry) => entry.id === id)
  if (!item || item.status === 'uploading' || item.status === 'processing') return
  queue.value = queue.value.filter((entry) => entry.id !== id)
}

function clearCompleted() {
  queue.value = queue.value.filter((entry) => entry.status !== 'success' && entry.status !== 'skipped')
}

function onPreview(row) {
  if (row.status === 0) {
    ElMessage.warning('文档处理中，暂不可预览')
    return
  }
  previewDoc.value = row
  previewVisible.value = true
}

async function onDownload(row) {
  try {
    const blob = await downloadDocFile(row.doc_id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = row.doc_name || 'document'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error(e.message || '下载失败，请稍后重试')
  }
}

async function onRemove(docId) {
  try {
    await ElMessageBox.confirm('确认删除该文档及其全部切片？该操作不可恢复。', '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'
    })
  } catch (_) {
    return // 用户取消
  }
  try {
    await deleteDoc(docId)
    ElMessage.success('已删除')
    await refresh(page.value)
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

async function onReparse(row) {
  try {
    await ElMessageBox.confirm(
      `确认重解析文档“${row.doc_name}”？将重新切片并替换向量，doc_id 保持不变。`,
      '重解析确认',
      { type: 'warning', confirmButtonText: '重解析', cancelButtonText: '取消' }
    )
  } catch (_) {
    return
  }
  processingIds.value.push(row.doc_id)
  try {
    const result = await reparseDoc(row.doc_id)
    ElMessage.success(`重解析完成：${result.old_chunks} 片 → ${result.new_chunks} 片`)
    await refresh(page.value)
  } catch (e) {
    ElMessage.error(e.message || '重解析失败，请查看后端日志')
  } finally {
    processingIds.value = processingIds.value.filter(id => id !== row.doc_id)
  }
}

async function onRebuildIndex() {
  try {
    await ElMessageBox.confirm(
      '确认重建 HNSW 向量索引？该操作为纯 DDL，不改变文档内容。',
      '重建索引确认',
      { type: 'warning', confirmButtonText: '重建索引', cancelButtonText: '取消' }
    )
  } catch (_) {
    return
  }
  indexBusy.value = true
  try {
    await rebuildIndex()
    ElMessage.success('HNSW 向量索引重建完成')
  } catch (e) {
    ElMessage.error(e.message || '索引重建失败，请查看后端日志')
  } finally {
    indexBusy.value = false
  }
}

function onPageChange(p) {
  refresh(p)
}

onMounted(async () => {
  try {
    sysInfo.value = await fetchSystemInfo()
    applyServerDefaults(sysInfo.value)
  } catch (_) {
    sysInfo.value = null
  }
  await refresh(1)
})

onBeforeUnmount(() => {
  clearTimeout(keywordTimer)
})
</script>

<template>
  <el-card>
    <template #header>
      <div class="page-head">
        <span>知识库管理</span>
        <el-tag v-if="sysInfo" type="info" size="small">
          向量维度 {{ sysInfo.embedding_dim }} · Top-K {{ sysInfo.top_k }} · 索引 HNSW 余弦
        </el-tag>
      </div>
    </template>

    <!-- 上传区：前端先做后缀与大小校验；合法文件进入队列，逐文件串行处理 -->
    <FileUploader
      :uploading="uploading"
      :max-mb="uploadLimits.totalMb"
      @files="onFiles"
      @invalid="onInvalidFiles"
    />

    <UploadQueue
      :items="queue"
      @retry="retryItem"
      @retry-all="retryAllFailed"
      @remove="removeItem"
      @clear="clearCompleted"
    />

    <!-- 分块策略：默认收起；分块大小与并发数可自选，选择记在 localStorage -->
    <el-collapse v-model="settingsOpen" class="upload-settings">
      <el-collapse-item name="settings">
        <template #title>
          <span class="settings-title">上传设置</span>
          <span class="settings-summary">{{ chunkMb }}MB / 片 · {{ concurrency }} 片并发</span>
        </template>
        <div class="upload-options">
          <span class="opt-label">分块大小</span>
          <el-select v-model="chunkMb" size="small" aria-label="分块大小" class="opt-select">
            <el-option v-for="mb in chunkOptions" :key="mb" :label="`${mb} MB`" :value="mb" />
          </el-select>
          <span class="opt-label">并发数</span>
          <el-select v-model="concurrency" size="small" aria-label="分块并发数" class="opt-select">
            <el-option v-for="n in [1, 2, 3, 4, 5, 6]" :key="n" :label="`${n}`" :value="n" />
          </el-select>
          <span class="opt-tip">
            ≤ {{ chunkMb }}MB 走单次请求；更大的文件按 {{ chunkMb }}MB/片、{{ concurrency }} 片并发上传，
            单文件上限 {{ uploadLimits.totalMb }}MB，单片上限 {{ uploadLimits.chunkMaxMb }}MB
          </span>
        </div>
      </el-collapse-item>
    </el-collapse>

    <el-divider />

    <div class="toolbar">
      <el-input
        v-model="keyword"
        size="small"
        clearable
        placeholder="输入文档名称关键字，自动筛选"
        aria-label="按文档名筛选"
        class="filter-input"
        @keyup.enter="onQuery"
      />
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        size="small"
        unlink-panels
        range-separator="至"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        value-format="YYYY-MM-DD"
        :shortcuts="dateShortcuts"
        class="filter-date"
        aria-label="按上传时间范围筛选"
      />
      <el-select v-model="statusFilter" size="small" aria-label="按状态筛选" class="filter-select">
        <el-option label="全部状态" value="all" />
        <el-option label="处理中" value="0" />
        <el-option label="成功" value="1" />
        <el-option label="失败" value="2" />
      </el-select>
      <el-button type="primary" size="small" @click="onQuery">查询</el-button>
      <el-button size="small" @click="onReset">重置</el-button>
      <span v-if="pendingFilter" class="pending-tip">筛选已修改，点击查询生效</span>
      <span class="toolbar-spacer" />
      <el-button type="primary" plain size="small" :loading="indexBusy" :disabled="uploading" @click="onRebuildIndex">
        重建 HNSW 索引
      </el-button>
    </div>

    <div class="result-summary">
      <span>共 {{ total }} 篇</span>
      <button v-if="hasFilters" type="button" class="clear-filter" @click="onReset">清空筛选</button>
    </div>

    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      show-icon
      title="文档列表加载失败，可点击重试"
      class="load-error"
    >
      <el-button size="small" @click="refresh(1)">重新加载</el-button>
    </el-alert>

    <!-- 文档列表：名称、状态、切片数、大小、时间、重解析与删除操作 -->
    <DocTable
      :rows="rows"
      :loading="tableLoading"
      :empty-text="emptyText"
      :processing-ids="processingIds"
      @remove="onRemove"
      @reparse="onReparse"
      @preview="onPreview"
      @download="onDownload"
    />

    <DocPreviewDialog
      v-model="previewVisible"
      :doc="previewDoc || {}"
      allow-download
    />

    <div class="pager" v-if="total > 0">
      <el-pagination
        size="small"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="size"
        :current-page="page"
        @current-change="onPageChange"
      />
    </div>
  </el-card>
</template>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.filter-input { width: 240px; }
.filter-date { width: 250px; }
.filter-select { width: 120px; }
.toolbar-spacer { flex: 1; min-width: 8px; }
.pending-tip { font-size: 12px; color: var(--warning, #e6a23c); }
.result-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--text-muted);
}
.clear-filter { border: none; background: transparent; color: var(--primary); cursor: pointer; font-size: 12px; padding: 0; }
.clear-filter:hover { text-decoration: underline; }
.upload-settings { margin-top: 10px; }
.settings-title { font-size: 13px; color: var(--text-secondary); }
.settings-summary { margin-left: 10px; font-size: 12px; color: var(--text-muted); }
.upload-options { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.opt-label { font-size: 12px; color: var(--text-secondary); }
.opt-select { width: 96px; }
.opt-tip { font-size: 12px; color: var(--text-muted); }
.load-error { margin-bottom: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
@media (max-width: 640px) {
  .page-head { flex-direction: column; align-items: flex-start; gap: 6px; }
  .toolbar { align-items: stretch; }
  .filter-input, .filter-date, .filter-select { width: 100%; }
  .toolbar-spacer { display: none; }
  .pager { justify-content: center; }
}
</style>
