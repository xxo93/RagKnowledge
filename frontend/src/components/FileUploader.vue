<script setup>
// 文档上传组件（P-01）：点击选择 / 拖拽上传，后缀白名单 + 10MB 限制，处理中 loading。
// M1 接通 uploadDoc；当前为交互结构骨架。
defineEmits(['upload'])
const onPick = (e) => {
  const file = e.target.files?.[0]
  if (file) /* TODO(M1): 先前端校验后缀/大小，再 emit('upload', file) */ e.target.value = ''
}
</script>

<template>
  <div class="uploader" @dragover.prevent @drop.prevent>
    <input type="file" accept=".pdf,.docx,.md" @change="onPick" hidden id="doc-picker" />
    <label class="drop-hint" for="doc-picker">
      <el-icon class="up-icon"><i>⬆</i></el-icon>
      <div>点击选择或拖拽 PDF / DOCX / Markdown 到此处上传</div>
      <div class="sub">单文件 ≤ 10MB；上传后同步完成解析、清洗、切片与向量化（M1 接通）</div>
    </label>
  </div>
</template>

<style scoped>
.uploader { border: 1px dashed #c0c4cc; border-radius: 8px; padding: 24px; }
.drop-hint { cursor: pointer; text-align: center; color: #606266; }
.sub { font-size: 12px; color: #909399; margin-top: 6px; }
.up-icon { font-size: 28px; }
</style>
