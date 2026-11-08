<script setup>
// 工具调用标签（P-02 / F-08）：展示「已检索知识库 / 已执行计算 / 正在统计…」。
defineProps({ tool: { type: Object, default: () => ({}) } })
const typeMap = { running: 'warning', done: 'success' }
</script>

<template>
  <el-tag size="small" :type="typeMap[tool.status] || 'info'" effect="plain">
    {{ tool.status === 'running' ? '⏳ ' : '✔ ' }}{{ tool.label || tool.name }}
  </el-tag>
</template>
