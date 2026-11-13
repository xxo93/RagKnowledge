<script setup>
// 会话列表：本地标题搜索、时间分组、消息数/相对时间、悬停操作与删除二次确认。
// 会话归属与删除权限仍由后端强制校验；本组件只做已加载列表的前端体验。
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useVirtualizer } from '@tanstack/vue-virtual'
import SessionRowContent from './SessionRowContent.vue'

const props = defineProps({
  sessions: { type: Array, default: () => [] },
  currentId: { type: [Number, null], default: null },
  loading: { type: Boolean, default: false },
  pendingTitleId: { type: [Number, null], default: null },
  total: { type: [Number, null], default: null },
  loadingMore: { type: Boolean, default: false },
  keyword: { type: String, default: '' },
  // M8 / WP5-9：归档区按需加载；置顶项独立分组
  archivedSessions: { type: Array, default: () => [] },
  archivedTotal: { type: Number, default: 0 },
  archivedLoading: { type: Boolean, default: false },
  archivedVisible: { type: Boolean, default: false }
})
const emit = defineEmits([
  'new', 'select', 'rename', 'remove', 'loadMore', 'search',
  'pin', 'archive', 'restore', 'toggleArchived', 'batch'
])

// v4 / N6：批量多选模式（不含删除）；选择状态只在前端，提交后由父组件刷新列表
const batchMode = ref(false)
const selectedIds = ref(new Set())
const selectedCount = computed(() => selectedIds.value.size)

function clearSelection() {
  selectedIds.value = new Set()
}

function toggleBatchMode() {
  batchMode.value = !batchMode.value
  selectedIds.value = new Set()
}

function isSelected(id) {
  return selectedIds.value.has(id)
}

function toggleSelect(id) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else if (next.size < 50) next.add(id)
  selectedIds.value = next
}

function selectAllLoaded() {
  const ids = props.sessions.map((s) => s.session_id)
  if (props.archivedVisible) ids.push(...props.archivedSessions.map((s) => s.session_id))
  selectedIds.value = new Set(ids.slice(0, 50))
}

function onRowClick(session) {
  if (batchMode.value) {
    toggleSelect(session.session_id)
    return
  }
  emit('select', session.session_id)
}

function submitBatch(action) {
  if (!selectedIds.value.size) return
  const ids = [...selectedIds.value]
  emit('batch', { action, ids })
  batchMode.value = false
  selectedIds.value = new Set()
}

function parseTime(value) {
  if (!value) return null
  const d = new Date(String(value).replace(' ', 'T'))
  return Number.isNaN(d.getTime()) ? null : d
}

const TWELVE_HOURS = 12 * 60 * 60 * 1000

function pad2(n) {
  return String(n).padStart(2, '0')
}

function fullTime(d) {
  if (!d) return '—'
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

function relTime(value) {
  const d = parseTime(value)
  if (!d) return '—'
  const diff = Date.now() - d.getTime()
  // 超过 12 小时直接显示完整年月日时分秒，避免“13 小时前/某天”跨天歧义
  if (diff > TWELVE_HOURS) return fullTime(d)
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时前`
}

function timeTitle(s) {
  const created = fullTime(parseTime(s.create_time))
  const active = fullTime(parseTime(activityTime(s)))
  return `创建时间：${created}\n最近活跃：${active}`
}

function groupKey(value) {
  const d = parseTime(value)
  if (!d) return 'earlier'
  const now = new Date()
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  if (d >= startToday) return 'today'
  const startWeek = new Date(startToday.getTime() - 6 * 24 * 3600 * 1000)
  if (d >= startWeek) return 'week'
  return 'earlier'
}

const GROUP_LABELS = { today: '今天', week: '近 7 天', earlier: '更早' }

function activityTime(s) {
  return s.last_active_at || s.create_time
}

const pinnedSessions = computed(() => props.sessions.filter((s) => s.is_pinned))

const groups = computed(() => {
  const map = { today: [], week: [], earlier: [] }
  props.sessions
    .filter((s) => !s.is_pinned)
    .forEach((s) => map[groupKey(activityTime(s))].push(s))
  return Object.entries(map)
    .filter(([, items]) => items.length)
    .map(([key, items]) => ({ key, label: GROUP_LABELS[key], items }))
})

const canLoadMore = computed(() => props.total != null && props.sessions.length < props.total)

// v4 / N1：只虚拟化非置顶普通会话；置顶、归档保持真实 DOM。
// 40 条以下保持原列表 DOM，便于小数据量下的调试与旧行为回归。
const scrollRef = ref(null)
const VIRTUAL_THRESHOLD = 40
const virtualDisabled = ref(readVirtualDisabled())

function readVirtualDisabled() {
  try {
    return window.localStorage.getItem('ragknowledge:session-virtual') === 'off'
  } catch {
    return false
  }
}

function toggleVirtual(disabled) {
  virtualDisabled.value = disabled
  try {
    window.localStorage.setItem('ragknowledge:session-virtual', disabled ? 'off' : 'on')
  } catch {
    // 忽略存储异常，仅当前会话生效
  }
}

const normalRows = computed(() => {
  const rows = []
  groups.value.forEach((group) => {
    rows.push({ type: 'header', key: `head-${group.key}`, label: group.label })
    group.items.forEach((s) => {
      rows.push({
        type: 'session',
        key: `session-${s.session_id}`,
        session: s,
        timeText: relTime(activityTime(s)),
        timeTooltip: timeTitle(s)
      })
    })
  })
  return rows
})

const virtualEnabled = computed(
  () => !virtualDisabled.value && normalRows.value.length >= VIRTUAL_THRESHOLD
)

const virtualizer = useVirtualizer(computed(() => ({
  count: virtualEnabled.value ? normalRows.value.length : 0,
  getScrollElement: () => scrollRef.value,
  estimateSize: (index) => (normalRows.value[index]?.type === 'header' ? 28 : 66),
  overscan: 8
})))

const virtualItems = computed(() => virtualizer.value.getVirtualItems())
const virtualTotalSize = computed(() => virtualizer.value.getTotalSize())

// v4 / N7：输入框本地即时回显，300ms 防抖后只发一次请求；清空立即恢复。
const searchText = ref((props.keyword || '').trim())
let searchTimer = null
let preSearchScrollTop = 0

function onSearchInput(value) {
  const next = String(value ?? '')
  searchText.value = next
  if (searchTimer) clearTimeout(searchTimer)
  if (!next.trim()) {
    searchTimer = null
    emit('search', '')
    return
  }
  searchTimer = setTimeout(() => {
    searchTimer = null
    emit('search', next.trim())
  }, 300)
}

watch(
  () => props.keyword,
  (value) => {
    const next = (value || '').trim()
    if (next === searchText.value.trim()) return
    searchText.value = next
  }
)

watch(
  () => props.keyword,
  (next, prev) => {
    const now = (next || '').trim()
    const before = (prev || '').trim()
    nextTick(() => {
      const el = scrollRef.value
      if (!el) return
      if (!now && before) {
        el.scrollTop = preSearchScrollTop
        return
      }
      if (!before) preSearchScrollTop = el.scrollTop || 0
      el.scrollTop = 0
      virtualizer.value?.scrollToOffset?.(0)
    })
  }
)

function onSessionKeydown(event, session) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    emit('select', session.session_id)
  }
}

async function onRemove(session) {
  try {
    await ElMessageBox.confirm(
      `确认删除会话“${session.session_name}”？该会话及其历史记录不可恢复。`,
      '删除会话确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch (_) {
    return
  }
  emit('remove', session.session_id)
}
</script>

<template>
  <el-card class="session-card" :class="{ 'batch-mode': batchMode }">
    <template #header>
      <div class="head">
        <span>会话列表</span>
        <div class="head-btns">
          <el-button
            size="small"
            :type="batchMode ? 'warning' : 'default'"
            @click="toggleBatchMode"
          >{{ batchMode ? '退出批量' : '批量' }}</el-button>
          <el-button size="small" type="primary" @click="emit('new')">新建</el-button>
        </div>
      </div>
      <el-input
        :model-value="searchText"
        size="small"
        clearable
        placeholder="搜索全部会话"
        aria-label="搜索会话标题"
        class="search-input"
        @update:model-value="onSearchInput"
      />
    </template>

    <div v-if="loading" class="skeleton-list" aria-label="会话列表加载中">
      <div v-for="n in 3" :key="n" class="skeleton-item">
        <div class="skeleton-line title"></div>
        <div class="skeleton-line meta"></div>
      </div>
    </div>
    <!-- 空态文案区分“本来没有会话”与“搜索未命中”，后者要带上关键词，避免误以为会话丢了 -->
    <div v-else class="session-body">
      <div v-if="pinnedSessions.length" class="session-group pinned-group session-pinned">
        <div class="group-title">
          <span class="group-pin">置顶</span>
        </div>
        <ul class="session-list">
          <li
            v-for="s in pinnedSessions"
            :key="`pin-${s.session_id}`"
            :class="{ active: s.session_id === currentId, pinned: true }"
            role="button"
            tabindex="0"
            :aria-label="`选择置顶会话 ${s.session_name}`"
            @click="onRowClick(s)"
            @keydown="onSessionKeydown($event, s)"
          >
            <el-checkbox
              v-if="batchMode"
              class="batch-check"
              :model-value="isSelected(s.session_id)"
              :aria-label="`选择会话 ${s.session_name}`"
              @click.stop
              @change="toggleSelect(s.session_id)"
            />
            <SessionRowContent
              :session="s"
              :pending-title-id="pendingTitleId"
              :highlight="searchText"
              variant="pinned"
              :time-text="relTime(activityTime(s))"
              :time-tooltip="timeTitle(s)"
              @pin="(session, pinned) => emit('pin', session, pinned)"
              @archive="(session) => emit('archive', session)"
              @rename="(session) => emit('rename', session)"
              @remove="onRemove"
            />
          </li>
        </ul>
      </div>

      <div ref="scrollRef" class="session-scroll">
        <el-empty
          v-if="!sessions.length"
          :description="props.keyword ? `没有匹配“${props.keyword}”的会话` : '暂无会话'"
          :image-size="60"
        />

        <!-- 200+ 会话：普通会话区虚拟滚动，分组标题也作为虚拟行参与定位 -->
        <ul
          v-if="virtualEnabled"
          class="session-list virtual-list"
          :style="{ height: `${virtualTotalSize}px` }"
          aria-label="普通会话虚拟列表"
        >
          <template v-for="vi in virtualItems" :key="normalRows[vi.index].key">
            <li
              v-if="normalRows[vi.index].type === 'header'"
              class="virtual-row virtual-header"
              :style="{ transform: `translateY(${vi.start}px)`, height: `${vi.size}px` }"
            >
              <div class="group-title">{{ normalRows[vi.index].label }}</div>
            </li>
            <li
              v-else
              class="virtual-row"
              :class="{ active: normalRows[vi.index].session.session_id === currentId }"
              role="button"
              tabindex="0"
              :aria-label="`选择会话 ${normalRows[vi.index].session.session_name}`"
              :style="{ transform: `translateY(${vi.start}px)`, height: `${vi.size}px` }"
              @click="onRowClick(normalRows[vi.index].session)"
              @keydown="onSessionKeydown($event, normalRows[vi.index].session)"
            >
              <el-checkbox
                v-if="batchMode"
                class="batch-check"
                :model-value="isSelected(normalRows[vi.index].session.session_id)"
                :aria-label="`选择会话 ${normalRows[vi.index].session.session_name}`"
                @click.stop
                @change="toggleSelect(normalRows[vi.index].session.session_id)"
              />
              <SessionRowContent
                :session="normalRows[vi.index].session"
                :pending-title-id="pendingTitleId"
              :highlight="searchText"
                :time-text="normalRows[vi.index].timeText"
                :time-tooltip="normalRows[vi.index].timeTooltip"
                @pin="(session, pinned) => emit('pin', session, pinned)"
                @archive="(session) => emit('archive', session)"
                @rename="(session) => emit('rename', session)"
                @remove="onRemove"
              />
            </li>
          </template>
        </ul>

        <template v-else>
          <div v-for="group in groups" :key="group.key" class="session-group">
            <div class="group-title">{{ group.label }}</div>
            <ul class="session-list">
              <li
                v-for="s in group.items"
                :key="s.session_id"
                :class="{ active: s.session_id === currentId }"
                role="button"
                tabindex="0"
                :aria-label="`选择会话 ${s.session_name}`"
                @click="onRowClick(s)"
                @keydown="onSessionKeydown($event, s)"
              >
                <el-checkbox
                  v-if="batchMode"
                  class="batch-check"
                  :model-value="isSelected(s.session_id)"
                  :aria-label="`选择会话 ${s.session_name}`"
                  @click.stop
                  @change="toggleSelect(s.session_id)"
                />
                <SessionRowContent
                  :session="s"
                  :pending-title-id="pendingTitleId"
              :highlight="searchText"
                  :time-text="relTime(activityTime(s))"
                  :time-tooltip="timeTitle(s)"
                  @pin="(session, pinned) => emit('pin', session, pinned)"
                  @archive="(session) => emit('archive', session)"
                  @rename="(session) => emit('rename', session)"
                  @remove="onRemove"
                />
              </li>
            </ul>
          </div>
        </template>

        <div v-if="archivedTotal > 0 || archivedVisible" class="archive-block">
          <button
            type="button"
            class="archive-toggle"
            :aria-expanded="archivedVisible"
            @click="emit('toggleArchived', !archivedVisible)"
          >
            <span class="archive-caret">{{ archivedVisible ? '▾' : '▸' }}</span>
            已归档 {{ archivedTotal }}
            <span v-if="archivedLoading" class="archive-loading">加载中…</span>
          </button>
          <div v-if="archivedVisible" class="archive-body">
            <div v-if="archivedLoading && !archivedSessions.length" class="skeleton-list">
              <div v-for="n in 2" :key="n" class="skeleton-item">
                <div class="skeleton-line title"></div>
                <div class="skeleton-line meta"></div>
              </div>
            </div>
            <div v-else-if="!archivedSessions.length" class="archive-empty">没有已归档会话</div>
            <ul v-else class="session-list archived-list">
              <li
                v-for="s in archivedSessions"
                :key="`arch-${s.session_id}`"
                :class="{ active: s.session_id === currentId }"
                role="button"
                tabindex="0"
                :aria-label="`恢复已归档会话 ${s.session_name}`"
                @click="batchMode ? toggleSelect(s.session_id) : emit('restore', s)"
                @keydown="onSessionKeydown($event, s)"
              >
                <el-checkbox
                  v-if="batchMode"
                  class="batch-check"
                  :model-value="isSelected(s.session_id)"
                  :aria-label="`选择已归档会话 ${s.session_name}`"
                  @click.stop
                  @change="toggleSelect(s.session_id)"
                />
                <div class="session-main">
                  <span class="name" :title="s.session_name">{{ s.session_name }}</span>
                  <span class="meta">
                    <span v-if="s.record_count != null">{{ s.record_count }} 条</span>
                    <span :title="`归档时间：${fullTime(parseTime(s.archived_at))}\n创建时间：${fullTime(parseTime(s.create_time))}`">归档于 {{ relTime(s.archived_at) }}</span>
                  </span>
                </div>
                <span class="ops archived-ops">
                  <el-button
                    link
                    size="small"
                    :aria-label="`恢复会话 ${s.session_name}`"
                    @click.stop="emit('restore', s)"
                  >恢复</el-button>
                  <el-button
                    link
                    size="small"
                    type="danger"
                    :aria-label="`删除会话 ${s.session_name}`"
                    @click.stop="onRemove(s)"
                  >🗑</el-button>
                </span>
              </li>
            </ul>
          </div>
        </div>
        <div v-if="canLoadMore" class="more-tip">
          <el-button link :loading="loadingMore" :disabled="loadingMore" @click="emit('loadMore')">
            加载更多会话
          </el-button>
        </div>
      </div>
    </div>

    <div v-if="batchMode" class="batch-bar">
      <div class="batch-info">
        已选 {{ selectedCount }} / 50
        <el-button link size="small" @click="selectAllLoaded">全选已加载</el-button>
        <el-button link size="small" @click="clearSelection">取消选择</el-button>
      </div>
      <div class="batch-actions">
        <el-button size="small" :disabled="!selectedCount" @click="submitBatch('pin')">置顶</el-button>
        <el-button size="small" :disabled="!selectedCount" @click="submitBatch('unpin')">取消置顶</el-button>
        <el-button size="small" type="warning" :disabled="!selectedCount" @click="submitBatch('archive')">归档</el-button>
        <el-button size="small" type="success" :disabled="!selectedCount" @click="submitBatch('restore')">恢复</el-button>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.head-btns {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.batch-check {
  flex: 0 0 auto;
  margin-right: 2px;
}

.batch-bar {
  flex: 0 0 auto;
  border-top: 1px solid var(--border);
  background: var(--panel);
  padding: 6px 2px 0;
}

.batch-info {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px 4px;
  font-size: 12px;
  color: var(--text-secondary);
}

.batch-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.batch-actions .el-button {
  margin-left: 0;
}

.session-card.batch-mode .ops {
  display: none !important;
}

.session-card.batch-mode .session-list li {
  cursor: default;
}

.search-input {
  margin-top: 10px;
}

.session-group {
  margin-bottom: 10px;
}

/* 会话很多时列表自己滚动，header（新建/搜索）保持固定，不把整页撑出可视区 */
.session-card {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.session-card :deep(.el-card__body) {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.session-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 置顶固定在上方，普通会话与大列表在下方独立滚动，避免虚拟列表受 scrollMargin 影响 */
.session-pinned {
  flex: 0 0 auto;
  margin-bottom: 4px;
}

.session-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}

.virtual-list {
  position: relative;
  margin: 0;
}

.virtual-list li.virtual-row {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  box-sizing: border-box;
}

.virtual-list li.virtual-header {
  display: flex;
  align-items: center;
  padding: 0;
  cursor: default;
  pointer-events: none;
}

.virtual-list li.virtual-header:hover {
  background: transparent;
  box-shadow: none;
}

.virtual-list li.virtual-header:hover::before {
  display: none;
}

.group-title {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 6px 4px;
}

.session-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.session-list li {
  position: relative;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background-color 0.15s ease, box-shadow 0.15s ease;
}

/* 悬停跟随鼠标：当前指针所在的那一条会实时高亮 + 左侧竖条，便于定位 */
.session-list li:hover {
  background: color-mix(in srgb, var(--primary) 7%, var(--panel));
  box-shadow: inset 0 0 0 1px var(--border);
}

.session-list li:hover::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--primary) 45%, transparent);
}

.session-list li.active {
  background: var(--primary-light);
}

.session-list li.active:hover {
  background: var(--primary-light);
  box-shadow: inset 0 0 0 1px var(--primary);
}

.session-list li.active:hover::before {
  background: var(--primary);
}

.session-list li.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 3px;
  background: var(--primary);
}

.session-main {
  min-width: 0;
  flex: 1;
}

.name {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 18px;
  max-height: 36px;
  font-size: 13px;
  color: var(--text);
}

.meta {
  display: flex;
  gap: 8px;
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-secondary);
}

.ops {
  display: none;
  align-items: center;
  gap: 2px;
  white-space: nowrap;
}

.session-list li:hover .ops,
.session-list li:focus-within .ops {
  display: flex;
}

/* 子组件 scoped 样式无法被父级选择器穿透，这里补可见性/置顶加粗 */
.session-list li:focus-within :deep(.ops) {
  display: flex;
}

.session-list li.pinned :deep(.name) {
  font-weight: 600;
}

.empty-filter,
.more-tip {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.name.pending {
  color: var(--text-secondary);
}

.pinned-group .group-title {
  display: flex;
  align-items: center;
  gap: 4px;
}

.group-pin {
  color: var(--primary);
  font-weight: 600;
}

.session-list li.pinned .name {
  font-weight: 600;
}

.archive-block {
  margin-top: 6px;
  border-top: 1px dashed var(--border);
  padding-top: 6px;
}

.archive-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 4px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}

.archive-toggle:hover {
  color: var(--text);
}

.archive-caret {
  font-size: 11px;
}

.archive-loading {
  font-size: 11px;
  color: var(--text-muted);
}

.archive-body {
  padding-left: 2px;
}

.archive-empty {
  padding: 6px;
  font-size: 12px;
  color: var(--text-muted);
}

.archived-list li .name {
  color: var(--text-secondary);
}

.archived-ops .el-button {
  font-size: 12px;
}

.skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 2px;
}

.skeleton-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.skeleton-line {
  border-radius: var(--radius-sm);
  background: var(--border);
  opacity: 0.45;
}

.skeleton-line.title {
  height: 16px;
  width: 82%;
}

.skeleton-line.meta {
  height: 12px;
  width: 42%;
}

@media (prefers-reduced-motion: reduce) {
  .session-list li {
    transition: none;
  }
}
</style>
