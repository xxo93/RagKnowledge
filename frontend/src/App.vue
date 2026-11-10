<script setup>
// 根组件：未登录显示登录页；登录后按角色展示聊天与管理员入口
// 前端隐藏普通用户不可见菜单；最终权限仍由后端强制校验
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import DocsView from './views/DocsView.vue'
import UsersView from './views/UsersView.vue'
import ChatView from './views/ChatView.vue'
import LoginView from './views/LoginView.vue'
import ChangePasswordDialog from './components/ChangePasswordDialog.vue'
import { fetchMe, logout as logoutApi } from './api/auth'
import { setErrorHandler } from './api/http'

const user = ref(null)
const checking = ref(true)
const current = ref('chat')
const pwdVisible = ref(false)

const menus = computed(() => {
  const list = [{ key: 'chat', label: '智能对话' }]
  if (user.value && user.value.role === 'admin') {
    list.push({ key: 'docs', label: '知识库管理' })
    list.push({ key: 'users', label: '用户管理' })
  }
  return list
})

function bindErrorHandler() {
  setErrorHandler((err) => {
    const unauthorized = err.bizCode === 40101 || err.httpStatus === 401
    if (unauthorized) {
      // 仅在“已登录但登录态失效”时提示并回登录页；登录页自身的错误由 LoginView 提示
      if (user.value) {
        user.value = null
        current.value = 'chat'
        pwdVisible.value = false
        ElMessage.warning('登录已过期，请重新登录')
      }
      return
    }
    const forbidden = err.bizCode === 40303 || err.httpStatus === 403
    ElMessage.error(forbidden ? '没有操作权限（仅管理员可执行）' : err.message || '请求失败')
  })
}

onMounted(async () => {
  bindErrorHandler()
  await reloadMe()
})

async function reloadMe() {
  checking.value = true
  try {
    user.value = await fetchMe()
  } catch {
    user.value = null
  } finally {
    checking.value = false
  }
}

function onLoggedIn(profile) {
  user.value = profile
  current.value = 'chat'
}

async function onLogout() {
  try {
    await logoutApi()
  } catch {
    // 本地状态仍需清理，避免停留在已登录界面
  }
  user.value = null
  current.value = 'chat'
  pwdVisible.value = false
}

function onSelect(key) {
  current.value = key
}
</script>

<template>
  <div v-if="checking" class="checking-tip">正在校验登录状态…</div>
  <LoginView v-else-if="!user" @success="onLoggedIn" />

  <el-container v-else class="app-shell">
    <el-header class="app-header">
      <div class="brand">RagKnowledge · 企业文档智能问答 Agent</div>
      <el-menu
        :default-active="current"
        mode="horizontal"
        class="nav"
        @select="onSelect"
      >
        <el-menu-item v-for="m in menus" :key="m.key" :index="m.key">
          {{ m.label }}
        </el-menu-item>
      </el-menu>
      <div class="user-box">
        <el-tag size="small" type="info" effect="plain">{{ user.role === 'admin' ? '管理员' : '普通用户' }}</el-tag>
        <span class="user-name">{{ user.display_name || user.username }}</span>
        <el-button size="small" plain @click="pwdVisible = true">修改密码</el-button>
        <el-button size="small" plain @click="onLogout">退出登录</el-button>
      </div>
    </el-header>

    <el-alert
      type="info"
      :closable="false"
      title="企业文档智能问答（RAG + Agent）：知识库全局共享；会话按用户隔离；文档维护、索引重建与账号管理仅限管理员。"
      class="stage-tip"
    />

    <el-main class="app-main">
      <UsersView v-if="current === 'users' && user.role === 'admin'" :current-user-id="user.user_id" />
      <DocsView v-else-if="current === 'docs' && user.role === 'admin'" />
      <ChatView v-else />
    </el-main>

    <ChangePasswordDialog v-model:visible="pwdVisible" :username="user.username" />
  </el-container>
</template>

<style scoped>
.app-shell { min-height: 100vh; }
.app-header { display: flex; align-items: center; gap: 24px; border-bottom: 1px solid #eee; }
.brand { font-weight: 600; white-space: nowrap; }
.nav { flex: 1; border-bottom: none; }
.user-box { display: flex; align-items: center; gap: 10px; }
.user-name { color: #606266; font-size: 13px; }
.stage-tip { margin: 12px 16px 0; }
.app-main { background: #f7f8fa; }
.checking-tip { min-height: 100vh; display: flex; align-items: center; justify-content: center; color: #909399; }
</style>
