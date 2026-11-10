<script setup>
// 文档表格：展示状态、切片数、大小、上传时间，并触发重解析与删除操作
defineProps({
  rows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  processingIds: { type: Array, default: () => [] }
})
const emit = defineEmits(['remove', 'reparse'])

// 状态：0 处理中(warning) / 1 成功(success) / 2 失败(danger)
const statusMap = { 0: '处理中', 1: '成功', 2: '失败' }
const tagType = (row) => (row.status === 1 ? 'success' : row.status === 2 ? 'danger' : 'warning')
</script>

<template>
  <el-table :data="rows" v-loading="loading" empty-text="暂无文档，上传后在此展示" border>
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
    <el-table-column prop="file_size" label="大小(B)" width="110" />
    <el-table-column prop="create_time" label="上传时间" width="180" />
    <el-table-column label="操作" width="150">
      <template #default="{ row }">
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
</template>
