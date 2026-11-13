<script setup>
// 管理员配置中心（M1）：检索组 + 生成组参数在线调整，保存后热生效
// 仅管理员可见；后端 require_admin 强制校验，隐藏菜单不是安全边界
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchAppConfig, saveAppConfig, resetAppConfig } from '../api/adminConfig'
import {
  fetchPrompts,
  createPromptVersion,
  updatePromptGray,
  activatePromptVersion
} from '../api/adminPrompts'

const loading = ref(false)
const saving = ref(false)
const resetting = ref(false)
const loadError = ref(false)
const groups = ref([])
const draft = ref({})
const saved = ref({})
const ttl = ref(5)
const priority = ref('')

// 提示词版本化（WP5-7）：与参数配置分开展示，避免把"改提示词"当成普通参数
const prompts = ref([])
const promptLoading = ref(false)
const promptDialog = ref(false)
const promptSaving = ref(false)
const promptEditing = ref(null)
const promptDraft = ref({ content: '', gray_percent: 100 })

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
  loadPrompts()
}

async function loadPrompts() {
  promptLoading.value = true
  try {
    const data = await fetchPrompts()
    prompts.value = (data?.items || []).map((p) => ({
      ...p,
      view_version: p.current_version
    }))
  } catch {
    // 统一拦截器提示；提示词卡片保持空态，不阻断普通配置
  } finally {
    promptLoading.value = false
  }
}

async function onPromptGray(p) {
  try {
    await updatePromptGray(p.name, p.gray_percent)
    ElMessage.success(`${p.label}灰度已调整为 ${p.gray_percent}%`)
    await loadPrompts()
  } catch {
    await loadPrompts()
  }
}

async function onActivatePrompt(p) {
  const version = p.view_version
  if (!version) return
  try {
    await ElMessageBox.confirm(
      `将「${p.label}」切换到 v${version}，并默认 100% 全量生效。历史版本仍保留，可随时回滚。确认继续？`,
      '切换提示词版本',
      { type: 'warning', confirmButtonText: '立即切换', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await activatePromptVersion(p.name, version, 100)
    ElMessage.success(`已切换到 v${version}（100% 生效）`)
    await loadPrompts()
  } catch {
    await loadPrompts()
  }
}

function currentContent(p) {
  const version = p.versions?.find((v) => v.version === p.current_version)
  return version?.content || ''
}

function openNewPrompt(p) {
  promptEditing.value = p
  promptDraft.value = { content: currentContent(p), gray_percent: 100 }
  promptDialog.value = true
}

async function submitNewPrompt() {
  if (!promptEditing.value || promptSaving.value) return
  const content = promptDraft.value.content.trim()
  if (!content) {
    ElMessage.warning('提示词内容不能为空')
    return
  }
  promptSaving.value = true
  try {
    const result = await createPromptVersion(
      promptEditing.value.name,
      content,
      promptDraft.value.gray_percent
    )
    promptDialog.value = false
    ElMessage.success(`已发布 v${result.version}，灰度 ${result.gray_percent}%`)
    await loadPrompts()
  } catch {
    // 统一拦截器提示；保留弹窗内容便于修改
  } finally {
    promptSaving.value = false
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
          在线调整检索、生成、会话记忆与归档、配额限流、上传与兜底参数，保存后立即刷新缓存、新请求即生效（无需重启）。
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

    <!-- 提示词版本化（WP5-7）：独立卡片，支持灰度、发版与回滚 -->
    <div class="group-card prompt-card">
      <div class="group-head">
        <div>
          <div class="group-title">提示词版本</div>
          <div class="prompt-head-desc">
            回答/记忆链路实际使用的系统提示。灰度按用户稳定分桶，未命中灰度的用户自动回落上一版本；改错可一键切回历史版本。
          </div>
        </div>
        <el-button link type="primary" size="small" :loading="promptLoading" @click="loadPrompts">
          刷新版本
        </el-button>
      </div>

      <div v-if="promptLoading && !prompts.length" class="prompt-empty">正在读取提示词版本…</div>
      <div v-else-if="!prompts.length" class="prompt-empty">暂无提示词版本数据</div>

      <div v-for="p in prompts" :key="p.name" class="prompt-row">
        <div class="prompt-info">
          <div class="prompt-title">
            {{ p.label }}
            <el-tag size="small" effect="plain">v{{ p.current_version }}</el-tag>
            <el-tag size="small" type="success" effect="plain">当前</el-tag>
            <el-tag v-if="p.gray_percent < 100" size="small" type="warning" effect="plain">
              灰度 {{ p.gray_percent }}%
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">全量</el-tag>
          </div>
          <div class="prompt-desc">{{ p.desc }}</div>
          <div class="prompt-meta">
            {{ p.name }} · 共 {{ p.versions.length }} 个版本 · 灰度按 user_id 稳定分桶
          </div>
        </div>

        <div class="prompt-control">
          <div class="gray-line">
            <span class="gray-label">灰度</span>
            <el-slider
              v-model="p.gray_percent"
              :min="0"
              :max="100"
              :step="10"
              :show-tooltip="false"
              class="gray-slider"
              @change="onPromptGray(p)"
            />
            <span class="gray-value">{{ p.gray_percent }}%</span>
          </div>
          <div class="version-line">
            <el-select v-model="p.view_version" size="small" class="version-select">
              <el-option
                v-for="v in p.versions"
                :key="v.version"
                :label="`v${v.version}${v.is_active ? '（当前）' : ''}`"
                :value="v.version"
              />
            </el-select>
            <el-button size="small" :disabled="!p.view_version" @click="onActivatePrompt(p)">
              启用该版本
            </el-button>
            <el-button size="small" type="primary" @click="openNewPrompt(p)">发新版本</el-button>
          </div>
        </div>
      </div>
    </div>

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

    <el-dialog
      v-model="promptDialog"
      :title="`发布新版本 · ${promptEditing?.label || ''}`"
      width="760px"
      destroy-on-close
    >
      <div class="prompt-dialog-tip">
        新版本会立即成为当前版本；默认 100% 全量生效。想先小范围验证，可把灰度调到 10–50%，命中灰度的用户走新版本，其余用户继续用上一版本。
      </div>
      <el-input
        v-model="promptDraft.content"
        type="textarea"
        :rows="14"
        resize="vertical"
        maxlength="8000"
        show-word-limit
        class="prompt-textarea"
      />
      <div class="dialog-gray-line">
        <span>灰度比例</span>
        <el-slider v-model="promptDraft.gray_percent" :min="0" :max="100" :step="10" class="gray-slider" />
        <span class="gray-value">{{ promptDraft.gray_percent }}%</span>
      </div>
      <template #footer>
        <el-button @click="promptDialog = false">取消</el-button>
        <el-button type="primary" :loading="promptSaving" @click="submitNewPrompt">
          发布并设为当前版本
        </el-button>
      </template>
    </el-dialog>
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

/* 提示词版本卡片：与普通参数行区分，突出"可灰度/可回滚" */
.prompt-card .group-head {
  align-items: flex-start;
}

.prompt-head-desc {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  max-width: 680px;
}

.prompt-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 16px;
  border-bottom: 1px dashed var(--border);
}

.prompt-row:last-child {
  border-bottom: none;
}

.prompt-info {
  min-width: 0;
  flex: 1;
}

.prompt-title {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 14px;
  font-weight: 500;
}

.prompt-desc {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.prompt-meta {
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.prompt-control {
  width: 320px;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.gray-line,
.dialog-gray-line {
  display: flex;
  align-items: center;
  gap: 10px;
}

.gray-label,
.dialog-gray-line > span:first-child {
  flex: 0 0 auto;
  font-size: 12px;
  color: var(--text-secondary);
}

.gray-slider {
  flex: 1;
  min-width: 120px;
}

.gray-value {
  flex: 0 0 38px;
  text-align: right;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--text-secondary);
}

.version-line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.version-select {
  width: 130px;
}

.prompt-empty {
  padding: 24px 16px;
  text-align: center;
  font-size: 13px;
  color: var(--text-secondary);
}

.prompt-dialog-tip {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warning) 10%, transparent);
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.prompt-textarea :deep(.el-textarea__inner) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.6;
}

.dialog-gray-line {
  margin-top: 14px;
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
  .param-row,
  .prompt-row {
    flex-direction: column;
    gap: 10px;
  }

  .param-control,
  .prompt-control {
    width: 100%;
    align-items: flex-start;
  }

  .param-key {
    text-align: left;
  }

  .version-line {
    flex-wrap: wrap;
  }
}
</style>
