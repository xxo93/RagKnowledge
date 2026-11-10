<script setup>
// 知识库管理页：批量上传、文档列表、删除、重解析与索引重建
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import FileUploader from '../components/FileUploader.vue'
import DocTable from '../components/DocTable.vue'
import {
  fetchSystemInfo,
  uploadDocsBatch,
  listDocs,
  deleteDoc,
  reparseDoc,
  rebuildIndex
} from '../api/docs'

const sysInfo = ref(null)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const size = 10
const tableLoading = ref(false)
const uploading = ref(false)
const indexBusy = ref(false)
const processingIds = ref([])

async function refresh(p = page.value) {
  tableLoading.value = true
  try {
    const data = await listDocs(p, size)
    rows.value = data?.list || []
    total.value = data?.total || 0
    page.value = p
  } catch (e) {
    ElMessage.error(e.message || '文档列表加载失败')
  } finally {
    tableLoading.value = false
  }
}

async function onUpload(payload) {
  const files = (Array.isArray(payload) ? payload : [payload]).filter(Boolean)
  if (!files.length) return
  uploading.value = true
  try {
    const data = await uploadDocsBatch(files)
    const success = data?.success || []
    const failed = data?.failed || []
    const totalChunks = success.reduce((sum, item) => sum + Number(item.chunk_count || 0), 0)
    if (failed.length === 0) {
      ElMessage.success(`批量上传完成：${success.length} 个文件，共 ${totalChunks} 个切片`)
    } else if (success.length > 0) {
      ElMessage.warning(`部分成功：成功 ${success.length} 个、失败 ${failed.length} 个；失败：${failed.map(f => f.file_name).join('、')}`)
    } else {
      ElMessage.error(`批量上传失败：${failed.map(f => f.file_name).join('、')}`)
    }
    await refresh(1)
  } catch (e) {
    ElMessage.error(e.message || '批量上传失败，请查看后端日志')
  } finally {
    uploading.value = false
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

    <!-- 上传区：前端先做后缀与大小校验，服务端批量顺序处理，完成后刷新列表 -->
    <FileUploader :uploading="uploading" @upload="onUpload" />

    <el-divider />

    <div class="toolbar">
      <span class="toolbar-title">运维操作</span>
      <el-button type="primary" plain size="small" :loading="indexBusy" @click="onRebuildIndex">
        重建 HNSW 索引
      </el-button>
    </div>

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
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.toolbar-title { font-size: 13px; color: #606266; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
