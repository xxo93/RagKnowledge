<script setup>
// 文档表格（P-01 / F-02）：名称、状态标签、切片数、上传时间、删除按钮。
// 状态：0 处理中(warning) / 1 成功(success) / 2 失败(danger + error_msg tooltip)。
// M1 接通 listDocs / deleteDoc；当前固定列结构与空态。
import { ref } from 'vue'
const rows = ref([]) // TODO(M1): 分页加载
const statusMap = { 0: '处理中', 1: '成功', 2: '失败' }
</script>

<template>
  <el-table :data="rows" empty-text="暂无文档，上传后在此展示" border>
    <el-table-column prop="doc_name" label="文档名称" min-width="220" />
    <el-table-column label="状态" width="100">
      <template #default="{ row }">
        <el-tag :type="row.status === 1 ? 'success' : row.status === 2 ? 'danger' : 'warning'" size="small">
          {{ statusMap[row.status] || '未知' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column prop="chunk_count" label="切片数" width="90" />
    <el-table-column prop="file_size" label="大小(B)" width="110" />
    <el-table-column prop="create_time" label="上传时间" width="180" />
    <el-table-column label="操作" width="90">
      <template #default>
        <!-- TODO(M1): 删除确认 + emit('remove', doc_id) -->
        <el-button type="danger" link size="small">删除</el-button>
      </template>
    </el-table-column>
  </el-table>
</template>
