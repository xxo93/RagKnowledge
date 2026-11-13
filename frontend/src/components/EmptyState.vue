<script setup>
// 空会话引导：品牌说明 + 示例问题分三组（查资料 / 写代码 / 做任务），点击卡片直接发送。
import { computed } from 'vue'
import { AGENT_NAME, AGENT_TAGLINE, AGENT_AVATAR, EXAMPLE_QUESTIONS } from '../config/brand.js'

const props = defineProps({
  questions: { type: Array, default: () => EXAMPLE_QUESTIONS }
})

defineEmits(['send'])

const GROUPS = computed(() => {
  const knowledge = (props.questions || []).slice(0, 3)
  return [
    {
      key: 'knowledge',
      title: '查资料',
      desc: '基于企业内部知识库回答，带引用来源',
      items: knowledge.length ? knowledge : EXAMPLE_QUESTIONS
    },
    {
      key: 'code',
      title: '写代码',
      desc: '通用能力直答，代码完整可运行并标注语言',
      items: [
        '用 Python 写一个快速排序，并说明时间复杂度',
        '写一段 SQL，查询近 7 天每天的订单量'
      ]
    },
    {
      key: 'task',
      title: '做任务',
      desc: '统计、计算等确定性工具任务，可复算可追溯',
      items: [
        '帮我统计知识库现在一共有多少份文档',
        '1234 × 56 等于多少？'
      ]
    }
  ]
})
</script>

<template>
  <div class="empty-state">
    <img class="empty-avatar" :src="AGENT_AVATAR" :alt="`${AGENT_NAME} 头像`" />
    <h2 class="empty-title">我是 {{ AGENT_NAME }}</h2>
    <p class="empty-desc">{{ AGENT_TAGLINE }}，可以查企业知识库、写代码、做计算与文档统计。</p>

    <div class="example-groups">
      <section v-for="group in GROUPS" :key="group.key" class="example-group">
        <div class="group-head">
          <span class="group-title">{{ group.title }}</span>
          <span class="group-desc">{{ group.desc }}</span>
        </div>
        <div class="example-grid">
          <button
            v-for="question in group.items"
            :key="question"
            type="button"
            class="example-card"
            :aria-label="`发送示例问题：${question}`"
            @click="$emit('send', question)"
          >
            {{ question }}
          </button>
        </div>
      </section>
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
  margin: 0 0 20px;
  max-width: 520px;
  font-size: 14px;
  line-height: 1.7;
  color: var(--text-muted);
}

.example-groups {
  display: flex;
  flex-direction: column;
  gap: 18px;
  width: min(720px, 100%);
}

.example-group {
  text-align: left;
}

.group-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}

.group-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.group-desc {
  font-size: 11px;
  color: var(--text-muted);
}

.example-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.example-card {
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: var(--radius-md);
  padding: 12px 14px;
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

@media (max-width: 900px) {
  .example-grid {
    grid-template-columns: 1fr;
  }
}
</style>
