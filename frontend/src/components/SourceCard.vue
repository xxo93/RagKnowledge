<script setup>
// 引用来源卡片（P-02 / F-07）：文档名 + 片段序号 + 相似度分数(两位小数) + 摘要。
defineProps({ source: { type: Object, default: () => ({}) } })
</script>

<template>
  <el-card class="source-card" shadow="never">
    <div class="line">
      <span class="doc">《{{ source.doc_name }}》</span>
      <el-tag size="small" type="info">片段 #{{ source.chunk_index }}</el-tag>
      <span class="score">相似度 {{ Number(source.score || 0).toFixed(2) }}</span>
    </div>
    <div class="snippet">{{ source.snippet }}</div>
  </el-card>
</template>

<style scoped>
.source-card { padding: 6px 10px; background: #fff; }
.line { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.doc { font-weight: 600; }
.score { margin-left: auto; color: #909399; }
.snippet { font-size: 12px; color: #606266; margin-top: 4px; }
</style>
