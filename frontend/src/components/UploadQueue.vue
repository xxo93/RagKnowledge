<script setup>
// 知识库上传队列：逐文件展示排队 / 上传 / 处理 / 成功 / 失败 / 跳过状态
// 只负责展示与操作事件，上传流程由 DocsView 串行调度，避免多个文件同时解析抢资源
import { computed } from 'vue'

const props = defineProps({
  items: { type: Array, default: () => [] }
})
const emit = defineEmits(['retry', 'remove', 'clear', 'retry-all'])

const STATUS_LABEL = {
  queued: '排队中',
  uploading: '上传中',
  processing: '处理中',
  success: '成功',
  failed: '失败',
  skipped: '已跳过'
}
const STATUS_TAG = {
  queued: 'info',
  uploading: 'warning',
  processing: 'warning',
  success: 'success',
  failed: 'danger',
  skipped: 'info'
}

const counts = computed(() => {
  const result = { success: 0, failed: 0, skipped: 0, queued: 0, active: 0 }
  for (const item of props.items) {
    if (item.status === 'success') result.success += 1
    else if (item.status === 'failed') result.failed += 1
    else if (item.status === 'skipped') result.skipped += 1
    else if (item.status === 'queued') result.queued += 1
    else result.active += 1
  }
  return result
})

const hasFailed = computed(() => counts.value.failed > 0)
const hasDone = computed(() => counts.value.success + counts.value.skipped > 0)

function extOf(name) {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot + 1).toUpperCase() : 'FILE'
}

function isBusy(item) {
  return item.status === 'uploading' || item.status === 'processing'
}

function percentOf(item) {
  if (item.status === 'success' || item.status === 'skipped') return 100
  if (item.status === 'processing') return 100
  return Math.max(0, Math.min(100, Number(item.percent || 0)))
}

function progressStatus(item) {
  if (item.status === 'success') return 'success'
  if (item.status === 'failed') return 'exception'
  return undefined
}

function formatSize(bytes) {
  const n = Number(bytes || 0)
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`
  return `${(n / (1024 * 1024)).toFixed(1)}MB`
}
</script>

<template>
  <div class="upload-queue">
    <div class="queue-head">
      <div class="queue-title">
        上传队列
        <span class="queue-count">{{ items.length }} 个文件</span>
      </div>
      <div class="queue-summary" aria-live="polite">
        <span class="sum success">成功 {{ counts.success }}</span>
        <span class="sum failed">失败 {{ counts.failed }}</span>
        <span class="sum skipped">跳过 {{ counts.skipped }}</span>
        <span v-if="counts.queued" class="sum queued">排队 {{ counts.queued }}</span>
        <el-button v-if="hasDone" link size="small" @click="emit('clear')">清除已完成</el-button>
        <el-button v-if="hasFailed" link size="small" type="primary" @click="emit('retry-all')">
          全部重试失败项
        </el-button>
      </div>
    </div>

    <div v-if="!items.length" class="queue-empty">还没有待上传文件</div>

    <div v-else class="queue-list">
      <div v-for="item in items" :key="item.id" class="queue-row" :class="item.status">
        <div class="row-main">
          <span class="ext-tag">{{ extOf(item.name) }}</span>
          <div class="file-text">
            <div class="file-name" :title="item.name">{{ item.name }}</div>
            <div class="file-meta">
              {{ formatSize(item.size) }}
              <span v-if="item.status === 'uploading' && item.chunkCount">
                · {{ item.chunkCount }} 片 · {{ formatSize(item.loaded) }}/{{ formatSize(item.total) }}
              </span>
              <span v-if="item.chunkCountResult">· {{ item.chunkCountResult }} 个切片</span>
            </div>
          </div>
          <el-tag :type="STATUS_TAG[item.status] || 'info'" size="small" effect="plain">
            {{ STATUS_LABEL[item.status] || item.status }}
          </el-tag>
        </div>

        <div class="row-progress">
          <el-progress
            :percentage="percentOf(item)"
            :status="progressStatus(item)"
            :stroke-width="6"
            :show-text="false"
          />
          <div class="progress-line">
            <span v-if="item.status === 'uploading'">{{ percentOf(item) }}%</span>
            <span v-else-if="item.status === 'processing'">上传完成，正在解析、切片并向量化…</span>
            <span v-else-if="item.status === 'success'">已入库</span>
            <span v-else-if="item.status === 'skipped'">{{ item.error || '已跳过' }}</span>
            <span v-else-if="item.status === 'failed'" class="error-text">{{ item.error }}</span>
            <span v-else>等待上传</span>
          </div>
        </div>

        <div class="row-actions">
          <el-button v-if="item.status === 'failed'" link size="small" type="primary" @click="emit('retry', item.id)">
            重试
          </el-button>
          <el-button v-if="!isBusy(item)" link size="small" @click="emit('remove', item.id)">移除</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.upload-queue {
  margin-top: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--panel);
  overflow: hidden;
}
.queue-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.queue-title { font-size: 13px; font-weight: 600; }
.queue-count { margin-left: 6px; font-size: 12px; color: var(--text-muted); font-weight: 400; }
.queue-summary { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12px; }
.sum { color: var(--text-muted); }
.sum.success { color: var(--success, #67c23a); }
.sum.failed { color: var(--danger, #f56c6c); }
.queue-empty { padding: 18px 12px; text-align: center; color: var(--text-muted); font-size: 12px; }
.queue-list { max-height: 330px; overflow-y: auto; }
.queue-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-areas: "main actions" "progress progress";
  gap: 4px 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}
.queue-row:last-child { border-bottom: none; }
.row-main { grid-area: main; display: flex; align-items: center; gap: 8px; min-width: 0; }
.ext-tag {
  flex: none;
  min-width: 42px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: 11px;
  text-align: center;
}
.file-text { min-width: 0; flex: 1; }
.file-name { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-meta { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
.row-actions { grid-area: actions; display: flex; align-items: center; gap: 2px; }
.row-progress { grid-area: progress; display: flex; align-items: center; gap: 8px; }
.row-progress :deep(.el-progress) { flex: 1; min-width: 80px; }
.progress-line { flex: none; max-width: 55%; font-size: 12px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.error-text { color: var(--danger, #f56c6c); }
@media (max-width: 640px) {
  .queue-head { align-items: flex-start; }
  .queue-summary { gap: 6px; }
  .queue-row { grid-template-columns: 1fr; grid-template-areas: "main" "progress" "actions"; }
  .row-actions { justify-content: flex-end; }
  .progress-line { max-width: 60%; }
}
</style>
