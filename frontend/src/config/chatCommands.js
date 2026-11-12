/**
 * `/` 命令面板的数据源（前端常量表）。
 *
 * 一期只做"插入模板 + 光标停到第一个占位"，不直接执行命令：
 * 业务库查询（WP3）与统计类能力都还在后端推进中，直接执行会给出假结果。
 * 数据结构预留 command/title/desc/category/template，后续接入意图注册表时
 * 可以把后端返回的意图直接映射成同一结构。
 */
export const CHAT_COMMANDS = [
  {
    command: '/订单',
    title: '查订单',
    desc: '按客户或订单号查订单明细与状态',
    category: '业务库',
    template: '查订单：客户/订单号 {客户或订单号}，时间 {开始日期}—{结束日期}'
  },
  {
    command: '/库存',
    title: '查库存',
    desc: '按商品与仓库查当前库存量',
    category: '业务库',
    template: '查库存：商品 {名称或 SKU}，仓库 {仓库名称}'
  },
  {
    command: '/客户',
    title: '查客户',
    desc: '查看客户画像、等级与联系方式',
    category: '业务库',
    template: '查客户：{客户名称}，查看维度 {联系方式/等级/区域}'
  },
  {
    command: '/知识库',
    title: '检索知识库',
    desc: '只从内部文档检索并回答',
    category: '内部知识',
    template: '请检索知识库：{问题}'
  },
  {
    command: '/统计',
    title: '统计概览',
    desc: '文档数、切片数、失败数等运行统计',
    category: '内部能力',
    template: '统计 {文档数/切片数/失败数}'
  },
  {
    command: '/帮助',
    title: '能力说明',
    desc: '查看这个助手支持哪些能力和示例',
    category: '使用帮助',
    template:
      '请列出你当前支持的三层能力（内部知识库检索 / 通用联网回答 / 业务库查询），并各给一个示例问题。'
  }
]

/**
 * 按已输入的命令词过滤（`/ord` 这类拼音不匹配，只匹配命令、标题、描述）。
 */
export function filterCommands(keyword = '') {
  const k = String(keyword).trim().toLowerCase()
  if (!k) return CHAT_COMMANDS
  return CHAT_COMMANDS.filter((c) =>
    `${c.command}${c.title}${c.desc}${c.category}`.toLowerCase().includes(k)
  )
}
