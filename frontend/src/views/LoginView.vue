<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { login } from '../api/auth'

const emit = defineEmits(['success'])
const form = ref({ username: '', password: '' })
const loading = ref(false)

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
        <div class="login-title">RagKnowledge 登录</div>
      </template>
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
      <p class="login-tip">登录后可管理本人会话；知识库维护操作仅限管理员。</p>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
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
  color: #909399;
  font-size: 12px;
  text-align: center;
}
</style>
