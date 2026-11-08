<script setup>
// 对话页（需求 P-02 / F-08）：左会话列表 + 右消息流（流式渲染 / 工具标签 / 来源卡片）+ 底部输入框。
// M3 接通 Agent、M4 接通 SSE 与历史回填；当前为结构骨架。
import { useChat } from '../composables/useChat'
import SessionList from '../components/SessionList.vue'
import MessageBubble from '../components/MessageBubble.vue'
import ChatInput from '../components/ChatInput.vue'

const { state, loadSessions, createNewSession, selectSession, removeSession, sendMessage, stop } = useChat()
</script>

<template>
  <el-row :gutter="12" class="chat-layout">
    <el-col :span="6">
      <SessionList
        :sessions="state.sessions"
        :current-id="state.currentSessionId"
        @new="createNewSession"
        @select="selectSession"
        @remove="removeSession"
      />
    </el-col>

    <el-col :span="18">
      <div class="chat-panel">
        <div class="msg-list">
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
.msg-list { flex: 1; overflow-y: auto; padding: 16px; }
</style>
