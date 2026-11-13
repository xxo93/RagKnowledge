<script setup>
// 回答依据强度徽标（M8 / WP5-10）：强＝内部命中≥2，中＝1 条，弱＝通用/联网。
// 与能力链路徽标并列，用户先看「该不该信」，再看「走的哪条链路」。
import { computed } from 'vue'

const LEVEL_META = {
  strong: { type: 'success', text: '依据强' },
  medium: { type: 'warning', text: '依据中' },
  weak: { type: 'info', text: '依据弱' }
}

const props = defineProps({
  evidence: { type: Object, default: null }
})

const meta = computed(() => LEVEL_META[props.evidence?.level] || LEVEL_META.weak)
const tip = computed(() => {
  const e = props.evidence
  if (!e) return ''
  const count = Number(e.source_count || 0)
  const base = e.reason || ''
  return count > 0 ? `${base}（来源 ${count} 条）` : base
})
</script>

<template>
  <el-tag
    v-if="evidence"
    size="small"
    :type="meta.type"
    effect="plain"
    round
    class="evidence-badge"
    :title="tip"
    :aria-label="tip"
  >{{ meta.text }}</el-tag>
</template>

<style scoped>
.evidence-badge {
  flex: 0 0 auto;
  cursor: help;
}
</style>
