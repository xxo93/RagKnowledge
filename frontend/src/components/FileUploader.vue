<script setup>
// 文档上传入口：点击多选或拖拽多个文件；前端先做后缀白名单与大小校验，
// 合法文件交给父组件排队；不支持/超限文件带原因返回，由上传队列统一展示
import { ElMessage } from 'element-plus'

const props = defineProps({
  uploading: { type: Boolean, default: false },
  // 单文件大小上限（MB）：由父组件按服务端 upload_max_mb 传入，避免前端写死
  maxMb: { type: Number, default: 100 }
})
const emit = defineEmits(['files', 'invalid'])

const ALLOWED_EXTS = ['.pdf', '.docx', '.md', '.xlsx', '.xlsm', '.xls', '.csv', '.doc']
const ACCEPT = ALLOWED_EXTS.join(',')

function pick(e) {
  const files = Array.from(e.target?.files || [])
  e.target.value = '' // 允许重复选择同一文件
  if (files.length) classify(files)
}

function onDrop(e) {
  const files = Array.from(e.dataTransfer?.files || [])
  if (files.length) classify(files)
}

function extOf(name) {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot).toLowerCase() : ''
}

function classify(files) {
  const valid = []
  const skipped = []
  const limitBytes = props.maxMb * 1024 * 1024
  for (const file of files) {
    if (!ALLOWED_EXTS.includes(extOf(file.name))) {
      skipped.push({ name: file.name, reason: '格式不支持', size: file.size })
      continue
    }
    if (file.size > limitBytes) {
      skipped.push({ name: file.name, reason: `超过 ${props.maxMb}MB`, size: file.size })
      continue
    }
    valid.push(file)
  }
  if (skipped.length) {
    ElMessage.warning(`已跳过 ${skipped.length} 个文件：${skipped.map((s) => `${s.name}(${s.reason})`).join('、')}`)
    emit('invalid', skipped)
  }
  if (valid.length) emit('files', valid)
}
</script>

<template>
  <div
    class="uploader"
    :class="{ busy: uploading }"
    @dragover.prevent
    @drop.prevent="onDrop"
  >
    <input
      type="file"
      :accept="ACCEPT"
      multiple
      @change="pick"
      hidden
      id="doc-picker"
    />
    <label class="drop-hint" for="doc-picker">
      <span class="up-icon" aria-hidden="true">⬆</span>
      <div class="drop-title">拖拽文件到这里，或点击选择</div>
      <div class="sub">
        支持 PDF / DOCX / MD / XLSX / XLSM / XLS / CSV / DOC
      </div>
      <div class="sub">
        单文件 ≤ {{ maxMb }}MB；大文件自动分块上传，逐文件完成解析、切片与向量化
      </div>
    </label>
    <div class="upload-note" aria-hidden="true">
      <span>多文件会按队列逐个处理</span>
      <span>失败可单文件重试</span>
    </div>
  </div>
</template>

<style scoped>
.uploader {
  position: relative;
  border: 1px dashed var(--border);
  border-radius: 8px;
  padding: 22px 18px 14px;
  background: var(--panel);
}
.uploader.busy { opacity: 0.75; }
.uploader:hover { border-color: var(--primary); }
.drop-hint { cursor: pointer; text-align: center; color: var(--text-muted); display: block; }
.drop-title { font-size: 14px; color: var(--text-secondary); margin-top: 6px; }
.sub { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.up-icon { font-size: 26px; line-height: 1; color: var(--primary); }
.upload-note {
  display: flex;
  justify-content: center;
  gap: 16px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-muted);
}
@media (max-width: 640px) {
  .upload-note { flex-direction: column; gap: 4px; align-items: center; }
}
</style>
