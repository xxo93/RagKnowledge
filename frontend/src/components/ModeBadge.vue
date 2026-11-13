<script setup>
// 能力模式徽标（v3 / WP2）：一眼看出这条回答走了哪条链路；hover 显示能力边界说明。
import { computed } from 'vue'

const props = defineProps({
  mode: { type: String, default: '' },
  intent: { type: String, default: '' },
  size: { type: String, default: 'small' }
})

// 颜色语义与需求 §4.4 对齐：知识库蓝 / 通用灰 / 联网橙 / 业务库绿 / 工具青 / 任务紫
const MODES = {
  knowledge: {
    label: '知识库',
    tone: 'knowledge',
    tip: '回答基于企业内部知识库检索结果，来源见下方引用卡片。'
  },
  general: {
    label: '通用',
    tone: 'general',
    tip: '由通用大模型直接回答，未使用企业内部资料；涉及内部制度/金额/人名请切换知识库模式。'
  },
  web: {
    label: '联网',
    tone: 'web',
    tip: '内部知识不足时联网检索所得，不属于企业内部知识，仅供参考。'
  },
  biz: {
    label: '业务库',
    tone: 'biz',
    tip: '查询企业业务库（订单/库存/客户/考勤/薪资），结果已在服务端脱敏并限制行数。'
  },
  tool: {
    label: '工具',
    tone: 'tool',
    tip: '由确定性工具计算或统计得出（计算器 / 文档统计），结果可复算。'
  },
  workflow: {
    label: '任务',
    tone: 'workflow',
    tip: '多步骤任务编排（规划 → 执行 → 汇总），每一步复用知识库/业务库/工具能力。'
  },
  guide: {
    label: '未命中',
    tone: 'guide',
    tip: '本次未命中内部知识库，按配置回落引导或联系管理员，未编造内容。'
  }
}

const meta = computed(() => {
  const byMode = {
    knowledge: 'knowledge',
    general: 'general',
    chat: 'general',
    web: 'web',
    biz_query: 'biz',
    tool: 'tool',
    workflow: 'workflow',
    fallback_guide: 'guide',
    fallback_contact: 'guide'
  }
  const key = byMode[props.mode] || (props.intent === 'workflow' ? 'workflow' : '')
  return MODES[key] || null
})
</script>

<template>
  <el-tooltip v-if="meta" :content="meta.tip" placement="top" :show-after="200">
    <span class="mode-badge" :class="[`tone-${meta.tone}`, `is-${size}`]" tabindex="0">
      <span class="dot" aria-hidden="true"></span>{{ meta.label }}
    </span>
  </el-tooltip>
</template>

<style scoped>
.mode-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
  font-size: 11px;
  line-height: 18px;
  white-space: nowrap;
  cursor: default;
  user-select: none;
}

.mode-badge.is-small {
  font-size: 11px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.9;
}

/* 知识库：蓝 */
.tone-knowledge {
  color: #2f6bff;
  background: rgba(47, 107, 255, 0.1);
  border-color: rgba(47, 107, 255, 0.25);
}

/* 通用：灰 */
.tone-general {
  color: #6b7280;
  background: rgba(107, 114, 128, 0.1);
  border-color: rgba(107, 114, 128, 0.22);
}

/* 联网：橙 */
.tone-web {
  color: #d97706;
  background: rgba(217, 119, 6, 0.1);
  border-color: rgba(217, 119, 6, 0.25);
}

/* 业务库：绿 */
.tone-biz {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.1);
  border-color: rgba(15, 157, 88, 0.25);
}

/* 工具：青 */
.tone-tool {
  color: #0e8fa3;
  background: rgba(14, 143, 163, 0.1);
  border-color: rgba(14, 143, 163, 0.25);
}

/* 任务：紫 */
.tone-workflow {
  color: #7c3aed;
  background: rgba(124, 58, 237, 0.1);
  border-color: rgba(124, 58, 237, 0.25);
}

/* 兜底：中性 */
.tone-guide {
  color: #8a94a6;
  background: rgba(138, 148, 166, 0.12);
  border-color: rgba(138, 148, 166, 0.25);
}
</style>
