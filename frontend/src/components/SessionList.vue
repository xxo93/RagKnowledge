<script setup>
// 会话列表：本地标题搜索、时间分组、消息数/相对时间、悬停操作与删除二次确认。
// 会话归属与删除权限仍由后端强制校验；本组件只做已加载列表的前端体验。
import { computed } from 'vue'
import { ElMessageBox } from 'element-plus'

const props = defineProps({
  sessions: { type: Array, default: () => [] },
  currentId: { type: [Number, null], default: null },
  loading: { type: Boolean, default: false },
  pendingTitleId: { type: [Number, null], default: null },
  total: { type: [Number, null], default: null },
  loadingMore: { type: Boolean, default: false },
  keyword: { type: String, default: '' }
})
const emit = defineEmits(['new', 'select', 'rename', 'remove', 'loadMore', 'search'])

function parseTime(value) {
  if (!value) return null
  const d = new Date(String(value).replace(' ', 'T'))
  return Number.isNaN(d.getTime()) ? null : d
}

function relTime(value) {
  const d = parseTime(value)
  if (!d) return '—'
  const diff = Date.now() - d.getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days} 天前`
  return d.toLocaleDateString('zh-CN')
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

const groups = computed(() => {
  const map = { today: [], week: [], earlier: [] }
  props.sessions.forEach((s) => map[groupKey(s.create_time)].push(s))
  return Object.entries(map)
    .filter(([, items]) => items.length)
    .map(([key, items]) => ({ key, label: GROUP_LABELS[key], items }))
})

const canLoadMore = computed(() => props.total != null && props.sessions.length < props.total)

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
  <el-card class="session-card">
    <template #header>
      <div class="head">
        <span>会话列表</span>
        <el-button size="small" type="primary" @click="emit('new')">新建</el-button>
      </div>
      <el-input
        :model-value="keyword"
        size="small"
        clearable
        placeholder="搜索全部会话"
        aria-label="搜索会话标题"
        class="search-input"
        @update:model-value="emit('search', $event)"
      />
    </template>

    <div v-if="loading" class="skeleton-list" aria-label="会话列表加载中">
      <div v-for="n in 3" :key="n" class="skeleton-item">
        <div class="skeleton-line title"></div>
        <div class="skeleton-line meta"></div>
      </div>
    </div>
    <!-- 空态文案区分“本来没有会话”与“搜索未命中”，后者要带上关键词，避免误以为会话丢了 -->
    <el-empty
      v-else-if="!sessions.length"
      :description="props.keyword ? `没有匹配“${props.keyword}”的会话` : '暂无会话'"
      :image-size="60"
    />
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
            @click="emit('select', s.session_id)"
            @keydown="onSessionKeydown($event, s)"
          >
            <div class="session-main">
              <span
                class="name"
                :class="{ pending: s.session_id === pendingTitleId }"
                :title="s.session_id === pendingTitleId ? '自动命名中…' : s.session_name"
              >{{ s.session_id === pendingTitleId ? '自动命名中…' : s.session_name }}</span>
              <span class="meta">
                <span v-if="s.record_count != null">{{ s.record_count }} 条</span>
                <span>{{ relTime(s.create_time) }}</span>
              </span>
            </div>
            <span class="ops">
              <el-button
                link
                size="small"
                :aria-label="`重命名会话 ${s.session_name}`"
                @click.stop="emit('rename', s)"
              >✎</el-button>
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
      <div v-if="canLoadMore" class="more-tip">
        <el-button link :loading="loadingMore" :disabled="loadingMore" @click="emit('loadMore')">
          加载更多会话
        </el-button>
      </div>
    </template>
  </el-card>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
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
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
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

.empty-filter,
.more-tip {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.name.pending {
  color: var(--text-secondary);
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
