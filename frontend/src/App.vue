<script setup>
// 根组件：顶部菜单在「知识库管理 / 对话」两个页面间切换（架构 §P-01/P-02，无需路由级配置）
import { ref } from 'vue'
import DocsView from './views/DocsView.vue'
import ChatView from './views/ChatView.vue'

const current = ref('docs')
const menus = [
  { key: 'docs', label: '知识库管理' },
  { key: 'chat', label: '智能对话' }
]
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <div class="brand">RagKnowledge · 企业文档智能问答 Agent</div>
      <el-menu
        :default-active="current"
        mode="horizontal"
        class="nav"
        @select="(k) => (current = k)"
      >
        <el-menu-item v-for="m in menus" :key="m.key" :index="m.key">
          {{ m.label }}
        </el-menu-item>
      </el-menu>
    </el-header>

    <el-alert
      type="info"
      :closable="false"
      title="骨架阶段（M0 前框架搭建）：页面结构与接口契约已就位，业务功能按 M1→M6 里程碑逐步接通。"
      class="stage-tip"
    />

    <el-main class="app-main">
      <DocsView v-if="current === 'docs'" />
      <ChatView v-else />
    </el-main>
  </el-container>
</template>

<style scoped>
.app-shell { min-height: 100vh; }
.app-header { display: flex; align-items: center; gap: 24px; border-bottom: 1px solid #eee; }
.brand { font-weight: 600; white-space: nowrap; }
.nav { flex: 1; border-bottom: none; }
.stage-tip { margin: 12px 16px 0; }
.app-main { background: #f7f8fa; }
</style>
