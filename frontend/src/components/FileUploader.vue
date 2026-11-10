<script setup>
// 文档上传组件（P-01 / F-01）：支持点击多选 / 拖拽多个文件；前端先做后缀白名单 + 10MB 校验，
// 合法文件以数组交给父组件顺序调用上传 API；处理中 loading。
import { ElMessage } from 'element-plus'

defineProps({ uploading: { type: Boolean, default: false } })
const emit = defineEmits(['upload'])

const ALLOWED_EXTS = ['.pdf', '.docx', '.md']
const MAX_MB = 10
const MAX_BYTES = MAX_MB * 1024 * 1024

function pick(e) {
  const files = Array.from(e.target?.files || [])
  e.target.value = '' // 允许重复选择同一文件
  if (files.length) emitValidFiles(files)
}

function onDrop(e) {
  const files = Array.from(e.dataTransfer?.files || [])
  if (files.length) emitValidFiles(files)
}

function extOf(name) {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot).toLowerCase() : ''
}

function emitValidFiles(files) {
  const valid = []
  const skipped = []
  for (const file of files) {
    if (!ALLOWED_EXTS.includes(extOf(file.name))) {
      skipped.push(`${file.name}(格式不支持)`)
      continue
    }
    if (file.size > MAX_BYTES) {
      skipped.push(`${file.name}(超 ${MAX_MB}MB)`)
      continue
    }
    valid.push(file)
  }
  if (skipped.length) ElMessage.warning(`已跳过：${skipped.join('、')}`)
  if (valid.length) emit('upload', valid)
}
</script>

<template>
  <div
    class="uploader"
    :class="{ busy: uploading }"
    @dragover.prevent
    @drop.prevent="onDrop"
    v-loading="uploading"
    element-loading-text="正在顺序解析、切片并向量化入库…"
  >
    <input type="file" accept=".pdf,.docx,.md" multiple @change="pick" hidden id="doc-picker" />
    <label class="drop-hint" for="doc-picker">
      <el-icon class="up-icon">⬆</el-icon>
      <div>点击多选或一次拖入多个 PDF / DOCX / Markdown 文件</div>
      <div class="sub">单文件 ≤ {{ MAX_MB }}MB；前端顺序调用现有上传接口，逐文件完成解析、清洗、切片与向量化</div>
    </label>
  </div>
</template>

<style scoped>
.uploader { border: 1px dashed #c0c4cc; border-radius: 8px; padding: 24px; }
.uploader.busy { opacity: 0.8; }
.drop-hint { cursor: pointer; text-align: center; color: #606266; }
.sub { font-size: 12px; color: #909399; margin-top: 6px; }
.up-icon { font-size: 28px; }
</style>
