<script setup>
// 文档表格：展示状态、切片数、大小、上传时间，并触发重解析与删除操作
defineProps({
  rows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  processingIds: { type: Array, default: () => [] },
  emptyText: { type: String, default: '暂无文档，上传后在此展示' }
})
const emit = defineEmits(['remove', 'reparse', 'preview', 'download'])

// 状态：0 处理中(warning) / 1 成功(success) / 2 失败(danger)
const statusMap = { 0: '处理中', 1: '成功', 2: '失败' }
const tagType = (row) => (row.status === 1 ? 'success' : row.status === 2 ? 'danger' : 'warning')

// 文件大小按量级显示，避免小文件出现“0.0MB”
function formatSize(bytes) {
  const n = Number(bytes || 0)
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

// 后端返回 ISO 时间；表格只展示到分钟，鼠标悬停看完整值
function formatTime(value) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value).replace('T', ' ').slice(0, 16)
  const pad = (x) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
</script>

<template>
  <div class="table-scroll">
  <el-table :data="rows" v-loading="loading" :empty-text="emptyText" border>
    <el-table-column prop="doc_name" label="文档名称" min-width="220" />
    <el-table-column label="状态" width="110">
      <template #default="{ row }">
        <el-tooltip v-if="row.status === 2 && row.error_msg" :content="row.error_msg" placement="top">
          <el-tag :type="tagType(row)" size="small">{{ statusMap[row.status] || '未知' }}</el-tag>
        </el-tooltip>
        <el-tag v-else :type="tagType(row)" size="small">{{ statusMap[row.status] || '未知' }}</el-tag>
      </template>
    </el-table-column>
    <el-table-column prop="chunk_count" label="切片数" width="90" />
    <el-table-column label="大小" width="110">
      <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
    </el-table-column>
    <el-table-column label="上传时间" width="170">
      <template #default="{ row }">
        <span :title="row.create_time">{{ formatTime(row.create_time) }}</span>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="220">
      <template #default="{ row }">
        <el-button type="primary" link size="small" @click="emit('preview', row)">预览</el-button>
        <el-button type="primary" link size="small" @click="emit('download', row)">下载</el-button>
        <el-button
          type="primary"
          link
          size="small"
          :disabled="processingIds.includes(row.doc_id)"
          @click="emit('reparse', row)"
        >重解析</el-button>
        <el-button type="danger" link size="small" @click="emit('remove', row.doc_id)">删除</el-button>
      </template>
    </el-table-column>
  </el-table>
  </div>
</template>

<style scoped>
.table-scroll {
  overflow-x: auto;
}
</style>
