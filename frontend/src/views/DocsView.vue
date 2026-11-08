<script setup>
// 知识库管理页（需求 P-01 / F-01~F-03）：上传 + 文档表格 + 删除。
// M1 接通：uploadDoc / listDocs / deleteDoc；当前为结构骨架与配置信息展示。
import { ref, onMounted } from 'vue'
import FileUploader from '../components/FileUploader.vue'
import DocTable from '../components/DocTable.vue'
import { fetchSystemInfo } from '../api/docs'

const sysInfo = ref(null)

onMounted(async () => {
  try {
    sysInfo.value = await fetchSystemInfo()
  } catch (_) {
    sysInfo.value = null // 后端未启动时静默降级，不阻塞页面结构展示
  }
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

    <!-- 上传区：点击 / 拖拽；处理中 loading；失败提示（M1 接逻辑） -->
    <FileUploader />

    <el-divider />

    <!-- 文档列表：名称 / 状态标签 / 切片数 / 上传时间 / 删除按钮 -->
    <DocTable />
  </el-card>
</template>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; }
</style>
