<script setup>
// 工具调用标签：展示工具名称、执行状态与轮次
defineProps({ tool: { type: Object, default: () => ({}) } })
const typeMap = { running: 'warning', done: 'success' }
</script>

<template>
  <el-tag size="small" :type="typeMap[tool.status] || 'info'" effect="plain">
    {{ tool.status === 'running' ? '⏳ ' : '✔ ' }}{{ tool.label || tool.name
    }}<span v-if="Number(tool.round) >= 1"> · 第{{ Number(tool.round) + 1 }}轮</span>
  </el-tag>
</template>
