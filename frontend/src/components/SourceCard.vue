<script setup>
// 引用来源卡片（P-02 / F-07）：文档名 + 片段序号 + 相似度分数(两位小数) + 摘要。
defineProps({ source: { type: Object, default: () => ({}) } })
</script>

<template>
  <el-card class="source-card" shadow="never">
    <div class="line">
      <span class="doc">《{{ source.doc_name }}》</span>
      <el-tag size="small" type="info">片段 #{{ source.chunk_index }}</el-tag>
      <span class="scores">
        <span v-if="source.rerank_score != null" class="rerank">重排 {{ Number(source.rerank_score).toFixed(2) }}</span>
        <span class="score">相似度 {{ Number(source.score || 0).toFixed(2) }}</span>
      </span>
    </div>
    <div class="snippet">{{ source.snippet }}</div>
  </el-card>
</template>

<style scoped>
.source-card { padding: 6px 10px; background: #fff; }
.line { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.doc { font-weight: 600; }
.scores { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.score { color: #909399; }
.rerank { color: #16a34a; font-weight: 600; }
.snippet { font-size: 12px; color: #606266; margin-top: 4px; }
</style>
