<script setup>
// 消息头像：助手使用品牌图标，用户使用姓名首字与稳定底色。
import { computed } from 'vue'
import { AGENT_AVATAR, AGENT_NAME } from '../config/brand.js'

const props = defineProps({
  role: { type: String, required: true },
  name: { type: String, default: '' },
  userId: { type: [Number, String, null], default: null },
  size: { type: Number, default: 36 }
})

const palette = ['#ecf5ff', '#f0f9eb', '#fdf6ec', '#fef0f0', '#f4f4f5', '#e6f7ff']

const displayName = computed(() => {
  if (props.role === 'assistant') return AGENT_NAME
  return props.name || '我'
})

const initial = computed(() => displayName.value.trim().charAt(0).toUpperCase() || '我')

const backgroundColor = computed(() => {
  if (props.role === 'assistant') return 'transparent'
  const source = String(props.userId || displayName.value)
  let hash = 0
  for (let i = 0; i < source.length; i += 1) hash = (hash * 31 + source.charCodeAt(i)) >>> 0
  return palette[hash % palette.length]
})

const avatarStyle = computed(() => ({
  '--avatar-size': `${props.size}px`,
  backgroundColor: backgroundColor.value
}))
</script>

<template>
  <div class="message-avatar" :class="role" :style="avatarStyle" :aria-label="displayName">
    <img v-if="role === 'assistant'" :src="AGENT_AVATAR" :alt="AGENT_NAME" />
    <span v-else>{{ initial }}</span>
  </div>
</template>

<style scoped>
.message-avatar {
  flex: none;
  width: var(--avatar-size, 36px);
  height: var(--avatar-size, 36px);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
}

.message-avatar img {
  width: 100%;
  height: 100%;
}
</style>
