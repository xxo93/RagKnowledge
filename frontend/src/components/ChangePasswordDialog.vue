<script setup>
// 修改本人密码弹窗：仅做交互校验，长度/新旧规则后端二次强校验。
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { changePassword } from '../api/auth'

const props = defineProps({
  visible: { type: Boolean, default: false },
  username: { type: String, default: '' }
})
const emit = defineEmits(['update:visible'])

const form = ref({ old_password: '', new_password: '', confirm: '' })
const loading = ref(false)

watch(
  () => props.visible,
  (open) => {
    if (open) form.value = { old_password: '', new_password: '', confirm: '' }
  }
)

function close() {
  emit('update:visible', false)
}

async function onSubmit() {
  const { old_password, new_password, confirm } = form.value
  if (!old_password || !new_password) {
    ElMessage.warning('请输入原密码与新密码')
    return
  }
  if (new_password.length < 8) {
    ElMessage.warning('新密码长度不少于 8 位')
    return
  }
  if (new_password !== confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  if (old_password === new_password) {
    ElMessage.warning('新密码不能与原密码相同')
    return
  }
  loading.value = true
  try {
    await changePassword({ old_password, new_password })
    ElMessage.success('密码已修改，其他设备的登录态已失效')
    close()
  } catch (e) {
    // 401/403 由全局拦截器统一提示；这里只兜底业务错误文案
    if (e.bizCode !== 40101 && e.httpStatus !== 401) {
      ElMessage.error(e.message || '修改密码失败')
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="修改密码"
    width="420px"
    :close-on-click-modal="false"
    @update:model-value="close"
  >
    <el-form label-position="top" @submit.prevent="onSubmit">
      <el-form-item label="当前账号">
        <el-input :model-value="username" disabled />
      </el-form-item>
      <el-form-item label="原密码">
        <el-input v-model="form.old_password" type="password" show-password autocomplete="current-password" />
      </el-form-item>
      <el-form-item label="新密码（至少 8 位）">
        <el-input v-model="form.new_password" type="password" show-password autocomplete="new-password" />
      </el-form-item>
      <el-form-item label="确认新密码">
        <el-input v-model="form.confirm" type="password" show-password autocomplete="new-password" @keyup.enter="onSubmit" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="loading" @click="onSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>
