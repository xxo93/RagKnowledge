<script setup>
// v3 / WP4：消息内嵌任务计划卡 / 任务中心详情卡（同一组件复用）
// 计划 → 执行 → 反思的实时进度：状态、预算、每步耗时与输出，可在卡内开始/暂停/取消/继续。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownRenderer from './MarkdownRenderer.vue'
import {
  cancelTask,
  confirmStep,
  pauseTask,
  resumeTask,
  runTask,
  streamWorkflowEvents
} from '../api/workflow'

const props = defineProps({
  task: { type: Object, required: true }
})

const STATUS = {
  planning: { label: '规划中', tone: 'info' },
  running: { label: '执行中', tone: 'primary' },
  awaiting_confirm: { label: '待确认', tone: 'warning' },
  paused: { label: '已暂停', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'info' },
  budget_exceeded: { label: '预算耗尽', tone: 'warning' },
  interrupted: { label: '已中断', tone: 'danger' }
}

const STEP_STATUS = {
  pending: { label: '等待', tone: 'info' },
  running: { label: '执行中', tone: 'primary' },
  done: { label: '完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  skipped: { label: '跳过', tone: 'info' },
  awaiting_confirm: { label: '待确认', tone: 'warning' }
}

const localTask = ref(null)
const busy = ref(false)
const expanded = ref(new Set())
const tick = ref(0)
let client = null
let timer = null

const view = computed(() => localTask.value || props.task)
const statusMeta = computed(() => STATUS[view.value?.status] || { label: view.value?.status || '未知', tone: 'info' })
const steps = computed(() => view.value?.steps || [])
const traceSteps = computed(() =>
  steps.value
    .filter((s) => s.started_at)
    .slice()
    .sort((a, b) => new Date(a.started_at).getTime() - new Date(b.started_at).getTime())
)
const hasParallel = computed(() =>
  traceSteps.value.some((step, i) => {
    if (!i) return false
    const prevEnd = traceSteps.value[i - 1].finished_at
      ? new Date(traceSteps.value[i - 1].finished_at).getTime()
      : Infinity
    return new Date(step.started_at).getTime() < prevEnd
  })
)
const progress = computed(() => view.value?.progress || { total: steps.value.length, done: 0, failed: 0, percent: 0 })
const awaitStep = computed(() => steps.value.find((s) => s.status === 'awaiting_confirm'))
const canStart = computed(() => ['awaiting_confirm', 'interrupted', 'paused', 'failed'].includes(view.value?.status) && !awaitStep.value)
const canPause = computed(() => view.value?.status === 'running')
const canCancel = computed(() => !['completed', 'failed', 'cancelled', 'budget_exceeded'].includes(view.value?.status))
const isLive = computed(() => ['running', 'awaiting_confirm'].includes(view.value?.status))

const elapsedSeconds = computed(() => {
  const t = view.value?.created_at
  if (!t) return 0
  const usedMs = Number(view.value?.used?.elapsed_ms)
  if (view.value?.status === 'completed' && usedMs) return Math.round(usedMs / 1000)
  // 预算只统计真正执行的时间：从第一个步骤开始执行算起，待确认阶段不消耗预算
  const started = steps.value.map((s) => s.started_at).filter(Boolean).sort()[0] || null
  if (!started && ['awaiting_confirm', 'planning'].includes(view.value?.status)) return 0
  const base = started || t
  const end = view.value?.finished_at ? new Date(view.value.finished_at).getTime() : Date.now()
  void tick.value
  return Math.max(0, Math.round((end - new Date(base).getTime()) / 1000))
})

const budgetSeconds = computed(() => Number(view.value?.budget?.max_seconds || 0))
const budgetPercent = computed(() => {
  if (!budgetSeconds.value) return 0
  return Math.min(100, Math.round(elapsedSeconds.value * 100 / budgetSeconds.value))
})

function statusOf(value) {
  return STATUS[value] || { label: value || '未知', tone: 'info' }
}

function stepStatusOf(value) {
  return STEP_STATUS[value] || { label: value || '未知', tone: 'info' }
}

function clock(step) {
  if (!step.started_at) return ''
  return new Date(step.started_at).toLocaleTimeString('zh-CN', { hour12: false })
}

function duration(step) {
  if (!step.started_at) return ''
  const end = step.finished_at ? new Date(step.finished_at).getTime() : Date.now()
  const ms = Math.max(0, end - new Date(step.started_at).getTime())
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

function outputSummary(step) {
  const out = step.output || {}
  if (out.summary) return String(out.summary)
  if (out.table) return `返回 ${out.row_count ?? (out.table.rows || []).length} 行业务数据`
  if (out.sources?.length) return `找到 ${out.sources.length} 条来源`
  if (step.error_msg) return step.error_msg
  if (step.skipped_reason) return step.skipped_reason
  if (step.status === 'awaiting_confirm') return '等待人工确认后执行'
  if (step.status === 'running') return '执行中…'
  if (step.status === 'pending') return '等待执行'
  return '—'
}

function plainText(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*`_~]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function outputDetail(step) {
  try {
    return JSON.stringify(step.output || {}, null, 2)
  } catch {
    return String(step.output || '')
  }
}

function toggle(index) {
  const set = new Set(expanded.value)
  if (set.has(index)) set.delete(index)
  else set.add(index)
  expanded.value = set
}

function applyTask(task) {
  if (!task || task.task_id !== props.task.task_id) return
  localTask.value = task
  if (['completed', 'failed', 'cancelled', 'budget_exceeded'].includes(task.status)) {
    disconnect()
    stopTimer()
  }
}

function connect() {
  disconnect()
  if (!isLive.value) return
  client = streamWorkflowEvents(props.task.task_id, {
    onSnapshot(data) { applyTask(data?.task) },
    onEvent(_name, data) {
      if (data?.task) applyTask(data.task)
      if (data?.status && !data.task) {
        localTask.value = { ...view.value, status: data.status }
      }
    },
    onDone(data) {
      applyTask(data?.task || data)
      disconnect()
    },
    onError() { /* 断线后由页面刷新兜底，不在卡内反复弹错 */ }
  })
}

function disconnect() {
  client?.abort?.()
  client = null
}

watch(
  () => [props.task?.task_id, props.task?.status],
  () => {
    localTask.value = null
    connect()
    if (isLive.value) startTimer()
    else stopTimer()
  },
  { immediate: true }
)

function startTimer() {
  stopTimer()
  timer = setInterval(() => { tick.value += 1 }, 1000)
}

function stopTimer() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

async function withBusy(fn) {
  if (busy.value) return
  busy.value = true
  try {
    const task = await fn()
    applyTask(task)
    if (['completed', 'failed', 'cancelled', 'budget_exceeded'].includes(task?.status)) disconnect()
  } catch (e) {
    ElMessage.error(e?.message || '操作失败')
  } finally {
    busy.value = false
  }
}

function start() {
  withBusy(() => runTask(props.task.task_id))
}

function resume() {
  withBusy(() => resumeTask(props.task.task_id))
}

function pause() {
  withBusy(() => pauseTask(props.task.task_id))
}

async function cancel() {
  try {
    await ElMessageBox.confirm('取消后已完成的步骤结果会保留，未执行的步骤不再执行。确认取消？', '取消任务', {
      confirmButtonText: '取消任务',
      cancelButtonText: '继续执行',
      type: 'warning'
    })
  } catch {
    return
  }
  withBusy(() => cancelTask(props.task.task_id))
}

function confirmStepAction(step, action) {
  withBusy(() => confirmStep(props.task.task_id, step.step_id, action))
}

onBeforeUnmount(() => {
  disconnect()
  stopTimer()
})

function fmtTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN', { hour12: false })
}

async function copyReport() {
  try {
    await navigator.clipboard.writeText(view.value?.result_summary || '')
    ElMessage.success('已复制任务报告')
  } catch {
    ElMessage.warning('当前浏览器不支持自动复制')
  }
}
</script>

<template>
  <section class="plan-card" :class="`is-${view.status}`" :data-task-id="view.task_id">
    <header class="plan-head">
      <div class="plan-title-row">
        <span class="plan-kicker">多步任务</span>
        <el-tag :type="statusMeta.tone" size="small" effect="light">{{ statusMeta.label }}</el-tag>
        <span class="plan-goal">{{ view.goal }}</span>
      </div>
      <div class="plan-meta">
        <span>#{{ view.task_id }}</span>
        <span>已用 {{ elapsedSeconds }}s<span v-if="budgetSeconds"> / 预算 {{ budgetSeconds }}s</span></span>
        <span>{{ progress.done }}/{{ progress.total }} 步</span>
      </div>
    </header>

    <div class="plan-progress">
      <el-progress
        :percentage="progress.percent"
        :status="view.status === 'failed' ? 'exception' : (view.status === 'completed' ? 'success' : undefined)"
        :stroke-width="6"
        :show-text="false"
      />
      <div class="plan-progress-foot">
        <span>执行进度 {{ progress.percent }}%</span>
        <span v-if="budgetSeconds" :class="{ 'budget-warn': budgetPercent >= 80 }">时间预算 {{ budgetPercent }}%</span>
      </div>
    </div>

    <div v-if="hasParallel" class="plan-trace">
      <div class="plan-trace-head">执行时序（按实际开始时间）</div>
      <div v-for="step in traceSteps" :key="`trace-${step.step_id}`" class="plan-trace-row">
        <span class="trace-index">{{ step.step_index }}</span>
        <span class="trace-clock">{{ clock(step) }}</span>
        <span class="trace-title">{{ step.title }}</span>
        <span class="trace-dur">{{ duration(step) }}</span>
      </div>
    </div>

    <ol class="plan-steps">
      <li v-for="step in steps" :key="step.step_id" class="plan-step" :class="`st-${step.status}`">
        <div class="step-rail">
          <span class="step-dot">{{ step.step_index }}</span>
          <span class="step-line" aria-hidden="true"></span>
        </div>
        <div class="step-body">
          <div class="step-top">
            <span class="step-title">{{ step.title }}</span>
            <el-tag :type="stepStatusOf(step.status).tone" size="small" effect="plain">
              {{ stepStatusOf(step.status).label }}
            </el-tag>
            <span class="step-tool">{{ step.tool_label }}</span>
            <span v-if="duration(step)" class="step-time">{{ duration(step) }}</span>
            <button
              v-if="step.output || step.error_msg"
              type="button"
              class="step-toggle"
              :aria-expanded="expanded.has(step.step_index)"
              @click="toggle(step.step_index)"
            >{{ expanded.has(step.step_index) ? '收起' : '查看输出' }}</button>
          </div>
          <div class="step-summary">{{ plainText(outputSummary(step)) }}</div>
          <div v-if="step.requires_confirm" class="step-confirm-hint">
            <span>该步骤需要人工确认后继续</span>
            <template v-if="step.status === 'awaiting_confirm'">
              <el-button size="small" type="primary" :loading="busy" @click="confirmStepAction(step, 'continue')">继续</el-button>
              <el-button size="small" :loading="busy" @click="confirmStepAction(step, 'skip')">跳过</el-button>
              <el-button size="small" :loading="busy" @click="confirmStepAction(step, 'cancel')">取消任务</el-button>
            </template>
          </div>
          <div v-if="expanded.has(step.step_index)" class="step-output">
            <MarkdownRenderer v-if="(step.output || {}).summary" :content="outputSummary(step)" />
            <pre v-else>{{ outputDetail(step) }}</pre>
          </div>
        </div>
      </li>
    </ol>

    <div v-if="view.error_msg" class="plan-error">{{ view.error_msg }}</div>

    <div v-if="view.result_summary" class="plan-report">
      <div class="report-head">
        <span>任务报告</span>
        <button type="button" class="report-copy" @click="copyReport">复制</button>
      </div>
      <MarkdownRenderer :content="view.result_summary" />
    </div>

    <footer class="plan-actions">
      <el-button v-if="canStart && view.status === 'awaiting_confirm'" type="primary" size="small" :loading="busy" @click="start">
        开始执行
      </el-button>
      <el-button v-else-if="canStart" type="primary" size="small" :loading="busy" @click="resume">
        {{ view.status === 'paused' ? '继续执行' : '恢复执行' }}
      </el-button>
      <el-button v-if="canPause" size="small" :loading="busy" @click="pause">暂停</el-button>
      <el-button v-if="canCancel" size="small" type="danger" plain :loading="busy" @click="cancel">取消</el-button>
      <span class="plan-hint">
        {{ view.status === 'awaiting_confirm' ? '计划先确认后执行，每一步都复用既有能力' : `创建于 ${fmtTime(view.created_at)}` }}
      </span>
    </footer>
  </section>
</template>

<style scoped>
.plan-card {
  margin-top: 10px;
  border: 1px solid var(--rk-border, #e5e7eb);
  border-radius: 12px;
  background: var(--rk-surface, #fff);
  padding: 14px 14px 12px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
.plan-head { display: flex; flex-direction: column; gap: 6px; }
.plan-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.plan-kicker { font-size: 12px; color: #6941c6; background: #f4ebff; border-radius: 999px; padding: 1px 8px; font-weight: 600; }
.plan-goal { font-weight: 600; font-size: 14px; line-height: 1.5; color: var(--rk-text, #101828); }
.plan-meta { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--rk-text-muted, #667085); }
.plan-progress { margin: 12px 0 10px; }
.plan-progress-foot { display: flex; justify-content: space-between; font-size: 12px; color: #667085; margin-top: 4px; }
.budget-warn { color: #b54708; font-weight: 600; }
.plan-steps { list-style: none; padding: 0; margin: 0; }
.plan-trace {
  margin: 4px 0 12px; padding: 8px 10px; border: 1px dashed #d0d5dd;
  border-radius: 8px; background: #fcfcfd;
}
.plan-trace-head { font-size: 11px; font-weight: 600; color: #667085; margin-bottom: 5px; }
.plan-trace-row { display: flex; align-items: center; gap: 8px; font-size: 11px; color: #475467; line-height: 1.8; }
.trace-index {
  width: 16px; height: 16px; border-radius: 50%; display: grid; place-items: center;
  background: #eff4ff; color: #175cd3; font-weight: 700; flex: 0 0 auto;
}
.trace-clock { font-variant-numeric: tabular-nums; color: #667085; flex: 0 0 auto; }
.trace-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-dur { flex: 0 0 auto; color: #667085; }
.plan-step { display: flex; gap: 10px; }
.step-rail { width: 22px; display: flex; flex-direction: column; align-items: center; flex: 0 0 auto; }
.step-dot {
  width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center;
  font-size: 11px; font-weight: 700; color: #475467; background: #f2f4f7; border: 1px solid #d0d5dd;
}
.plan-step.st-running .step-dot { background: #eff4ff; border-color: #84adff; color: #175cd3; }
.plan-step.st-done .step-dot { background: #ecfdf3; border-color: #75e0a7; color: #027a48; }
.plan-step.st-failed .step-dot { background: #fef3f2; border-color: #fda29b; color: #b42318; }
.plan-step.st-awaiting_confirm .step-dot { background: #fffaeb; border-color: #fec84b; color: #b54708; }
.step-line { flex: 1; width: 1px; background: #eaecf0; min-height: 10px; }
.plan-step:last-child .step-line { display: none; }
.step-body { flex: 1; min-width: 0; padding-bottom: 12px; }
.step-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.step-title { font-size: 13px; font-weight: 600; color: var(--rk-text, #101828); }
.step-tool, .step-time { font-size: 11px; color: #667085; }
.step-toggle {
  margin-left: auto; border: none; background: none; color: #175cd3; font-size: 12px;
  cursor: pointer; padding: 2px 4px; border-radius: 4px;
}
.step-toggle:hover { background: #eff4ff; }
.step-summary { font-size: 12px; color: #475467; line-height: 1.6; margin-top: 4px; white-space: normal; word-break: break-word; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.step-confirm-hint {
  margin-top: 6px; padding: 6px 8px; background: #fffaeb; border: 1px solid #fedf89;
  border-radius: 8px; font-size: 12px; color: #93370d; display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
}
.step-output { margin-top: 8px; background: #f9fafb; border: 1px solid #eaecf0; border-radius: 8px; padding: 8px; }
.step-output pre { margin: 0; font-size: 11px; line-height: 1.55; max-height: 240px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
.step-output :deep(.markdown-body) { font-size: 11px; line-height: 1.6; max-height: 240px; overflow: auto; }
.step-output :deep(.markdown-body h1),
.step-output :deep(.markdown-body h2),
.step-output :deep(.markdown-body h3) { font-size: 12px; margin: 6px 0 4px; }
.plan-error { margin-top: 8px; padding: 8px 10px; border-radius: 8px; background: #fef3f2; color: #b42318; font-size: 12px; }
.plan-report { margin-top: 10px; border: 1px solid #eaecf0; border-radius: 10px; background: #fcfcfd; max-height: 340px; overflow: auto; }
.report-head { display: flex; justify-content: space-between; padding: 8px 10px; border-bottom: 1px solid #eaecf0; font-size: 12px; font-weight: 600; color: #344054; position: sticky; top: 0; background: #fcfcfd; z-index: 1; }
.report-copy { border: none; background: none; color: #175cd3; cursor: pointer; font-size: 12px; }
.plan-report pre { margin: 0; padding: 10px; font-size: 12px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
.plan-report :deep(.markdown-body) { padding: 8px 10px; font-size: 12px; line-height: 1.65; }
.plan-report :deep(.markdown-body h1) { font-size: 16px; margin: 2px 0 8px; }
.plan-report :deep(.markdown-body h2) { font-size: 14px; margin: 10px 0 4px; }
.plan-report :deep(.markdown-body h3) { font-size: 13px; margin: 8px 0 4px; }
.plan-report :deep(.markdown-body p) { margin: 4px 0; }
.plan-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; padding-top: 10px; border-top: 1px solid #eaecf0; }
.plan-hint { font-size: 11px; color: #667085; margin-left: auto; }
@media (max-width: 600px) {
  .plan-card { padding: 12px 10px 10px; }
  .plan-meta { gap: 8px; }
  .plan-hint { margin-left: 0; width: 100%; }
  .step-toggle { margin-left: 0; }
}
</style>
