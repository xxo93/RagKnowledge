<script setup>
// 工具调用标签：running 显示进行态；done 使用后端完成态文案；旧后端文案做兼容转换。
import { computed } from 'vue'

const props = defineProps({
  tool: { type: Object, default: () => ({}) }
})

const typeMap = { running: 'warning', done: 'success', failed: 'danger' }

const displayLabel = computed(() => {
  const label = props.tool.label || props.tool.name || ''
  if (props.tool.status === 'done' && /^正在/.test(label)) {
    return `${label.replace(/^正在/, '').replace(/…$/, '')}完成`
  }
  return label
})

const icon = computed(() => {
  if (props.tool.status === 'running') return '⏳ '
  if (props.tool.status === 'failed') return '✖ '
  return '✔ '
})
</script>

<template>
  <el-tag size="small" :type="typeMap[tool.status] || 'info'" effect="plain">
    {{ icon }}{{ displayLabel }}
    <span v-if="Number(tool.round) >= 1"> · 第{{ Number(tool.round) + 1 }}轮</span>
  </el-tag>
</template>
