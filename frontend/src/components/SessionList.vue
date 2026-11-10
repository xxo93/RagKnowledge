<script setup>
// 会话列表：新建、切换、重命名与删除；会话归属由后端强制校验
defineProps({ sessions: { type: Array, default: () => [] }, currentId: { type: [Number, null], default: null } })
defineEmits(['new', 'select', 'rename', 'remove'])
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
        <span class="name" :title="s.session_name">{{ s.session_name }}</span>
        <span class="ops">
          <el-button link size="small" @click.stop="$emit('rename', s)">重命名</el-button>
          <el-button link size="small" type="danger" @click.stop="$emit('remove', s.session_id)">删除</el-button>
        </span>
      </li>
    </ul>
  </el-card>
</template>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
.session-list { list-style: none; margin: 0; padding: 0; }
.session-list li { display: flex; justify-content: space-between; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 6px; cursor: pointer; }
.session-list li.active { background: #ecf5ff; }
.name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 140px; }
.ops { display: flex; align-items: center; white-space: nowrap; }
</style>
