<script setup>
// 空会话引导：品牌说明 + 示例问题；点击卡片直接发送。
import { AGENT_NAME, AGENT_TAGLINE, AGENT_AVATAR, EXAMPLE_QUESTIONS } from '../config/brand.js'

const props = defineProps({
  questions: { type: Array, default: () => EXAMPLE_QUESTIONS }
})

defineEmits(['send'])
</script>

<template>
  <div class="empty-state">
    <img class="empty-avatar" :src="AGENT_AVATAR" :alt="`${AGENT_NAME} 头像`" />
    <h2 class="empty-title">我是 {{ AGENT_NAME }}</h2>
    <p class="empty-desc">{{ AGENT_TAGLINE }}，可以基于企业知识库回答问题、做简单计算和文档统计。</p>

    <div class="example-grid">
      <button
        v-for="question in props.questions"
        :key="question"
        type="button"
        class="example-card"
        :aria-label="`发送示例问题：${question}`"
        @click="$emit('send', question)"
      >
        {{ question }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 420px;
  padding: 24px;
  text-align: center;
}

.empty-avatar {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-lg);
  margin-bottom: 16px;
}

.empty-title {
  margin: 0 0 8px;
  font-size: 22px;
  color: var(--text);
}

.empty-desc {
  margin: 0 0 24px;
  max-width: 520px;
  font-size: 14px;
  line-height: 1.7;
  color: var(--text-muted);
}

.example-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(220px, 1fr));
  gap: 12px;
  width: min(640px, 100%);
}

.example-card {
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  text-align: left;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text);
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
}

.example-card:hover,
.example-card:focus-visible {
  transform: translateY(-1px);
  border-color: var(--primary);
  box-shadow: var(--shadow-card);
}

@media (max-width: 768px) {
  .example-grid {
    grid-template-columns: 1fr;
  }
}
</style>
