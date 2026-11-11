<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { login, fetchDemoHint } from '../api/auth'
import { AGENT_NAME } from '../config/brand.js'

const emit = defineEmits(['success'])
const form = ref({ username: '', password: '' })
const loading = ref(false)
const hint = ref(null) // { enabled, username, password }，默认关闭时后端返回 enabled=false

onMounted(async () => {
  try {
    const data = await fetchDemoHint()
    if (data && data.enabled && data.username) {
      hint.value = data
    }
  } catch {
    // 提示接口失败不影响登录表单
  }
})

function fillDemo() {
  if (!hint.value) return
  form.value.username = hint.value.username
  form.value.password = hint.value.password
}

async function onSubmit() {
  if (!form.value.username.trim() || !form.value.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const user = await login({
      username: form.value.username.trim(),
      password: form.value.password
    })
    ElMessage.success('登录成功')
    emit('success', user)
  } catch (e) {
    ElMessage.error(e.message || '登录失败，请检查账号密码')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <template #header>
        <div class="login-title">{{ AGENT_NAME }} 登录</div>
      </template>

      <!-- 演示账号提示：仅当后端 DEMO_CREDENTIAL_HINT=true 且默认密码仍可登录时出现 -->
      <el-alert
        v-if="hint"
        type="warning"
        :closable="false"
        show-icon
        class="demo-hint"
        title="演示环境默认超级管理员"
      >
        <div class="demo-body">
          <span class="demo-cred">{{ hint.username }} / {{ hint.password }}</span>
          <el-button size="small" type="primary" plain @click="fillDemo">一键填入</el-button>
        </div>
        <div class="demo-tip">该提示由部署方显式开启；登录后请在右上角「修改密码」。</div>
      </el-alert>

      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            placeholder="请输入用户名"
            autocomplete="username"
            clearable
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            show-password
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" class="login-btn" :loading="loading" @click="onSubmit">
          登录
        </el-button>
      </el-form>
      <p class="login-tip">登录后可管理本人会话；知识库维护与账号管理仅限管理员。</p>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
}
.login-card {
  width: 380px;
}
.login-title {
  font-size: 18px;
  font-weight: 600;
  text-align: center;
}
.login-btn {
  width: 100%;
}
.login-tip {
  margin: 12px 0 0;
  color: var(--text-secondary);
  font-size: 12px;
  text-align: center;
}
.demo-hint {
  margin-bottom: 12px;
}
.demo-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 4px;
}
.demo-cred {
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  font-weight: 600;
  color: var(--warning);
  user-select: all;
}
.demo-tip {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
