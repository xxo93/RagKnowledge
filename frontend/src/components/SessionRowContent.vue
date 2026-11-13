<template>
  <div class="row-content">
    <div class="session-main">
      <span
        class="name"
        :class="{ pending: session.session_id === pendingTitleId }"
        :title="session.session_id === pendingTitleId ? '自动命名中…' : session.session_name"
      >
        <template v-if="session.session_id === pendingTitleId">自动命名中…</template>
        <template v-else>
          <span v-for="(part, i) in nameParts" :key="i" :class="{ hit: part.hit }">{{ part.text }}</span>
        </template>
      </span>
      <span class="meta">
        <span v-if="session.record_count != null">{{ session.record_count }} 条</span>
        <span :title="timeTooltip">{{ timeText }}</span>
      </span>
    </div>
    <span class="ops">
      <el-button
        v-if="variant === 'pinned'"
        link
        size="small"
        :aria-label="`取消置顶会话 ${session.session_name}`"
        title="取消置顶"
        @click.stop="emit('pin', session, false)"
      >★</el-button>
      <el-button
        v-else
        link
        size="small"
        :aria-label="`置顶会话 ${session.session_name}`"
        title="置顶"
        @click.stop="emit('pin', session, true)"
      >☆</el-button>
      <el-button
        link
        size="small"
        :aria-label="`归档会话 ${session.session_name}`"
        title="归档"
        @click.stop="emit('archive', session)"
      >⇩</el-button>
      <el-button
        link
        size="small"
        :aria-label="`重命名会话 ${session.session_name}`"
        title="重命名"
        @click.stop="emit('rename', session)"
      >✎</el-button>
      <el-button
        link
        size="small"
        type="danger"
        :aria-label="`删除会话 ${session.session_name}`"
        title="删除"
        @click.stop="emit('remove', session)"
      >🗑</el-button>
    </span>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  session: { type: Object, required: true },
  pendingTitleId: { type: [Number, null], default: null },
  variant: { type: String, default: 'normal' }, // normal | pinned
  timeText: { type: String, default: '—' },
  timeTooltip: { type: String, default: '' },
  highlight: { type: String, default: '' }
})

// v4 / N7：命中关键词高亮；按片段渲染，不拼 HTML，避免标题内容被当作标签解析。
const nameParts = computed(() => {
  const name = String(props.session?.session_name || '')
  const keyword = String(props.highlight || '').trim()
  if (!keyword) return [{ text: name, hit: false }]
  const lowerName = name.toLowerCase()
  const lowerKey = keyword.toLowerCase()
  const parts = []
  let start = 0
  let index = lowerName.indexOf(lowerKey)
  while (index !== -1) {
    if (index > start) parts.push({ text: name.slice(start, index), hit: false })
    parts.push({ text: name.slice(index, index + keyword.length), hit: true })
    start = index + keyword.length
    index = lowerName.indexOf(lowerKey, start)
  }
  if (start < name.length) parts.push({ text: name.slice(start), hit: false })
  return parts.length ? parts : [{ text: name, hit: false }]
})

const emit = defineEmits(['pin', 'archive', 'rename', 'remove'])
</script>

<style scoped>
.row-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
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

.name.pending {
  color: var(--text-secondary);
}

.name .hit {
  color: #b54708;
  background: #fff4cc;
  border-radius: 2px;
  padding: 0 1px;
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

.row-content:hover .ops,
.row-content:focus-within .ops {
  display: flex;
}
</style>
