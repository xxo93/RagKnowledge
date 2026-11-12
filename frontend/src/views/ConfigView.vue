<script setup>
// 管理员配置中心（M1）：检索组 + 生成组参数在线调整，保存后热生效
// 仅管理员可见；后端 require_admin 强制校验，隐藏菜单不是安全边界
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchAppConfig, saveAppConfig, resetAppConfig } from '../api/adminConfig'

const loading = ref(false)
const saving = ref(false)
const resetting = ref(false)
const loadError = ref(false)
const groups = ref([])
const draft = ref({})
const saved = ref({})
const ttl = ref(5)
const priority = ref('')

const dirtyKeys = computed(() => {
  const keys = []
  for (const group of groups.value) {
    for (const p of group.params) {
      if (draft.value[p.key] !== saved.value[p.key]) keys.push(p.key)
    }
  }
  return keys
})
const isDirty = computed(() => dirtyKeys.value.length > 0)
// 「恢复默认」的可用性取决于"库里存在被覆盖的项"，而不是"有未保存的改动"：
// 保存之后草稿与已存值相同（isDirty=false），但仍然存在自定义值，必须还能恢复。
const overriddenCount = computed(
  () => groups.value.reduce((n, g) => n + g.params.filter((p) => p.overridden).length, 0)
)

function groupHasOverride(group) {
  return group.params.some((p) => p.overridden)
}

// 单项状态：dirty = 本次改动还没保存；overridden = 库里的值已不同于默认值
function isDirtyKey(key) {
  return dirtyKeys.value.includes(key)
}

function applySnapshot(data) {
  groups.value = data?.groups || []
  ttl.value = data?.cache_ttl_seconds ?? 5
  priority.value = data?.priority || ''
  const next = {}
  for (const group of groups.value) {
    for (const p of group.params) next[p.key] = p.current
  }
  draft.value = { ...next }
  saved.value = { ...next }
}

async function load() {
  loading.value = true
  try {
    applySnapshot(await fetchAppConfig())
    loadError.value = false
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (!isDirty.value || saving.value) return
  const values = {}
  for (const key of dirtyKeys.value) values[key] = draft.value[key]
  saving.value = true
  try {
    const data = await saveAppConfig(values)
    applySnapshot(data.snapshot)
    ElMessage.success(`已保存 ${data.changed.length} 项，${ttl.value} 秒内（新请求立即）生效`)
  } catch {
    // 错误提示由统一 http 拦截器负责；保留草稿便于修改
  } finally {
    saving.value = false
  }
}

async function onReset(payload, label) {
  if (resetting.value) return
  try {
    await ElMessageBox.confirm(
      `将${label}恢复为 .env / 代码默认值，当前自定义值会被删除。确认继续？`,
      '恢复默认',
      { type: 'warning', confirmButtonText: '恢复默认', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  resetting.value = true
  try {
    const data = await resetAppConfig(payload)
    applySnapshot(data.snapshot)
    ElMessage.success('已恢复默认值')
  } catch {
    // 统一拦截器提示
  } finally {
    resetting.value = false
  }
}

function fmtDefault(param) {
  if (param.type === 'bool') return param.default ? '开启' : '关闭'
  return String(param.default)
}

function fmtRange(param) {
  if (param.type === 'enum') return param.options.map(enumLabel).join(' / ')
  if (param.type === 'str') return `≤ 500 字`
  if (param.min === null || param.min === undefined) return '—'
  return `${param.min} ~ ${param.max}`
}

// 枚举值给中文注释，避免管理员在配置页看到裸的 web/guide/contact_admin
const ENUM_LABELS = {
  web: 'web（联网检索）',
  guide: 'guide（引导用户补充描述）',
  contact_admin: 'contact_admin（提示联系管理员）',
  bailian: 'bailian（百炼）',
  bocha: 'bocha（博查）',
  tavily: 'tavily（Tavily）'
}
function enumLabel(value) {
  return ENUM_LABELS[value] || value
}

onMounted(load)
</script>

<template>
  <div class="config-page">
    <div class="page-head">
      <div class="head-text">
        <h2 class="page-title">配置中心</h2>
        <p class="page-desc">
          在线调整检索、生成、上传与兜底参数，保存后立即刷新缓存、新请求即生效（无需重启）。
          取值优先级：{{ priority }}；密钥类参数（API Key / 数据库连接串）不在本页维护。
        </p>
      </div>
      <div class="head-actions">
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button :disabled="!overriddenCount || saving || resetting" @click="onReset({}, '全部参数')">
          全部恢复默认{{ overriddenCount ? `（${overriddenCount}）` : '' }}
        </el-button>
        <el-button type="primary" :loading="saving" :disabled="!isDirty" @click="onSave">
          保存{{ isDirty ? `（${dirtyKeys.length}）` : '' }}
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      title="配置加载失败，请确认后端与数据库可用后点「刷新」重试。"
      class="tip"
    />

    <!-- 逐行已有「未保存」标签 + 原值提示，这里不再重复一条全局提示条 -->

    <div v-for="group in groups" :key="group.key" class="group-card">
      <div class="group-head">
        <div class="group-title">{{ group.label }}</div>
        <el-button
          link
          type="primary"
          size="small"
          :disabled="!groupHasOverride(group) || resetting"
          @click="onReset({ group: group.key }, `${group.label}组`)"
        >
          恢复该组默认
        </el-button>
      </div>

      <div
        v-for="p in group.params"
        :key="p.key"
        class="param-row"
        :class="{ dirty: isDirtyKey(p.key), custom: p.overridden }"
      >
        <div class="param-info">
          <div class="param-label">
            {{ p.label }}
            <el-tag v-if="isDirtyKey(p.key)" size="small" type="danger" effect="dark">未保存</el-tag>
            <el-tag v-if="p.overridden" size="small" type="warning" effect="plain">已自定义</el-tag>
          </div>
          <div class="param-desc">{{ p.desc }}</div>
          <div class="param-meta">
            默认 {{ fmtDefault(p) }} · 范围 {{ fmtRange(p) }} · {{ p.effect }}
          </div>
        </div>

        <div class="param-control" :class="{ wide: p.type === 'str' }">
          <el-switch v-if="p.type === 'bool'" v-model="draft[p.key]" />
          <el-select v-else-if="p.type === 'enum'" v-model="draft[p.key]" class="ctl">
            <el-option v-for="opt in p.options" :key="opt" :label="enumLabel(opt)" :value="opt" />
          </el-select>
          <el-input
            v-else-if="p.type === 'str'"
            v-model="draft[p.key]"
            class="ctl ctl-text"
            type="textarea"
            :rows="3"
            resize="vertical"
            maxlength="500"
            show-word-limit
          />
          <el-input-number
            v-else
            v-model="draft[p.key]"
            class="ctl"
            :min="p.min"
            :max="p.max"
            :step="p.type === 'float' ? 0.05 : 1"
            :precision="p.type === 'float' ? 2 : 0"
            controls-position="right"
          />
          <div class="param-key">{{ p.key }}</div>
          <div v-if="isDirtyKey(p.key)" class="dirty-note">原值：{{ saved[p.key] }}</div>
        </div>
      </div>
    </div>

    <div v-if="!loading && !groups.length && !loadError" class="empty-tip">暂无可配置参数</div>
  </div>
</template>

<style scoped>
.config-page {
  max-width: 980px;
  margin: 0 auto;
  padding: 4px 4px 24px;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.page-desc {
  margin: 6px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  max-width: 640px;
}

.head-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.tip {
  margin: 14px 0 0;
}

.group-card {
  margin-top: 16px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 60%, transparent);
}

.group-title {
  font-size: 14px;
  font-weight: 600;
}

.param-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 16px;
  border-bottom: 1px dashed var(--border);
}

.param-row:last-child {
  border-bottom: none;
}

/* 标黄提示：本次改动未保存（黄色实边框）；已自定义（浅黄底），一眼区分"和默认不一样" */
.param-row.custom {
  background: color-mix(in srgb, var(--warning) 8%, transparent);
}

.param-row.dirty {
  background: color-mix(in srgb, var(--warning) 16%, transparent);
  border-left: 3px solid var(--warning);
  padding-left: 13px;
}

.param-row.dirty .param-key {
  color: var(--warning);
}

.dirty-note {
  margin-top: 4px;
  font-size: 11px;
  color: var(--warning);
}

.param-info {
  min-width: 0;
  flex: 1;
}

.param-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 500;
}

.param-desc {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.param-meta {
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-muted);
}

.param-control {
  width: 200px;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
}

.ctl {
  width: 160px;
}

/* 长文本参数（兜底话术）用文本域，需要更宽的控件区，否则内容被截断、字数提示挤在角上 */
.param-control.wide {
  width: 420px;
}

.ctl-text {
  width: 100%;
}

.ctl-text :deep(.el-textarea__inner) {
  font-size: 12px;
  line-height: 1.5;
}

.param-key {
  font-size: 11px;
  color: var(--text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.empty-tip {
  margin-top: 24px;
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
}

@media (max-width: 720px) {
  .param-row {
    flex-direction: column;
    gap: 10px;
  }

  .param-control {
    width: 100%;
    align-items: flex-start;
  }

  .param-key {
    text-align: left;
  }
}
</style>
