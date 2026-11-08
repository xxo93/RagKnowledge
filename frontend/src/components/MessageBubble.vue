<script setup>
// 消息气泡（P-02）：用户 / 助手样式区分；助手消息上方工具标签，下方来源卡片。
import ToolChip from './ToolChip.vue'
import SourceCard from './SourceCard.vue'
defineProps({ message: { type: Object, required: true } })
</script>

<template>
  <div class="bubble-row" :class="message.role">
    <div class="bubble">
      <div v-if="message.toolCalls?.length" class="tools">
        <ToolChip v-for="(t, i) in message.toolCalls" :key="i" :tool="t" />
      </div>
      <div class="content">{{ message.content }}</div>
      <div v-if="message.sources?.length" class="sources">
        <SourceCard v-for="(s, i) in message.sources" :key="i" :source="s" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.bubble-row { display: flex; margin-bottom: 14px; }
.bubble-row.user { justify-content: flex-end; }
.bubble { max-width: 72%; padding: 10px 14px; border-radius: 10px; white-space: pre-wrap; }
.user .bubble { background: #ecf5ff; }
.assistant .bubble { background: #f4f4f5; }
.tools { margin-bottom: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.sources { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
</style>
