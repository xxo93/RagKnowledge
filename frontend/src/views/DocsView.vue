<script setup>
// 知识库管理页：批量上传（大文件自动分块）、文档列表、删除、重解析与索引重建
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import FileUploader from '../components/FileUploader.vue'
import DocTable from '../components/DocTable.vue'
import {
  fetchSystemInfo,
  uploadDoc,
  uploadDocChunked,
  listDocs,
  deleteDoc,
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
const uploading = ref(false)
const indexBusy = ref(false)
const processingIds = ref([])
const keyword = ref('')
const statusFilter = ref('all')
const loadError = ref(false)
const uploadReport = ref(null)
const uploadProgress = ref('')

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
    status: statusFilter.value
  }
}

async function refresh(p = page.value) {
  tableLoading.value = true
  try {
    const data = await listDocs(p, size, currentFilters())
    rows.value = data?.list || []
    total.value = data?.total || 0
    page.value = p
    loadError.value = false
  } catch (e) {
    loadError.value = true
    ElMessage.error(e.message || '文档列表加载失败')
  } finally {
    tableLoading.value = false
  }
}

watch([keyword, statusFilter], () => refresh(1))

function onInvalidFiles(skipped) {
  uploadReport.value = { success: [], failed: [], skipped: skipped || [], totalChunks: 0 }
}

function formatMb(bytes) {
  return `${(Number(bytes || 0) / (1024 * 1024)).toFixed(1)}MB`
}

async function onUpload(payload) {
  const files = (Array.isArray(payload) ? payload : [payload]).filter(Boolean)
  if (!files.length) return
  uploading.value = true
  const success = []
  const failed = []
  const chunkBytes = chunkMb.value * 1024 * 1024
  let totalChunks = 0
  try {
    // 逐文件串行：避免多个文件的解析/向量化同时抢占 CPU 与 Embedding 配额；
    // 单个文件内部按「分块大小 + 并发数」并行上传分片
    for (const file of files) {
      try {
        let result
        if (file.size > chunkBytes) {
          uploadProgress.value = `${file.name}：准备分块上传…`
          result = await uploadDocChunked(file, {
            chunkSize: chunkBytes,
            concurrency: concurrency.value,
            onProgress: (p) => {
              uploadProgress.value = `${file.name}：分块上传 ${p.percent}%（${p.chunkCount} 片，${formatMb(p.loaded)}/${formatMb(p.total)}）`
            }
          })
        } else {
          uploadProgress.value = `${file.name}：上传中…`
          result = await uploadDoc(file)
        }
        success.push({
          file_name: file.name,
          doc_id: result?.doc_id,
          chunk_count: result?.chunk_count,
          status: result?.status,
          mode: file.size > chunkBytes ? 'chunked' : 'single'
        })
        totalChunks += Number(result?.chunk_count || 0)
      } catch (e) {
        failed.push({ file_name: file.name, error: e.message || '上传失败，请查看后端日志' })
      } finally {
        uploadProgress.value = ''
      }
    }
    uploadReport.value = { success, failed, skipped: [], totalChunks }
    if (!failed.length) {
      ElMessage.success(`上传完成：${success.length} 个文件，共 ${totalChunks} 个切片`)
    } else if (success.length > 0) {
      ElMessage.warning(`部分成功：成功 ${success.length} 个、失败 ${failed.length} 个`)
    } else {
      ElMessage.error('上传失败，请查看下方失败原因')
    }
    await refresh(1)
  } finally {
    uploading.value = false
    uploadProgress.value = ''
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

    <!-- 上传区：前端先做后缀与大小校验；超过分块阈值的文件自动分块并发上传 -->
    <FileUploader
      :uploading="uploading"
      :max-mb="uploadLimits.totalMb"
      :progress-text="uploadProgress"
      @upload="onUpload"
      @invalid="onInvalidFiles"
    />

    <!-- 分块策略：分块大小与并发数可自选，选择记在 localStorage，范围受服务端上限约束 -->
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

    <el-alert
      v-if="uploadReport"
      :type="(uploadReport.failed.length || uploadReport.skipped?.length) ? 'warning' : 'success'"
      :closable="false"
      show-icon
      class="upload-report"
    >
      <template #title>
        本轮上传：成功 {{ uploadReport.success.length }} 个、失败 {{ uploadReport.failed.length }} 个
        <span v-if="uploadReport.skipped?.length">、跳过 {{ uploadReport.skipped.length }} 个</span>
        <span v-if="uploadReport.totalChunks">，共 {{ uploadReport.totalChunks }} 个切片</span>
      </template>
      <div v-if="uploadReport.failed.length" class="report-failed">
        <div v-for="(item, idx) in uploadReport.failed" :key="`fail-${idx}`">
          · {{ item.file_name }}：{{ item.error || item.error_msg || '解析或向量化失败' }}
        </div>
      </div>
      <div v-if="uploadReport.skipped?.length" class="report-failed">
        <div v-for="(item, idx) in uploadReport.skipped" :key="`skip-${idx}`">
          · {{ item }}
        </div>
      </div>
    </el-alert>

    <el-divider />

    <div class="toolbar">
      <el-input
        v-model="keyword"
        size="small"
        clearable
        placeholder="按文档名筛选"
        aria-label="按文档名筛选"
        class="filter-input"
      />
      <el-select v-model="statusFilter" size="small" aria-label="按状态筛选" class="filter-select">
        <el-option label="全部状态" value="all" />
        <el-option label="处理中" value="0" />
        <el-option label="成功" value="1" />
        <el-option label="失败" value="2" />
      </el-select>
      <el-button type="primary" plain size="small" :loading="indexBusy" @click="onRebuildIndex">
        重建 HNSW 索引
      </el-button>
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
      :processing-ids="processingIds"
      @remove="onRemove"
      @reparse="onReparse"
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
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.filter-input { width: 220px; }
.filter-select { width: 130px; }
.upload-report { margin: 12px 0 0; }
.upload-options { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.opt-label { font-size: 12px; color: var(--text-secondary); }
.opt-select { width: 96px; }
.opt-tip { font-size: 12px; color: var(--text-muted); }
.report-failed { margin-top: 6px; font-size: 12px; color: var(--text-muted); }
.load-error { margin-bottom: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
