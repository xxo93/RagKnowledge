<script setup>
// 业务库查询结果表格卡（M4）：默认折叠展示 10 行，可展开/收起，脱敏列带标记。
import { computed, ref } from 'vue'

const props = defineProps({
  table: { type: Object, required: true }
})

const PREVIEW_ROWS = 10
const expanded = ref(false)

const columns = computed(() => props.table?.columns || [])
const rows = computed(() => props.table?.rows || [])
const total = computed(() => Number(props.table?.total ?? rows.value.length))
const collapsedRows = computed(() => (expanded.value ? rows.value : rows.value.slice(0, PREVIEW_ROWS)))
const canExpand = computed(() => rows.value.length > PREVIEW_ROWS)
const maskedCount = computed(() => columns.value.filter((c) => c.masked).length)
const truncated = computed(() => Boolean(props.table?.truncated))
// 只有结果本身超过硬上限时，CSV 才会缺行；否则导出已含全部结果
const hardTruncated = computed(() => Boolean(props.table?.hard_truncated))
const inlineMax = computed(() => Number(props.table?.inline_max ?? rows.value.length))
const downloadUrl = computed(() => props.table?.download_url || '')
const downloadRows = computed(() => Number(props.table?.download_rows ?? 0))
const downloadTtl = computed(() => Number(props.table?.download_ttl_minutes ?? 0))

function cellValue(row, col) {
  const value = row?.[col.key]
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}
</script>

<template>
  <div class="data-table-card" role="region" :aria-label="`${table.title || '业务查询'} 结果`">
    <div class="card-head">
      <div class="title-wrap">
        <span class="title">{{ table.title || '业务查询结果' }}</span>
        <el-tag size="small" type="success" effect="plain">业务库</el-tag>
        <el-tag v-if="maskedCount" size="small" type="warning" effect="plain">
          {{ maskedCount }} 列已脱敏
        </el-tag>
      </div>
      <div class="meta">
        共 {{ total }} 行<span v-if="truncated">，仅展示前 {{ rows.length }} 行</span>
        <span class="elapsed">· {{ table.elapsed_ms }}ms</span>
      </div>
    </div>

    <div class="table-wrap" :class="{ expanded }">
      <table>
        <thead>
          <tr>
            <th v-for="col in columns" :key="col.key">
              {{ col.title }}
              <span v-if="col.masked" class="mask-flag" title="该列已服务端脱敏">*</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in collapsedRows" :key="index">
            <td v-for="col in columns" :key="col.key" :class="{ masked: col.masked }">
              {{ cellValue(row, col) }}
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length || 1" class="empty">没有匹配的数据</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card-foot">
      <div class="hints">
        <span v-if="downloadUrl">
          结果超过内联上限 {{ inlineMax }} 行，完整 {{ downloadRows }} 行 CSV 已脱敏导出，<span
            v-if="downloadTtl > 0"
          >链接 {{ downloadTtl }} 分钟内有效、过期前可重复下载</span><span v-else>链接长期有效、可重复下载</span><span
            v-if="hardTruncated"
          >；超出单次查询硬上限的部分未包含</span>。
        </span>
        <span v-else-if="maskedCount">手机号、身份证、邮箱、姓名等敏感列已按规则脱敏，原文不返回给前端。</span>
      </div>
      <div class="actions">
        <el-button v-if="canExpand" link size="small" @click="expanded = !expanded">
          {{ expanded ? '收起' : `展开全部 ${rows.length} 行` }}
        </el-button>
        <a
          v-if="downloadUrl"
          class="dl-link"
          :href="downloadUrl"
          :download="`biz_${table.intent || 'export'}.csv`"
          title="链接绑定当前账号，有效期内可重复下载"
        >
          下载 CSV · {{ downloadRows }} 行
        </a>
        <el-button v-else link size="small" disabled title="结果未超过内联上限，无需导出">下载</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.data-table-card {
  margin-top: 10px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  background: var(--surface);
  overflow: hidden;
  /* 窄屏关键：卡片必须跟随气泡宽度收缩，宽表格改为卡内横向滚动，不能把气泡/页面撑溢 */
  width: 100%;
  max-width: 100%;
  min-width: 0;
  box-sizing: border-box;
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 8px 10px;
  background: var(--surface-muted);
  flex-wrap: wrap;
}

.title-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.meta {
  font-size: 11px;
  color: var(--text-secondary);
}

.elapsed {
  margin-left: 4px;
}

.table-wrap {
  overflow: auto;
  max-height: 320px;
}

.table-wrap.expanded {
  max-height: none;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

th,
td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-light);
  text-align: left;
  white-space: nowrap;
}

th {
  position: sticky;
  top: 0;
  background: var(--surface);
  color: var(--text-secondary);
  font-weight: 600;
  z-index: 1;
}

td.masked {
  color: var(--text-secondary);
  letter-spacing: 0.5px;
}

.mask-flag {
  color: var(--warning);
  margin-left: 2px;
}

tbody tr:hover td {
  background: var(--surface-muted);
}

.empty {
  text-align: center;
  color: var(--text-muted);
  padding: 16px;
}

.card-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-top: 1px solid var(--border-light);
  flex-wrap: wrap;
}

.hints {
  font-size: 11px;
  color: var(--text-muted);
  min-width: 0;
  flex: 1;
}

.actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
  align-items: center;
}

.dl-link {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--primary);
  background: var(--primary-light);
  border: 1px solid transparent;
  text-decoration: none;
  transition: background 0.15s ease, box-shadow 0.15s ease;
}

.dl-link:hover {
  background: color-mix(in srgb, var(--primary) 16%, var(--panel));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary) 35%, transparent);
}
</style>
