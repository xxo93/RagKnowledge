<script setup>
// 根组件：未登录显示登录页；登录后按角色展示聊天与管理员入口
// 前端隐藏普通用户不可见菜单；最终权限仍由后端强制校验
import { ref, computed, onMounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import DocsView from './views/DocsView.vue'
import UsersView from './views/UsersView.vue'
import ChatView from './views/ChatView.vue'
import LoginView from './views/LoginView.vue'
import ChangePasswordDialog from './components/ChangePasswordDialog.vue'
import { fetchMe, logout as logoutApi } from './api/auth'
import { setErrorHandler } from './api/http'
import { AGENT_NAME, AGENT_TAGLINE } from './config/brand.js'
import { useTheme } from './composables/useTheme.js'
import { clearDraftsStorage } from './composables/draftStore'

const user = ref(null)
const checking = ref(true)
const current = ref('chat')
const pwdVisible = ref(false)
const pwdButtonRef = ref(null)
const AUTH_CHECK_TIMEOUT_MS = 8000
const { theme, toggleTheme, initTheme } = useTheme()

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
        clearDraftsStorage()
        ElMessage.warning('登录已过期，请重新登录')
      }
      return
    }
    const forbidden = err.bizCode === 40303 || err.httpStatus === 403
    ElMessage.error(forbidden ? '没有操作权限（仅管理员可执行）' : err.message || '请求失败')
  })
}

onMounted(async () => {
  // 主题/错误处理属于非关键初始化，即使异常也不能阻断登录态校验，
  // 否则 checking 会永远为 true，界面停留在“正在校验登录状态…”
  try {
    initTheme()
  } catch (e) {
    console.error('主题初始化失败', e)
  }
  bindErrorHandler()
  await reloadMe()
})

async function reloadMe() {
  checking.value = true
  const mePromise = fetchMe()
  mePromise.catch(() => {})
  try {
    user.value = await Promise.race([
      mePromise,
      new Promise((_, reject) => {
        setTimeout(() => reject(new Error('AUTH_CHECK_TIMEOUT')), AUTH_CHECK_TIMEOUT_MS)
      })
    ])
  } catch {
    // 登录态校验失败、超时或网络不可用时回登录页，避免一直停留在校验遮罩
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
  clearDraftsStorage()
}

function onSelect(key) {
  current.value = key
}

function openPasswordDialog() {
  pwdButtonRef.value = document.activeElement
  pwdVisible.value = true
}

async function onPasswordDialogClosed() {
  await nextTick()
  pwdButtonRef.value?.focus?.()
}
</script>

<template>
  <div v-if="checking" class="checking-tip">正在校验登录状态…</div>
  <LoginView v-else-if="!user" @success="onLoggedIn" />

  <el-container v-else class="app-shell">
    <el-header class="app-header">
      <div class="brand">{{ AGENT_NAME }} · {{ AGENT_TAGLINE }}</div>
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
        <el-button size="small" plain :aria-label="theme === 'dark' ? '切换到浅色主题' : '切换到暗色主题'" @click="toggleTheme">
          {{ theme === 'dark' ? '浅色' : '暗色' }}
        </el-button>
        <el-button ref="pwdButtonRef" size="small" plain @click="openPasswordDialog">修改密码</el-button>
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
      <ChatView v-else :current-user="user" />
    </el-main>

    <ChangePasswordDialog v-model:visible="pwdVisible" :username="user.username" @closed="onPasswordDialogClosed" />
  </el-container>
</template>

<style scoped>
.app-shell { min-height: 100vh; }
.app-header {
  display: flex;
  align-items: center;
  gap: 24px;
  border-bottom: 1px solid var(--border);
  /* Element Plus 的 el-header 默认固定 60px 高；窄屏换行后内容会被裁切并被主体遮挡，
     因此这里改为自适应高度并保留最小高度 */
  height: auto;
  min-height: 60px;
  padding: 8px 20px;
}
.brand { font-weight: 600; white-space: nowrap; }
.nav { flex: 1; border-bottom: none; }
.user-box { display: flex; align-items: center; gap: 10px; }
.user-name { color: var(--text-muted); font-size: 13px; }
.stage-tip { margin: 12px 16px 0; }
.app-main { background: var(--bg); }
.checking-tip { min-height: 100vh; display: flex; align-items: center; justify-content: center; color: var(--text-secondary); }

@media (max-width: 992px) {
  .app-header {
    flex-wrap: wrap;
    gap: 8px 12px;
    padding: 8px 12px;
  }

  .brand {
    flex: 1 1 0;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 14px;
  }

  .nav {
    flex: 1 1 100%;
  }

  .el-menu--horizontal {
    overflow: visible;
    border-bottom: none;
  }

  /* 操作按钮永不换行裁切：换行后独占一行靠右对齐 */
  .user-box {
    margin-left: auto;
    flex-wrap: wrap;
    row-gap: 6px;
    justify-content: flex-end;
  }
}
</style>
