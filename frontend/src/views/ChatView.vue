<script setup>
// 对话页（需求 P-02 / F-08）：左会话列表 + 右消息流（流式渲染 / 工具标签 / 来源卡片）+ 底部输入框。
// M5 接通：onMounted 加载会话并默认选中最近会话；消息深度变化自动滚动到底；工具进行中状态条。
import { ref, watch, nextTick, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useChat } from '../composables/useChat'
import SessionList from '../components/SessionList.vue'
import MessageBubble from '../components/MessageBubble.vue'
import ChatInput from '../components/ChatInput.vue'

const { state, loadSessions, createNewSession, selectSession, removeSession, renameSession, sendMessage, stop } = useChat()

const listRef = ref(null)

async function scrollToBottom() {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

// token 增量 / 工具 / 来源变化都触发跟随滚动
watch(() => state.messages, () => scrollToBottom(), { deep: true })

async function onRename(session) {
  let name
  try {
    const result = await ElMessageBox.prompt('请输入新的会话名称', '重命名会话', {
      inputValue: session.session_name,
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
      confirmButtonText: '保存',
      cancelButtonText: '取消'
    })
    name = result.value
  } catch (_) {
    return // 用户取消
  }
  const ok = await renameSession(session.session_id, name)
  if (ok) ElMessage.success('会话已重命名')
}

onMounted(async () => {
  await loadSessions()
  if (state.sessions.length) {
    await selectSession(state.sessions[0].session_id)
  }
})
</script>

<template>
  <el-row :gutter="12" class="chat-layout">
    <el-col :span="6">
      <SessionList
        :sessions="state.sessions"
        :current-id="state.currentSessionId"
        @new="createNewSession"
        @select="selectSession"
        @rename="onRename"
        @remove="removeSession"
      />
    </el-col>

    <el-col :span="18">
      <div class="chat-panel">
        <div v-if="state.streaming && state.toolStatus" class="tool-banner">
          <el-tag size="small" type="warning" effect="plain">
            ⏳ {{ state.toolStatus.label || state.toolStatus.name }}
          </el-tag>
        </div>
        <div ref="listRef" class="msg-list">
          <el-empty v-if="!state.messages.length" description="开始提问：知识库检索 / 数学计算 / 文档统计" />
          <MessageBubble
            v-for="(m, i) in state.messages"
            :key="i"
            :message="m"
          />
        </div>

        <ChatInput
          :streaming="state.streaming"
          @send="sendMessage"
          @stop="stop"
        />
      </div>
    </el-col>
  </el-row>
</template>

<style scoped>
.chat-layout { height: calc(100vh - 180px); }
.chat-panel { display: flex; flex-direction: column; height: 100%; background: #fff; border-radius: 8px; }
.tool-banner { padding: 8px 16px 0; }
.msg-list { flex: 1; overflow-y: auto; padding: 16px; }
</style>
