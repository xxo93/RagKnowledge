<script setup>
// 顶栏能力状态灯：知识库 / 联网 / 业务库三点。
// 绿=可用、灰=未配置、红=异常。hover 显示具体含义，窄屏只留圆点，靠 tooltip 说明。
import { computed } from 'vue'

const props = defineProps({
  status: { type: Object, default: null },
  loading: { type: Boolean, default: false }
})

const items = computed(() => {
  const s = props.status || {}
  return [
    {
      key: 'kb',
      label: '知识库',
      ready: !!s.knowledge_base_ready,
      hint: s.knowledge_base_ready ? '向量库连接正常，可检索内部文档' : '向量库不可用，检索能力暂时关闭'
    },
    {
      key: 'web',
      label: '联网',
      ready: !!s.web_search_ready,
      hint: s.web_search_ready
        ? '已配置联网搜索，可用于通用兜底回答'
        : '未配置联网搜索（WEB_SEARCH_API_KEY），问到内部知识之外的问题会走兜底话术'
    },
    {
      key: 'biz',
      label: '业务库',
      ready: !!s.biz_query_ready,
      hint: s.biz_query_ready ? '业务库只读连接已配置，可查询订单/库存等数据' : '业务库查询尚未接入或未配置只读连接'
    }
  ]
})

function stateClass(item) {
  if (props.loading) return 'dot pending'
  return item.ready ? 'dot ok' : 'dot off'
}
</script>

<template>
  <div class="status-dots" role="group" aria-label="系统能力状态">
    <el-tooltip
      v-for="item in items"
      :key="item.key"
      :content="`${item.label} · ${loading ? '检测中' : item.ready ? '可用' : '未配置'}｜${item.hint}`"
      placement="bottom"
      :show-after="200"
    >
      <span class="dot-wrap">
        <span :class="stateClass(item)" aria-hidden="true"></span>
        <span class="dot-label">{{ item.label }}</span>
      </span>
    </el-tooltip>
  </div>
</template>

<style scoped>
.status-dots {
  display: flex;
  align-items: center;
  gap: 12px;
}

.dot-wrap {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: default;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
}

.dot.ok {
  background: var(--success, #22c55e);
  box-shadow: 0 0 0 2px rgba(34, 197, 94, 0.15);
}

.dot.off {
  background: var(--border-strong, #c0c4cc);
}

.dot.pending {
  background: var(--warning, #e6a23c);
}

.dot-label {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}

/* 窄屏只保留圆点，文字靠 tooltip / aria-label 说明，保证顶栏严格单行 */
@media (max-width: 992px) {
  .dot-label {
    display: none;
  }

  .status-dots {
    gap: 8px;
  }
}
</style>
