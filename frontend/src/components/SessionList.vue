<script setup>
// 会话列表（P-02 / F-10）：新建 / 切换 / 删除；按创建时间倒序。
defineProps({ sessions: { type: Array, default: () => [] }, currentId: { type: [Number, null], default: null } })
defineEmits(['new', 'select', 'remove'])
</script>

<template>
  <el-card class="session-card">
    <template #header>
      <div class="head">
        <span>会话列表</span>
        <el-button size="small" type="primary" @click="$emit('new')">新建</el-button>
      </div>
    </template>
    <el-empty v-if="!sessions.length" description="暂无会话" :image-size="60" />
    <ul class="session-list">
      <li
        v-for="s in sessions"
        :key="s.session_id"
        :class="{ active: s.session_id === currentId }"
        @click="$emit('select', s.session_id)"
      >
        <span class="name">{{ s.session_name }}</span>
        <el-button link size="small" type="danger" @click.stop="$emit('remove', s.session_id)">删除</el-button>
      </li>
    </ul>
  </el-card>
</template>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
.session-list { list-style: none; margin: 0; padding: 0; }
.session-list li { display: flex; justify-content: space-between; padding: 8px 10px; border-radius: 6px; cursor: pointer; }
.session-list li.active { background: #ecf5ff; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 140px; }
</style>
