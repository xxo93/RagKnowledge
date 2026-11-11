<script setup>
// 文档上传组件：支持点击多选或拖拽多个文件；前端先做后缀白名单与大小校验，
// 合法文件数组交给父组件按「小文件单请求 / 大文件分块」上传
import { ElMessage } from 'element-plus'

const props = defineProps({
  uploading: { type: Boolean, default: false },
  // 单文件大小上限（MB）：由父组件按服务端 upload_total_max_mb 传入，避免前端写死
  maxMb: { type: Number, default: 100 },
  // 上传进度文案，例如「a.pdf：分块上传 67%（3 片，8.0MB/12.0MB）」
  progressText: { type: String, default: '' }
})
const emit = defineEmits(['upload', 'invalid'])

const ALLOWED_EXTS = ['.pdf', '.docx', '.md']

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
  const limitBytes = props.maxMb * 1024 * 1024
  for (const file of files) {
    if (!ALLOWED_EXTS.includes(extOf(file.name))) {
      skipped.push(`${file.name}(格式不支持)`)
      continue
    }
    if (file.size > limitBytes) {
      skipped.push(`${file.name}(超 ${props.maxMb}MB)`)
      continue
    }
    valid.push(file)
  }
  if (skipped.length) {
    ElMessage.warning(`已跳过：${skipped.join('、')}`)
    emit('invalid', skipped)
  }
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
    :element-loading-text="progressText || '正在解析、切片并向量化入库…'"
  >
    <input type="file" accept=".pdf,.docx,.md" multiple @change="pick" hidden id="doc-picker" />
    <label class="drop-hint" for="doc-picker">
      <el-icon class="up-icon">⬆</el-icon>
      <div>点击多选或一次拖入多个 PDF / DOCX / Markdown 文件</div>
      <div class="sub">
        单文件 ≤ {{ maxMb }}MB；大文件自动分块并行上传，逐文件完成解析、清洗、切片与向量化
      </div>
    </label>
  </div>
</template>

<style scoped>
.uploader { border: 1px dashed var(--border); border-radius: 8px; padding: 24px; }
.uploader.busy { opacity: 0.8; }
.drop-hint { cursor: pointer; text-align: center; color: var(--text-muted); }
.sub { font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
.up-icon { font-size: 28px; }
</style>
