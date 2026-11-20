<script setup>
// 安全 HTML 渲染：DOCX 转换结果与 Excel 样式表格统一走 DOMPurify 消毒。
// 仅允许文档预览需要的排版属性与 data:image 图片，禁止脚本、事件属性和外部危险协议。
import { computed } from 'vue'
import DOMPurify from 'dompurify'

const props = defineProps({
  html: { type: String, default: '' }
})

const ALLOWED_URI = /^(?:https?:|mailto:|data:image\/)/i

const sanitized = computed(() => DOMPurify.sanitize(props.html || '', {
  ADD_ATTR: [
    'style', 'colspan', 'rowspan', 'width', 'height', 'align', 'valign',
    'cellspacing', 'cellpadding', 'border', 'nowrap'
  ],
  ALLOWED_URI_REGEXP: ALLOWED_URI
}))
</script>

<template>
  <div class="safe-html" v-html="sanitized"></div>
</template>

<style scoped>
.safe-html {
  word-break: break-word;
}
</style>
