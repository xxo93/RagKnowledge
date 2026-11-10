<script setup>
// 用户管理页（管理员专属，设计 §7.4 / P5）：
// 列表 + 创建 + 启用/禁用 + 重置密码。安全边界仍在后端（member 调用一律 40303），
// 前端只按角色渲染入口、禁用“操作自己”这类明显无意义按钮。
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listUsers,
  createUser,
  updateUserStatus,
  resetUserPassword
} from '../api/admin'

const props = defineProps({
  currentUserId: { type: Number, default: null }
})

const rows = ref([])
const total = ref(0)
const page = ref(1)
const size = 20
const loading = ref(false)
const busyId = ref(null)

const createVisible = ref(false)
const creating = ref(false)
const form = ref({ username: '', display_name: '', password: '', role: 'member' })

const PASSWORD_MIN = 8

function fmtTime(value) {
  if (!value) return '—'
  const text = String(value).replace('T', ' ')
  return text.slice(0, 19)
}

async function refresh(p = page.value) {
  loading.value = true
  try {
    const data = await listUsers(p, size)
    rows.value = data?.list || []
    total.value = data?.total || 0
    page.value = p
  } catch (e) {
    ElMessage.error(e.message || '用户列表加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.value = { username: '', display_name: '', password: '', role: 'member' }
  createVisible.value = true
}

async function onCreate() {
  const f = form.value
  if (!f.username.trim()) {
    ElMessage.warning('请输入用户名')
    return
  }
  if (!/^[a-zA-Z0-9_.-]{2,64}$/.test(f.username.trim())) {
    ElMessage.warning('用户名仅支持字母、数字、下划线、点、短横线，2–64 位')
    return
  }
  if (!f.password || f.password.length < PASSWORD_MIN) {
    ElMessage.warning(`密码长度不少于 ${PASSWORD_MIN} 位`)
    return
  }
  creating.value = true
  try {
    const created = await createUser({
      username: f.username.trim(),
      display_name: f.display_name.trim() || f.username.trim(),
      password: f.password,
      role: f.role
    })
    ElMessage.success(`账号已创建：${created.username}`)
    createVisible.value = false
    await refresh(1)
  } catch (e) {
    // 40004（用户名已存在）等业务错误由全局提示；这里补充针对创建表单的文案
    if (e.bizCode === 40004) ElMessage.error('用户名已存在，请更换')
  } finally {
    creating.value = false
  }
}

async function onToggleStatus(row) {
  const enable = row.status !== 1
  const actionText = enable ? '启用' : '禁用'
  try {
    await ElMessageBox.confirm(
      enable
        ? `确认启用账号“${row.username}”？`
        : `确认禁用账号“${row.username}”？禁用后其现有登录态立即失效。`,
      `${actionText}确认`,
      { type: 'warning', confirmButtonText: actionText, cancelButtonText: '取消' }
    )
  } catch (_) {
    return
  }
  busyId.value = row.user_id
  try {
    await updateUserStatus(row.user_id, enable ? 1 : 0)
    ElMessage.success(`账号已${actionText}`)
    await refresh(page.value)
  } catch (e) {
    ElMessage.error(e.message || `${actionText}失败`)
  } finally {
    busyId.value = null
  }
}

async function onResetPassword(row) {
  let pwd
  try {
    const result = await ElMessageBox.prompt(
      `为“${row.username}”设置新密码（至少 ${PASSWORD_MIN} 位）。重置后该账号现有登录态会全部失效。`,
      '重置密码',
      {
        inputType: 'password',
        inputPattern: new RegExp(`^.{${PASSWORD_MIN},128}$`),
        inputErrorMessage: `密码长度需为 ${PASSWORD_MIN}–128 位`,
        confirmButtonText: '重置',
        cancelButtonText: '取消'
      }
    )
    pwd = result.value
  } catch (_) {
    return
  }
  busyId.value = row.user_id
  try {
    const data = await resetUserPassword(row.user_id, pwd)
    ElMessage.success(`密码已重置，已吊销 ${data?.revoked_sessions ?? 0} 个登录态`)
  } catch (e) {
    ElMessage.error(e.message || '重置密码失败')
  } finally {
    busyId.value = null
  }
}

function isSelf(row) {
  return props.currentUserId !== null && row.user_id === props.currentUserId
}

function onPageChange(p) {
  refresh(p)
}

onMounted(() => refresh(1))
</script>

<template>
  <el-card>
    <template #header>
      <div class="page-head">
        <span>用户管理</span>
        <div class="head-ops">
          <el-tag type="info" size="small">共 {{ total }} 个账号</el-tag>
          <el-button type="primary" size="small" @click="openCreate">新建账号</el-button>
        </div>
      </div>
    </template>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="知识库对所有登录用户共享；账号仅决定「能否管理知识库/账号」。禁用账号会立即吊销其登录态。"
      class="tip"
    />

    <el-table :data="rows" v-loading="loading" size="small" class="table">
      <el-table-column prop="user_id" label="ID" width="70" />
      <el-table-column label="用户名" min-width="150">
        <template #default="{ row }">
          <span>{{ row.username }}</span>
          <el-tag v-if="isSelf(row)" size="small" type="success" effect="plain" class="self-tag">当前账号</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="display_name" label="显示名" min-width="130" />
      <el-table-column label="角色" width="100">
        <template #default="{ row }">
          <el-tag :type="row.role === 'admin' ? 'warning' : 'info'" size="small" effect="plain">
            {{ row.role === 'admin' ? '管理员' : '普通用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 1 ? 'success' : 'danger'" size="small" effect="dark">
            {{ row.status === 1 ? '启用' : '禁用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" min-width="150">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="最近登录" min-width="150">
        <template #default="{ row }">{{ fmtTime(row.last_login_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            size="small"
            :type="row.status === 1 ? 'danger' : 'success'"
            :loading="busyId === row.user_id"
            :disabled="isSelf(row)"
            :title="isSelf(row) ? '不能禁用自己的账号' : ''"
            @click="onToggleStatus(row)"
          >
            {{ row.status === 1 ? '禁用' : '启用' }}
          </el-button>
          <el-button
            link
            size="small"
            type="primary"
            :disabled="busyId === row.user_id"
            @click="onResetPassword(row)"
          >
            重置密码
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无账号" :image-size="60" />
      </template>
    </el-table>

    <div class="pager" v-if="total > size">
      <el-pagination
        size="small"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="size"
        :current-page="page"
        @current-change="onPageChange"
      />
    </div>

    <!-- 新建账号 -->
    <el-dialog
      v-model="createVisible"
      title="新建账号"
      width="460px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="onCreate">
        <el-form-item label="用户名（登录名，2–64 位）">
          <el-input v-model="form.username" placeholder="字母/数字/下划线/点/短横线" clearable />
        </el-form-item>
        <el-form-item label="显示名（可选）">
          <el-input v-model="form.display_name" placeholder="留空则与用户名一致" clearable />
        </el-form-item>
        <el-form-item :label="`初始密码（至少 ${PASSWORD_MIN} 位）`">
          <el-input v-model="form.password" type="password" show-password autocomplete="new-password" />
        </el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="form.role">
            <el-radio value="member">普通用户（仅本人会话 + 知识库问答）</el-radio>
            <el-radio value="admin">管理员（可管理文档与账号）</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; }
.head-ops { display: flex; align-items: center; gap: 10px; }
.tip { margin-bottom: 12px; }
.table { width: 100%; }
.self-tag { margin-left: 8px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
