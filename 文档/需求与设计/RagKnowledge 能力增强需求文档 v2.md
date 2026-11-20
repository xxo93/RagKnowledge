# RagKnowledge 能力增强需求文档 v2

> 版本：v2（审核通过版）  
> 日期：2026-11-11  
> 范围：输入框交互、检索可调与联网兜底、双业务库查询、顶栏 UI、管理员配置中心  
> 说明：本文件是需求规格，不含实现步骤；实现按文末 M1–M5 里程碑推进，每个里程碑单独出验收报告。

---

## 0. 已决策事项（本次审核锁定）

| 编号 | 决策项 | 结论 |
|---|---|---|
| A | 两个业务库的部署形态 | **同一 PostgreSQL 实例两个 schema**：`biz_a`（客户 / 订单 / 商品 / 库存）、`biz_b`（员工 / 考勤 / 薪资）。跨"库"联表用一条 SQL 完成 |
| B | 联网检索 provider | **阿里百炼 DashScope**（`qwen-plus` + `enable_search` + `search_options.enable_source`），Key 走 `.env`（`BAILIAN_SEARCH_API_KEY`，复用百炼 Key）；provider / base_url / model 均可在配置中心切换。无 Key / 超时 / 无结果 / 报错 → 固定兜底话术，不静默降级为模型自答 |
| C | 0.5 阈值作用于哪个分数 | **作用于余弦相似度 `score`**；`rerank_score` 只参与排序与截断，不做绝对过滤（依据：样例重排分为 0.56 / 0.06 / 0.06 / 0.01 / 0.01，做绝对阈值会误杀 4/5） |
| D | 配置中心是否含意图注册表增删改 | **一期只读展示 + JSON 导入**（导入前做 schema 校验并备份旧文件），可视化编辑留二期 |
| E | 内部能力未命中时的兜底方式 | 新增配置项 `FALLBACK_MODE`（三选一）：`web` 联网回答（默认）/ `guide` 内置"知识库未找到 + 引导换问法"话术 / `contact_admin` 内置"请联系管理员"话术。联网失败时**无论哪种模式**都回落 `contact_admin` 并带错误码 |

补充默认值（同前次审核）：过滤后有效命中 < 2 条即判定"内部知识不足"并转通用链路；意图识别置信度 < 0.6 或参数缺失 → 反问澄清，不执行；导出链接 TTL **0（默认永不过期，后台可配 0–720 分钟）**，有效期内可重复下载。

---

## 1. 现状基线（需求依据）

| 主题 | 现状事实 |
|---|---|
| 输入框 | `ChatInput.vue` 已支持 `autosize 1–6 行`、Enter 发送 / Shift+Enter 换行、500 字上限；`ChatView.vue` 桌面下输入框固定在面板底部，但 `@media (max-width:992px)` 让 `.chat-layout` 高度自适应，对话变长后整页滚动、输入框被推出首屏 |
| 检索 | `TOP_K=5`、`RETRIEVAL_MIN_SCORE=0.0`（等于不过滤）、`.env` 中 `RERANK_ENABLED=true`、`RERANK_TOP_N=5` |
| 无命中 | 命中为 0 时直接返回 `NO_RESULT_FALLBACK`，**不调 LLM**；`RAG_SYSTEM_PROMPT` 明确"只能依据参考资料回答" |
| 工具 | 仅 `knowledge_search / calculator / doc_stats`，白名单硬编码在 `agent/decision.py`、`agent/replan.py._TOOLS`、`agent/graph.py` |
| 温度 | `llm.py` 的 `chat / chat_stream` 把 `temperature=0.3` 写成函数默认参数，**未接 settings**，当前不可配 |
| UI | 顶栏右侧为平铺的"管理员 / 系统管理员 / 暗色 / 修改密码 / 退出登录"五组按钮，窄屏换行并被主体遮挡（已在上一轮修复，但层级仍混乱） |
| 数据 | 单库 `rag_agent`（pgvector）；`operation_log` 已承担管理操作审计；无业务库、无配置表、无导出能力 |

---

## 2. WP1 输入框固定 + 多行输入 + `/` 命令面板

### 2.1 目标行为

1. **固定**：任何窗口宽度下，输入框钉在视口底部；对话区内部滚动，外层页面不出现整页滚动条。
2. **多行**：输入框可视行数上限由 6 行提到 **8 行**，超出后输入框内部滚动；粘贴超长文本时提示"内容较长，已保留可继续编辑"。
3. **命令面板**：输入框行首键入 `/` 弹出命令面板，↑↓ 选择、Enter/Tab 插入、Esc 关闭；移动端提供可点击的 `/` 按钮唤起。

### 2.2 命令集（一期 6 条）

| 命令 | 面板说明 | 插入模板（`{}` 为占位参数，插入后光标停在第一个占位处） |
|---|---|---|
| `/订单` | 查询业务订单 | 查订单：客户/订单号 {客户或订单号}，时间 {起}—{止} |
| `/库存` | 查询商品库存 | 查库存：商品 {名称或 SKU}，仓库 {仓库名} |
| `/客户` | 查询客户信息 | 查客户：{客户名}，维度 {联系方式/等级/区域} |
| `/知识库` | 检索内部文档 | 请检索知识库：{问题} |
| `/统计` | 知识库统计 | 统计 {文档数/切片数/失败数} |
| `/帮助` | 能力说明 | 请列出你能做的全部事情并各给一个示例 |

### 2.3 约束

- 面板数据源为一期前端常量表，结构预留 `{command, title, desc, template, category}`，便于二期改为接口下发。
- 命令仅插入模板，不直接触发请求；发送仍由用户确认。

### 2.4 验收

- 1440px / 939px / 375px 三档：连发 10 轮长回答，输入框位置不动，页面无整页滚动。
- 多行输入到 8 行后内部滚动，Shift+Enter 换行、Enter 发送不回归。
- `/` 面板可用键盘完整操作；`/帮助` 能列出三层能力（知识库 / 通用联网 / 业务库查询）与示例。

### 2.5 影响面

`frontend/src/views/ChatView.vue`、`components/ChatInput.vue`、新增 `components/SlashCommandPanel.vue`、`App.vue`（`el-main` 滚动策略）。

---

## 3. WP2 检索参数可调 + 通用联网兜底

### 3.1 检索参数（全部来自配置中心，热生效）

| 参数 | 键名 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| 相似度阈值 | `RETRIEVAL_MIN_SCORE` | **0.50** | 0–1 | 低于该值的召回不展示、不参与生成、不进来源卡片 |
| 候选召回数 | `RETRIEVAL_CANDIDATES`（新增） | 20 | 5–50 | 向量召回候选量，重排的输入规模 |
| 重排开关 | `RERANK_ENABLED` | true | — | 关闭时按相似度排序 |
| 重排保留数 | `RERANK_TOP_N` | 5 | 1–20 | 重排后保留条数 |
| 展示 Top-K | `TOP_K` | 5 | 1–10 | 最终展示与生成的条数 |

处理顺序固定为：向量召回 `CANDIDATES` 条 → 可选重排 → **按相似度阈值过滤** → 按重排分（或相似度）排序截断到 `TOP_K`。

### 3.2 内部能力未命中时的兜底策略（可切换）

**判定口径**：只要本次回答**没有经过任何内部能力**（知识库检索、calculator、doc_stats、biz_query 均未产出有效结果），即视为"内部能力未命中"，由配置项 `FALLBACK_MODE` 决定怎么回；纯寒暄（意图判定为 `chat`）例外，仍返回简短引导话术，不触发联网（避免"你好"去打搜索接口）。

| `FALLBACK_MODE` | 行为 | 是否调 LLM | 是否联网 | 前端展示 |
|---|---|---|---|---|
| `web`（默认） | 百炼 DashScope 联网检索（超时 8s，取前 5 条来源）→ 摘要 → LLM 作答 | 是 | 是 | 标识卡「联网通用回答 · 未使用内部知识库，非内部 Agent 检索能力」+ 网页来源列表 |
| `guide` | 固定话术：`知识库中未找到相关内容。可以补充时间范围、业务对象或具体字段再问一次，例如“2026 年 10 月的订单”。` | 否 | 否 | 话术 + 改写建议；带"未使用内部知识库"淡标记，不展示来源 |
| `contact_admin` | 固定话术：`当前问题超出我可用的内部能力范围，请联系管理员协助处理（错误码 NO_INTERNAL_RESULT）。` | 否 | 否 | 话术；不展示来源 |

规则细节：

1. `FALLBACK_MODE=web` 但联网关闭 / 无 Key / 超时 / 无结果 / provider 报错 → 一律回落 `contact_admin` 话术，并追加错误码 `WEB_SEARCH_UNAVAILABLE`；**不得**改用模型自身知识冒充实网结果。
2. `WEB_SEARCH_ENABLED` 只决定"web 模式能否真的联网"，不决定模式本身；`FALLBACK_MODE` 决定语义方向。两者同屏展示，避免管理员误以为关了开关就换了策略。
3. 两种话术的文案可在配置页编辑（`FALLBACK_TEXT_GUIDE` / `FALLBACK_TEXT_CONTACT`），留空时用内置默认；文案支持 `{错误码}` 占位。
4. 内部命中 ≥ 2 条时走原 RAG 路径（`build_prompt` + 内部来源卡片），完全不受本开关影响。
5. 兜底路径的 `answer_mode` 落库为 `fallback_web` / `fallback_guide` / `fallback_contact`，便于统计三种策略的实际使用占比。
6. 寒暄/通用知识类问题（意图 `general`）在 `FALLBACK_MODE=web` 下走通用链路并带标识卡；在另外两种模式下同样按上表执行。

标识卡文案（前端固定渲染）：`联网通用回答 · 未使用内部知识库，非内部 Agent 检索能力`。

### 3.3 协议变更（向后兼容）

- SSE 新增 `event: notice`，`data: {"kind": "web_general" | "fallback_guide" | "fallback_contact", "text": "...", "provider": "tavily", "error_code": null | "WEB_SEARCH_UNAVAILABLE"}`，`kind=web_general` 位于 token 之前。
- 网页来源随 `event: source` 下发，但条目新增 `source_type: "web"` 与 `url`；内部来源 `source_type: "internal"`。
- `done` 帧新增 `answer_mode: internal | web | fallback_web | fallback_guide | fallback_contact | tool`。
- `chat_record` 新增列 `answer_mode VARCHAR(16)`，便于统计各兜底策略的实际占比。

### 3.4 联网实现约束

- provider 可插拔：`WEB_SEARCH_PROVIDER = tavily`（预留 serper / bing 的适配器接口 `search(query) -> [{title, url, snippet}]`）。
- 检索结果**不入库、不写入知识库、不参与向量索引**。
- 网页来源仅展示标题 + 域名 + 链接，链接 `target="_blank" rel="noopener"`。

### 3.5 验收

| 用例 | 期望 |
|---|---|
| 阈值 0.5，问"年假怎么申请" | 保留 2 条来源，正常作答，无标识卡 |
| 阈值调到 0.85，同一问题（`FALLBACK_MODE=web`） | 有效命中 < 2 → 联网作答，出现标识卡与网页来源 |
| 移除 `WEB_SEARCH_API_KEY`（`FALLBACK_MODE=web`） | 回落"请联系管理员"话术 + 错误码 `WEB_SEARCH_UNAVAILABLE`，不出现模型自答 |
| 关闭 `WEB_SEARCH_ENABLED`（`FALLBACK_MODE=web`） | 同上回落 `contact_admin`，不再出现旧的"知识库中未找到相关内容" |
| 切到 `FALLBACK_MODE=guide`，问一个知识库没有的问题 | 返回"知识库中未找到 + 补充时间/对象/字段"话术与改写示例，**不联网、不调 LLM**，无来源卡片 |
| 切到 `FALLBACK_MODE=contact_admin`，同一问题 | 返回"请联系管理员（NO_INTERNAL_RESULT）"话术 |
| 任一模式下问"你好" | 走寒暄引导，不触发联网 |
| 网页来源点击 | 在新标签打开真实 URL |

### 3.6 影响面

`backend/config.py`（新增 `WEB_SEARCH_*`、`FALLBACK_MODE`、`FALLBACK_TEXT_GUIDE`、`FALLBACK_TEXT_CONTACT`）、`services/rag.py`（阈值过滤与有效命中判定）、新增 `services/web_search.py`、新增 `services/fallback.py`（三模式统一出口，保证话术只在一处）、`prompts.py`（新增 `WEB_GENERAL_PROMPT` 与两句兜底话术常量）、`routers/chat.py`（notice 帧、mode 落库）、`services/chat_store.py`（列变更）、`frontend/src/api/sse.js`（notice 事件）、`components/MessageBubble.vue` / 新增 `WebNoticeCard.vue`。

---

## 4. WP3 双业务库查询能力

### 4.1 数据形态（决策 A）

同一实例两个 schema，与 `rag_agent` 共库不同 schema：

- `biz_a`：`customer`（客户）、`orders`（订单）、`product`（商品）、`inventory`（库存）
- `biz_b`：`employee`（员工）、`attendance`（考勤）、`payroll`（薪资）

演示数据要求：`biz_a` 客户 ≥ 20、订单 ≥ 300（覆盖跨月）、商品 ≥ 15、库存 ≥ 30 行；`biz_b` 员工 ≥ 20、考勤 ≥ 500、薪资 ≥ 20，且存在可通过 `employee_id` / `customer_id` 关联的字段，保证"联表查询"与"单表查询"两种场景都可演示。

### 4.2 意图注册表（一期文件维护）

位置：`backend/biz_intents.json`，字段：

```
intent_id        唯一标识（如 order_by_customer）
title            中文名（如 按客户查订单）
examples         自然语言示例数组（≥3 条，供识别与帮助文案）
schema           绑定的 schema（biz_a / biz_b）
tables           设计到的表（白名单）
join_plan        预定义 JOIN 片段（仅允许注册表内组合，模型无法自造）
allowed_columns  允许投影与过滤的列
params           参数定义：name / type(number|string|date) / required / range / example
allowed_roles    admin | member | member_self
max_rows         该意图的行数上限（不得超过全局硬上限）
```

一期提供 5 个意图：`order_by_customer`、`inventory_by_sku`、`customer_profile`、`employee_attendance`、`payroll_by_employee`。

### 4.3 执行链路（五层闸门，逐层拒绝并记审计）

| 层 | 规则 | 失败表现 |
|---|---|---|
| ① 意图识别 | 关键词预筛 → LLM 结构化输出 `{intent, args, confidence, reason}`（`json_mode`）。`confidence < 0.6`、缺少必填参数、参数类型/范围不符 → 反问澄清；无匹配意图 → 说明可查能力清单 | 澄清问句，不执行 SQL |
| ② 权限 | 独立只读 DSN（`BIZDB_DSN_READONLY`，仅 `SELECT` 权限的角色）；角色级（admin 可查全部意图，member 按 `allowed_roles`）；`member_self` 意图强制注入 `user_id` 绑定参数，禁止查询他人数据 | `40303 无权限`，不泄露表结构 |
| ③ 合规 | 只允许单条 `SELECT`；表/列/JOIN 全部来自注册表；参数一律 `%s` 绑定；禁 `SELECT *`、禁 `pg_*` / `information_schema`、禁多语句 / 注释 / `;`；语句超时 5s；限流每用户 10 次/分钟 | `40001 参数非法` / `42901 请求过于频繁` |
| ④ 数据边界 | 时间范围必填或默认近 90 天；行数上限（内联 ≤100，硬上限 1000）；敏感字段脱敏：手机 前3后4、身份证 前6后4、邮箱 前2+域名、姓名 保留姓 | 超出即转导出，脱敏在服务端完成 |
| ⑤ 返回 | ≤100 行：内联表格，默认折叠显示 10 行 + "共 N 行 / 展开 / 收起" + 下载按钮；>100 行：仅汇总（总行数、查询条件、字段清单）+ 签名下载链接（**默认长期有效、可重复下载**） | — |

### 4.4 工具与前端

- 新增 Agent 工具 `biz_query`，同步更新 `agent/decision.py` 白名单、`agent/replan.py._TOOLS`、`agent/graph.py` 路由、`prompts.py` 工具说明（含强约束："业务数据只能通过 biz_query 获取，禁止编造数字"）。
- SSE 新增 `event: table`，`data: {intent, columns:[{key,title,masked}], rows, total, truncated, hard_truncated, download_url|null, download_rows, download_ttl_minutes}`。
  - `truncated`：内联只展示了前 N 行（结果多于内联上限）；
  - `hard_truncated`：结果总数超过单次查询硬上限，CSV 里也只有前 hard_max 行（为 false 表示导出已含全部结果）。
- 表格帧随记录落库（`chat_record.table_payload JSONB`），刷新/切页后历史消息仍能还原表格卡与下载链接。
- 前端新增 `DataTableCard.vue`：折叠/展开、脱敏标记、行数统计、下载按钮、超时提示。

### 4.5 导出

- 生成 CSV（UTF-8 BOM，Excel 可直接打开），落 `data/exports/`；
- 链接：`GET /api/export/{token}`，token 为随机串（库中只存 SHA-256），**绑定 user_id + 可选有效期**（`BIZ_EXPORT_TTL_MINUTES`，0 = 永不过期，默认 0），**有效期内可重复下载**；
- 过期或他人访问 → `40402` / `40303`；后台任务只清理"设置过有效期且已过期"的记录与文件，孤儿文件保留 24h。

### 4.6 验收

| 用例 | 期望 |
|---|---|
| member 查"我上个月的订单" | 只返回本人数据、手机号脱敏、折叠表格、下载可用 |
| 跨 schema 联表"查某客户的订单和商品" | 结果正确，仅出现白名单列 |
| 构造 3000 行结果（造数 3000 单，硬上限 3000） | 对话只给汇总 + 链接（CSV 3000 行 / 3001 行含表头）；**同一链接可重复下载**；显式设置有效期后过期 → 40402；换用户访问 403 |
| 输入 `drop table orders`、`1; select * from app_user`、`information_schema.tables` | 全部拦截并写审计，返回 `40001` |
| confidence 低（"查一下那个东西"） | 反问澄清，不执行 |
| 审计 | `operation_log` 可见每次 `BIZ_QUERY` 的意图、参数摘要、行数、耗时、是否导出 |

### 4.7 影响面

新增 `services/biz_query.py`（注册表加载 / 校验 / 执行 / 脱敏）、`agent/tools/biz_query_tool.py`、`routers/biz.py`、`routers/export.py`、`services/export_store.py`、`biz_intents.json`、`scripts/seed_biz_db.py`（造数与建 schema）、前端 `DataTableCard.vue` 与 SSE 解析。

### 4.8 流式正文的打字机观感（体验项）

- 现状：SSE 是 LLM chunk 级流式，前端 60ms 合并重绘，观感是「成段蹦」；业务库/工具/兜底话术路径本身就是一次性下发。
- 目标：流式阶段正文按固定节奏逐字增长（≈90 字符/秒），回答结束后立即补齐，刷新/历史回放不重播。
- 实现：新增 `frontend/src/composables/useTypewriter.js`，`MessageBubble` 把 `typedContent` 交给 `MarkdownRenderer`；复制/重试仍用原始 `message.content`，引用与表格卡不受影响。
- 节奏：`BASE_CHARS=2`、`TICK_MS=22`；步长 `2 + floor(积压/60)` 自适应加速，保证长回答尾部不严重滞后；`streaming=false` 时同步整段补齐。
- 边界：非流式路径（`biz_query` 表格汇总、`calculator/doc_stats`、兜底整句）不播动画；`abort/error` 走同一补齐逻辑。
- 验收：采样 100ms 记录正文长度，需出现多次递增且终态与后端答案等长；`aria-busy` 在流式期间为 `true`，结束为 `false`。

---

## 5. WP4 顶栏（右上角）UI 重构

### 5.1 布局

- **左**：品牌区 = 图标 + "RagKnowledge" + 环境徽标（`DEV` / `PROD`）。
- **中**：主导航 = 智能对话 / 知识库管理 / 用户管理 / 配置中心（仅 admin 可见）。
- **右**：能力状态灯（知识库 · 联网 · 业务库，三个小圆点，hover 显示"可用 / 未配置"）→ 主题切换图标按钮 → 用户区（头像 + 显示名 + 角色）下拉（个人设置 / 修改密码 / 退出登录）。

### 5.2 视觉规范（对齐 Linear / Notion 的克制风）

- 顶栏高 60px，白/暗双主题下均为淡分隔线（`1px var(--border)`）；
- 图标按钮 32×32、圆角 8px、hover 仅换底色不描边；
- 右侧分组间距 12px，状态灯直径 8px（绿=可用、灰=未配置、红=异常）；
- 窄屏（<992px）：导航收进"更多"下拉，状态灯收进工具提示，顶栏保持单行不换行。

### 5.3 验收

- 1440 / 939 / 375 三档顶栏单行、无裁切、右上角元素全部可点击；
- 状态灯与 `GET /api/system/info` 的 `db_ok / llm_ok / web_search_enabled / biz_query_enabled` 一致；
- 下拉菜单键盘可达，主题切换即时生效且刷新后保持。

### 5.4 影响面

`frontend/src/App.vue`（顶栏结构 / 样式 / 状态灯）、新增 `components/UserMenu.vue`、`components/StatusDots.vue`、`router/index.js`（配置中心路由 + admin 守卫）、`api/system.js`。

---

## 6. WP5 管理员配置中心

### 6.1 能力

- 仅 `admin` 角色可见/可访问；普通成员访问返回 `40303`。
- 参数落库、**热生效**（进程内缓存 TTL 5s，无需重启）；保存即校验类型与范围；保存写审计；支持"恢复默认"（回退到 `.env` / 代码默认值）。
- 密钥类（`LLM_API_KEY`、`EMBEDDING_API_KEY`、`WEB_SEARCH_API_KEY`、`BIZDB_DSN_READONLY`）**不进配置页**，仅以脱敏只读形式展示来源（`.env`）。

### 6.2 参数总表

| 分组 | 参数 | 默认 | 范围 | 生效时机 |
|---|---|---|---|---|
| 检索 | 相似度阈值 | 0.50 | 0–1 | 下次请求 |
| 检索 | 候选召回数 | 20 | 5–50 | 下次请求 |
| 检索 | 重排开关 / 重排保留数 | true / 5 | 1–20 | 下次请求 |
| 检索 | 展示 Top-K | 5 | 1–10 | 下次请求 |
| 生成 | temperature | 0.30 | 0–1.5 | 下次请求（需打通 `llm.py` 参数管道） |
| 生成 | max_tokens | 1024 | 128–4096 | 下次请求 |
| 生成 | 通用兜底开关 / 联网开关 | true / true | — | 下次请求 |
| 生成 | **兜底方式 `FALLBACK_MODE`** | `web` | web / guide / contact_admin | 下次请求 |
| 生成 | 兜底话术-引导重问（`FALLBACK_TEXT_GUIDE`） | 内置文案 | ≤200 字，空=用默认 | 下次请求 |
| 生成 | 兜底话术-联系管理员（`FALLBACK_TEXT_CONTACT`） | 内置文案 | ≤200 字，空=用默认 | 下次请求 |
| 上传 | 单片上限 / 单文件总上限 | 32 / 100 MB | 1–512 | 下次请求 |
| 上传 | 默认分块大小 / 默认并发 | 4 MB / 3 | 1–32 / 1–6 | 前端下次加载 |
| 上传 | 分片过期清理小时 | 24 | 1–168 | 下次上传触发的清理 |
| 查询 | 内联行数上限 / 硬上限 / 导出 TTL | 100 / 3000 / **0（永不过期，0–720）** | — | 下次查询 |
| 查询 | 限流（次/分钟） | 10 | 1–100 | 下次查询 |
| 会话 | 历史轮数 / 标题 LLM 提炼开关 | 5 / false | 1–20 | 下次请求 |

### 6.3 存储与优先级

- 新表 `app_config`：`key / value_json / group_name / value_type / default_json / min_value / max_value / updated_by / updated_at`。
- 取值优先级：`app_config`（DB 有效值） **>** `.env` **>** 代码默认。页面"恢复默认" = 删除该 key 的 DB 记录。
- 审计：复用 `operation_log`，`action = CONFIG_UPDATE`，`detail` 记录 key / 旧值 / 新值 / 操作人。

### 6.4 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/config` | 返回全部分组参数（含默认值、范围、当前值、是否被 DB 覆盖） |
| PUT | `/api/admin/config` | 批量保存 `{key: value}`，逐项校验，非法项整批拒绝 |
| POST | `/api/admin/config/reset` | 恢复默认，可传单 key 或分组 |
| GET | `/api/admin/config/intents` | 意图注册表只读展示 + JSON 原文 |
| POST | `/api/admin/config/intents/import` | JSON 导入覆盖，先 schema 校验后备份旧文件 |

### 6.5 验收

- 温度 0.3 → 0.8，5 秒内新请求生效（日志打印实际 temperature）。
- 兜底方式 web → guide，5 秒内同一"知识库没有"的问题由联网作答变为固定引导话术（响应里不再有网页来源）。
- 兜底方式改 contact_admin 并自定义文案，返回的正是自定义文案；恢复默认后回到内置文案。
- 阈值 0.50 → 0.85，同一问题由"内部作答"变为按当前 `FALLBACK_MODE` 兜底。
- 分块默认 4MB → 8MB，刷新知识库页后大文件按 8MB 切分。
- 非 admin 访问两个 admin 接口 → `40303`；保存后审计可查；"恢复默认"可还原。
- 越界值（阈值 1.5、并发 10）→ `40001` 整批拒绝并提示具体字段。

### 6.6 影响面

新增 `services/app_config.py`、`routers/admin_config.py`、`schemas/admin_config.py`、`scripts/init_db.py` 增表、前端 `views/AdminConfigView.vue`、`api/admin.js`、路由与菜单；`llm.py` 改为从配置读 `temperature / max_tokens`；`rag.py`、`docs.py`、`config.py` 增 `RETRIEVAL_CANDIDATES` 等字段。

---

## 7. 数据库变更（DDL 草案）

```sql
-- 配置中心
CREATE TABLE IF NOT EXISTS app_config (
  key         VARCHAR(64) PRIMARY KEY,
  value_json  JSONB       NOT NULL,
  group_name  VARCHAR(32) NOT NULL,
  value_type  VARCHAR(16) NOT NULL,          -- int / float / bool / str / json
  default_json JSONB      NOT NULL,
  min_value   DOUBLE PRECISION NULL,
  max_value   DOUBLE PRECISION NULL,
  updated_by  BIGINT      NULL REFERENCES app_user(user_id),
  updated_at  TIMESTAMP   NOT NULL DEFAULT now()
);

-- 对话模式标记
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS answer_mode VARCHAR(16) DEFAULT NULL;

-- 业务查询表格帧持久化（刷新/切页后仍能还原表格卡与下载链接）
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS table_payload JSONB DEFAULT NULL;

-- 导出令牌（只存哈希；expires_at 为 NULL = 永不过期）
CREATE TABLE IF NOT EXISTS export_token (
  token_hash   CHAR(64) PRIMARY KEY,
  user_id      BIGINT      NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
  file_path    VARCHAR(512) NOT NULL,
  filename     VARCHAR(255) NOT NULL,
  row_count    INT         NOT NULL DEFAULT 0,
  expires_at   TIMESTAMP   NULL,
  downloaded_at TIMESTAMP  NULL,
  created_at   TIMESTAMP   NOT NULL DEFAULT now()
);
```

---

## 8. 里程碑与验收口径

| 里程碑 | 内容 | 验收方式 |
|---|---|---|
| M1 | 配置中心骨架：`app_config` + 读写接口 + 热生效 + 温度参数管道 + 配置页（检索/生成组） | pytest 覆盖配置读写与越界；接口冒烟；页面截图 |
| M2 | WP1 输入框/多行/`/` 命令 + WP4 顶栏重构 | `npm run build` EXIT=0；三档宽度浏览器实测截图 |
| M3 | WP2 阈值过滤 + 有效命中判定 + `FALLBACK_MODE` 三模式兜底 + 联网 + notice 标识卡 + 网页来源 | 阈值切换用例、三种兜底模式用例、无 Key 回落用例、SSE 帧顺序核对 |
| M4 | WP3 前半：建 schema 与造数 + 意图注册表 + 单库查询 + 五层闸门 + 审计 + 折叠表格 | 权限/越权/澄清/脱敏用例，pytest + 接口冒烟 |
| M5 | WP3 后半：跨 schema 联表 + 大结果导出 + 链接 TTL 校验 | 3000 行导出、TTL 过期、跨用户 403、清理任务 |

每个里程碑交付：代码 + `M{n} 验收报告.md`（含测试命令、结果、遗留项），并回归后端 `pytest` 与前端 `npm run build`。

---

## 9. 风险与应对

| 风险 | 应对 |
|---|---|
| 阈值调到 0.85+ 导致大量问题转联网 | 配置页展示"近 7 天内部命中率"提示；阈值变更写审计，可一键回默认 |
| 联网不可用 / 超额 | 8s 超时 + 兜底话术 + 开关可关；provider 抽象便于替换 |
| 业务查询越权或注入 | 只读 DSN + 注册表白名单 + 参数绑定 + 语句校验 + 限流 + 全量审计，五层任一不过即拒 |
| 导出文件堆积 | 有效期内可重复下载（不因下载过而失效）；设置有效期后才过期，后台只清理显式过期项；孤儿文件 24h 回收 |
| 配置误改影响线上 | 范围校验 + 保存审计 + 恢复默认；密钥类参数不进配置页 |
| 顶栏改动破坏登录/主题流程 | 改动只涉及结构与样式，保留 `initTheme`/`reloadMe` 的 try/catch 防护 |

---

## 10. 明确不做（一期）

- 用户自写 SQL、任意 JOIN 组合、跨实例联邦查询、写操作与存储过程；
- 联网结果写入知识库或参与向量检索；
- 意图注册表的可视化增删改（仅只读 + JSON 导入）；
- 配置项版本历史与多环境配置集；
- XLSX 导出、导出分页续传；
- 独立"数据查询"页签（一期仅 Agent 工具 + 结果卡片）。
