<script setup>
// v3 / WP4：任务中心 —— 任务列表 + 详情时间线 + 最终报告
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchWorkflowTask, listWorkflowTasks, planTask } from '../api/workflow'
import PlanCard from '../components/PlanCard.vue'

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'awaiting_confirm', label: '待确认' },
  { value: 'running', label: '执行中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
  { value: 'interrupted', label: '已中断' },
  { value: 'budget_exceeded', label: '预算耗尽' }
]
const STATUS_META = {
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

const loading = ref(false)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const status = ref('')
const keyword = ref('')
const createVisible = ref(false)
const goal = ref('')
const planning = ref(false)
const drawerVisible = ref(false)
const detail = ref(null)
const detailLoading = ref(false)
let timer = null

const activeCount = computed(() => rows.value.filter((r) => ['running', 'awaiting_confirm'].includes(r.status)).length)
const hasMore = computed(() => page.value * size.value < total.value)

async function load() {
  loading.value = true
  try {
    const data = await listWorkflowTasks({
      status: status.value || undefined,
      keyword: keyword.value.trim() || undefined,
      page: page.value,
      size: size.value
    })
    rows.value = data?.list || []
    total.value = data?.total ?? rows.value.length
  } catch {
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function changeFilter() {
  page.value = 1
  load()
}

async function openDetail(taskId) {
  drawerVisible.value = true
  detailLoading.value = true
  try {
    detail.value = await fetchWorkflowTask(taskId)
  } catch (e) {
    ElMessage.error(e?.message || '任务详情加载失败')
    drawerVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

async function createPlan() {
  const text = goal.value.trim()
  if (!text) {
    ElMessage.warning('请先描述任务目标')
    return
  }
  planning.value = true
  try {
    const task = await planTask({ goal: text })
    createVisible.value = false
    goal.value = ''
    ElMessage.success('计划已生成，请确认后开始执行')
    detail.value = task
    drawerVisible.value = true
    await load()
  } catch (e) {
    ElMessage.error(e?.message || '计划生成失败')
  } finally {
    planning.value = false
  }
}

function statusMeta(value) {
  return STATUS_META[value] || { label: value || '未知', tone: 'info' }
}

function fmtTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN', { hour12: false })
}

function plainSummary(text) {
  return String(text || '')
    .replace(/^#+\s*/gm, '')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function duration(row) {
  if (!row.created_at) return '—'
  const end = row.finished_at ? new Date(row.finished_at).getTime() : Date.now()
  const sec = Math.max(0, Math.round((end - new Date(row.created_at).getTime()) / 1000))
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${sec % 60}s`
}

onMounted(() => {
  load()
  // 列表只做低频刷新；运行中的实时进度在详情卡里走 SSE
  timer = setInterval(load, 15000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="workflow-page">
    <header class="wf-head">
      <div>
        <h2>任务中心</h2>
        <p>多步任务由「规划 → 执行 → 反思」编排；每步复用知识库、业务库、计算器等既有工具，执行前可确认、执行中可暂停或取消。</p>
      </div>
      <el-button type="primary" @click="createVisible = true">新建任务</el-button>
    </header>

    <div class="wf-overview">
      <div class="wf-stat"><span class="num">{{ total }}</span><span class="label">累计任务</span></div>
      <div class="wf-stat"><span class="num">{{ activeCount }}</span><span class="label">本页进行中</span></div>
      <div class="wf-stat"><span class="num">{{ rows.filter(r => r.status === 'completed').length }}</span><span class="label">本页已完成</span></div>
      <div class="wf-stat"><span class="num">{{ rows.filter(r => r.status === 'failed').length }}</span><span class="label">本页失败</span></div>
    </div>

    <div class="wf-toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索任务目标"
        clearable
        class="wf-search"
        @keyup.enter="search"
        @clear="search"
      />
      <el-select v-model="status" class="wf-status" placeholder="全部状态" @change="changeFilter">
        <el-option v-for="opt in STATUS_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
      <el-button @click="search">查询</el-button>
    </div>

    <div v-loading="loading" class="wf-list">
      <article v-for="row in rows" :key="row.task_id" class="wf-row" :data-task-id="row.task_id" @click="openDetail(row.task_id)">
        <div class="wf-row-main">
          <div class="wf-row-title">
            <el-tag :type="statusMeta(row.status).tone" size="small" effect="light">{{ statusMeta(row.status).label }}</el-tag>
            <span class="goal-text">{{ row.goal }}</span>
          </div>
          <div class="wf-row-meta">
            <span>#{{ row.task_id }}</span>
            <span>{{ row.step_done }}/{{ row.step_total }} 步</span>
            <span>耗时 {{ duration(row) }}</span>
            <span>{{ fmtTime(row.created_at) }}</span>
          </div>
          <div v-if="row.result_summary" class="wf-row-summary">{{ plainSummary(row.result_summary) }}</div>
        </div>
        <div class="wf-row-side">
          <div class="mini-progress">
            <span :style="{ width: (row.step_total ? Math.round(row.step_done * 100 / row.step_total) : 0) + '%' }"></span>
          </div>
          <span class="detail-link">查看详情 →</span>
        </div>
      </article>
      <div v-if="!loading && !rows.length" class="wf-empty">
        <div class="empty-title">还没有任务</div>
        <p>点击「新建任务」描述一个多步目标，例如「查一下退款流程，并统计知识库文档数量」。</p>
      </div>
    </div>

    <div v-if="total > size" class="wf-pager">
      <el-button :disabled="page <= 1" size="small" @click="page -= 1; load()">上一页</el-button>
      <span>{{ page }} / {{ Math.ceil(total / size) }}</span>
      <el-button :disabled="!hasMore" size="small" @click="page += 1; load()">下一页</el-button>
    </div>

    <el-dialog v-model="createVisible" title="新建任务" width="min(560px, 94vw)" append-to-body>
      <el-form label-position="top">
        <el-form-item label="任务目标">
          <el-input
            v-model="goal"
            type="textarea"
            :rows="4"
            maxlength="2000"
            show-word-limit
            placeholder="例如：先查询知识库里的退款流程，再统计当前文档总数，最后给出处理建议"
          />
        </el-form-item>
      </el-form>
      <div class="dialog-hint">系统会先生成执行计划（最多 6 步），你确认后才开始执行。</div>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="planning" @click="createPlan">生成计划</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="drawerVisible"
      title="任务详情"
      size="min(620px, 100vw)"
      append-to-body
      class="wf-drawer"
    >
      <div v-loading="detailLoading" class="wf-detail">
        <PlanCard v-if="detail" :task="detail" />
        <div v-else-if="!detailLoading" class="wf-empty">任务不存在或已被删除</div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.workflow-page { padding: 4px 2px 24px; max-width: 1100px; margin: 0 auto; }
.wf-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.wf-head h2 { margin: 0 0 4px; font-size: 20px; color: var(--rk-text, #101828); }
.wf-head p { margin: 0; color: #667085; font-size: 13px; line-height: 1.6; max-width: 720px; }
.wf-overview { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.wf-stat { background: var(--rk-surface, #fff); border: 1px solid #eaecf0; border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 2px; }
.wf-stat .num { font-size: 22px; font-weight: 700; color: #101828; line-height: 1.2; }
.wf-stat .label { font-size: 12px; color: #667085; }
.wf-toolbar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.wf-search { flex: 1; min-width: 180px; }
.wf-status { width: 150px; }
.wf-list { display: flex; flex-direction: column; gap: 10px; min-height: 180px; }
.wf-row {
  display: flex; gap: 16px; align-items: center; background: var(--rk-surface, #fff);
  border: 1px solid #eaecf0; border-radius: 12px; padding: 14px 16px; cursor: pointer;
  transition: border-color .15s, box-shadow .15s;
}
.wf-row:hover { border-color: #84adff; box-shadow: 0 2px 8px rgba(23, 92, 211, 0.08); }
.wf-row-main { flex: 1; min-width: 0; }
.wf-row-title { display: flex; gap: 8px; align-items: flex-start; }
.goal-text { font-size: 14px; font-weight: 600; color: #101828; line-height: 1.5; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.wf-row-meta { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; color: #667085; margin-top: 6px; }
.wf-row-summary { margin-top: 6px; font-size: 12px; color: #475467; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-row-side { width: 132px; flex: 0 0 auto; display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }
.mini-progress { width: 100%; height: 5px; border-radius: 999px; background: #f2f4f7; overflow: hidden; }
.mini-progress span { display: block; height: 100%; background: linear-gradient(90deg, #2970ff, #84adff); border-radius: 999px; }
.detail-link { font-size: 12px; color: #175cd3; }
.wf-empty { text-align: center; padding: 48px 16px; color: #667085; background: var(--rk-surface, #fff); border: 1px dashed #d0d5dd; border-radius: 12px; }
.empty-title { font-weight: 600; color: #344054; margin-bottom: 6px; }
.wf-empty p { margin: 0; font-size: 13px; }
.wf-pager { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 14px; font-size: 13px; color: #667085; }
.dialog-hint { font-size: 12px; color: #667085; }
.wf-detail { min-height: 200px; }
@media (max-width: 700px) {
  .wf-overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .wf-row-side { width: 72px; }
  .wf-row-summary { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
}
</style>
