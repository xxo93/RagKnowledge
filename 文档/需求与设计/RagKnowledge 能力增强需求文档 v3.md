# RagKnowledge 能力增强需求文档 v3.1

> 主题：**会话记忆 · 通用对话 · 输入输出日志 · 工作流 Agent**
> 版本：v3.1（评审稿 · WP1 范围修订）
> 日期：2026-11-12
> 关系：承接《RagKnowledge 能力增强需求文档 v2》（配置中心 / 联网兜底 / 双业务库 / 顶栏 / 上传优化）与《前端优化需求文档 V1.0》
> 原则：延续现有工程约束——不引入 Redis / Celery / Milvus，全部能力在 PostgreSQL + FastAPI + Vue3 内闭环；每条需求必须可演示、可测试、可回滚

## 0.0 本次修订（v3.0 → v3.1）

| # | 修订 | 说明 |
| --- | --- | --- |
| 1 | **删除跨会话长期记忆**（v3.0 的 L3 `user_memory` 表、`/api/memory*` 接口、记忆管理页） | 记忆严格按会话隔离：一个会话一份记忆，新建会话从零开始 |
| 2 | WP1 由"长期记忆"改名为**"会话记忆"** | 目标是"同一会话聊到第 20 轮还记得第 2 轮的口径"，不是"记住我是谁" |
| 3 | 窗口长度改为**双阈值 + 业界基线推导** | 轮次 8 轮 + token 3000，先到者为准；附业界默认值对照与离线实测方法（§3.2） |
| 4 | 新增**会话事实**（P1）层 | 摘要是有损压缩，硬约束/关键数字需结构化定点保存，否则多轮压缩后被磨掉 |
| 5 | 记忆管理页 → **会话记忆面板**（P1） | 入口从"我的记忆"挪到会话详情，删会话即删记忆 |
| 6 | 历史注入改为 **answer_digest（头尾保留）** | 替代现有"砍尾 800 字"，避免长答案挤掉整个窗口 |

> 文件仍沿用 `v3.md` 命名，内容版本为 v3.1（不另存一份 800 行副本，避免两份文档并存产生歧义）。WP2 / WP3 / WP4 未改动。

---

## 0. 需求与交付物映射

| 你的原始诉求 | 对应章节 | 一句话结论 |
| --- | --- | --- |
| 1. 缺乏多轮记忆（连续对话） | WP1 | 现在只有"最近 5 轮 + 答案砍到 800 字"，第 6 轮起就忘；本版补齐**会话内**摘要与事实记忆，**不做**跨会话 |
| 2. 没用知识库/工具时没有聊天能力（写代码等） | WP2 | 现在几乎所有问题都被默认路由到 `knowledge_search`，无命中就转联网或"联系管理员"，`tool=null` 分支还只回固定话术、不调模型 |
| 3. 用户输入需要输入/输出日志承载 | WP3 | 现在只有成功落库的 `chat_record`，中断/失败/被拦截的轮次全部丢失，且没有 token 用量、trace_id、模型调用明细 |
| 4. 增加工作流 Agent，自主规划处理任务 | WP4 | 现在只有"单轮工具路由 + 最多 3 轮 replan"，无任务级计划、无步骤状态、无断点恢复、无人工确认 |
| 附加优化点 | WP5 | 模式选择器、会话记忆面板、临时会话、链路时间线、模型分级、反馈与评测、额度控制等 10 项 |

---

## 1. 现状基线（需求依据）

### 1.1 会话记忆能力

| 事实 | 证据 |
| --- | --- |
| 只取**当前会话**最近 `HISTORY_ROUNDS=5` 轮，`recent_records` SQL 上限 20 | `config.py:90`、`services/memory.py:29-31`、`services/chat_store.py:332-348` |
| 助手答案超 800 字**直接砍尾**加省略号，不留结论段 | `services/memory.py:17,47-55` |
| 窗口按"轮数"计，不按 token 计；模型 `deepseek-chat` 有 64K 上下文，实际历史只用了约 2K | `config.py:50` vs `memory.py` 无任何 token 统计 |
| 窗口外无任何压缩：第 6 轮起，第 1 轮定的口径（"金额用万元""别给 MySQL 方案"）彻底消失 | 同上；`chat_session` 无 summary 字段 |
| 会话之间零共享——**这是设计选择，不是缺陷**（2026-11-12 需求方确认） | 本文档 §3.1 边界 |

**结论**：问题不是"跨会话不记得"，而是"**同一个会话里，聊长一点就忘**"。修复范围锁定在会话内部，不做任何跨会话共享。

### 1.2 通用对话能力

| 事实 | 证据 |
| --- | --- |
| 意图决策默认落到 `knowledge_search`：非算式、非统计、非业务关键词，一律走知识库 | `agent/decision.py:73-77` |
| `PLAN_SYSTEM_PROMPT` 里 `null` 只描述为"日常寒暄"，没有"通用问答/代码/写作"这一类 | `prompts.py:38-49` |
| `tool=null` 分支返回**硬编码引导语**，不调用 LLM | `routers/chat.py:304-311` |
| 知识库有效命中 < `FALLBACK_MIN_HITS=2` 即判"内部知识不足"，转 `FALLBACK_MODE`（默认 web / 否则话术） | `routers/chat.py:208-249`、`routers/chat.py:86-135` |
| 因此"用 Python 写个快速排序"→ `knowledge_search` → 0 命中 → 联网（若开）或"请联系管理员" | 链路推导，可直接复现 |

**结论**：系统只会"查资料"，不会"聊天/写代码"。即使问的是纯创作类问题，也会被当成检索任务处理。

### 1.3 输入输出日志

| 事实 | 证据 |
| --- | --- |
| 只有 `chat_record`（成功、完整产出、正常结束才写） | `routers/chat.py:313-324`、`chat_store.insert_record` |
| 客户端中断（`CancelledError`）**不落库、不记录**，日志只有一行 info | `routers/chat.py:356-361` |
| 无 token 用量、无首字延迟、无模型名、无 temperature 快照 | `chat_record` 表结构（`init_db.sql:109-127`） |
| `request_id` 只写响应头，不回传前端、不落库，无法与一次问答关联 | `app_logging.py:32-52` |
| `operation_log` 明确"**不记录聊天问答内容**" | `services/audit.py:1-4`、`init_db.sql:89` |
| 无 LLM 调用级明细（plan / answer / title / summary 各花了多少） | `llm.py` 未记录 usage |

**结论**：线上出问题时，你只能看到"用户说没答好"，拿不到那次请求的输入、输出、耗时与模型用量；被中断/被拦截的请求完全不可追溯。

### 1.4 工作流能力

| 事实 | 证据 |
| --- | --- |
| 只有单轮工具路由：`plan → 工具 → assess → replan → answer` | `agent/graph.py:382-425` |
| `replan` 默认**确定性收敛**（`LLM_REPLAN_ENABLED=False`），最多 3 轮 | `config.py:91-95`、`agent/replan.py` |
| 没有"任务"概念：无计划、无步骤、无状态持久化、无断点续跑 | 库中无 task/step 表 |
| 无异步长任务：一次 SSE 连接即一次任务，断开即失败 | `routers/chat.py` 全程单请求 |
| 无人工确认门、无预算控制（步数/时长/token） | 无相关配置 |

**结论**：能回答"一个问题"，不能完成"一件事"。

---

## 2. 需求总览与优先级

| 编号 | 需求 | 优先级 | 后端 | 前端 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| WP1-1 | 历史窗口与 token 预算治理（轮次 + token 双阈值、answer_digest） | P0 | 是 | 否 | 无 |
| WP1-2 | 会话滚动摘要（窗口外压缩，解决长对话失忆） | P0 | 是 | 否 | WP1-1 |
| WP1-3 | 会话事实抽取（会话内数字/约束/待办，结构化定点保存） | P1 | 是 | 是 | WP1-2 |
| WP1-4 | 会话记忆面板 + 临时会话开关 | P1 | 是 | 是 | WP1-3 |
| WP1-5 | 上下文用量观测（`done` 帧带 context_usage + A/B 实测脚本） | P2 | 是 | 是 | WP1-1 |
| WP2-1 | 通用对话意图与直答链路 | P0 | 是 | 是 | 无 |
| WP2-2 | 路由优先级与"内部事实优先"保护 | P0 | 是 | 是 | WP2-1 |
| WP2-3 | 能力模式选择器（自动/知识库/通用/业务库/工作流） | P1 | 是 | 是 | WP2-1 |
| WP2-4 | 代码块增强（下载/行号/折叠） | P1 | 否 | 是 | 无 |
| WP3-1 | trace_id 全链路贯通 | P0 | 是 | 是 | 无 |
| WP3-2 | 轮次级 I/O 日志（含中断/失败） | P0 | 是 | 否 | WP3-1 |
| WP3-3 | 模型调用级日志（token/耗时/purpose） | P0 | 是 | 否 | WP3-1 |
| WP3-4 | 管理端日志查询与导出 | P1 | 是 | 是 | WP3-2/3 |
| WP3-5 | 保留期、脱敏、用户可删除 | P0 | 是 | 是 | WP3-2 |
| WP4-1 | 任务规划（Planner，结构化步骤） | P0 | 是 | 是 | 无 |
| WP4-2 | 步骤执行与失败重试（Executor/Reflector） | P0 | 是 | 是 | WP4-1 |
| WP4-3 | 任务持久化与断点恢复 | P0 | 是 | 是 | WP4-1 |
| WP4-4 | 异步运行 + 进度订阅 + 暂停/取消 | P0 | 是 | 是 | WP4-3 |
| WP4-5 | 人工确认门（HITL） | P1 | 是 | 是 | WP4-2 |
| WP4-6 | 预算控制（步数/时长/token） | P0 | 是 | 否 | WP4-2 |
| WP5-1 | 链路时间线查看器（Trace View） | P1 | 是 | 是 | WP3 |
| WP5-2 | 模型分级路由（小模型做标题/摘要/抽取） | P1 | 是 | 否 | WP1 |
| WP5-3 | 反馈（赞/踩 + 原因）进评测集 | P2 | 是 | 是 | WP3 |
| WP5-4 | 上下文预算面板 | P2 | 是 | 是 | WP1 |
| WP5-5 | 会话导出（MD/JSON） | P2 | 是 | 是 | 无 |
| WP5-6 | 每用户日 token 配额与限流 | P2 | 是 | 否 | WP3-3 |
| WP5-7 | 提示词版本化表 | P2 | 是 | 否 | 无 |

---

# WP1 会话记忆能力（P0）

## 3.1 目标与边界

一句话：**"同一个会话里，聊到第 20 轮，仍然记得第 2 轮定的口径。"**

| 做 | 不做 |
| --- | --- |
| 会话内多轮记忆：原文窗口 + 滚动摘要 + 会话事实 | 跨会话记忆：用户画像、跨会话偏好、`user_memory` 表 |
| 记忆随会话生命周期存亡（删会话即删记忆） | 跨会话检索历史对话（属另一个产品能力，见 §12） |
| 会话内可查看 / 纠正 / 清空记忆 | 全局"我的记忆"管理页 |

**为什么不做跨会话**（这是设计决策，不是遗漏）：
1. 需求方明确只要会话内记忆，每个会话一份，互不影响；
2. 跨会话记忆的价值依赖"身份稳定 + 长期使用"，在内网工具/Demo 场景收益低，而**污染风险高**——一条抽错的记忆会被注入到之后的所有会话，且用户很难发现；
3. 业界也是这么切分的：LlamaIndex 官方明确说 `ChatMemoryBuffer` / `ChatSummaryMemoryBuffer` 这类模块**只管单会话内**，跨会话要另接 mem0 / Zep 一类组件。本期不引入这类依赖。

**三层结构（全部挂在 session 上，没有 user 级）**

| 层 | 名称 | 作用 | 生命周期 | 存储 |
| --- | --- | --- | --- | --- |
| L1 | 原文窗口 | 最近 N 轮对话（按轮次与 token 双阈值裁剪） | 会话内 | 现有 `chat_record` |
| L2 | 会话摘要 | 窗口外的历史压成 ≤400 字，随会话滚动更新 | 会话内 | `chat_session.summary` + 游标 |
| L3 | 会话事实 | 会话内已确认的数字/约束/待办，结构化定点保存 | 会话内 | 新表 `session_memory` |

**注入顺序（固定）**

```
系统提示 → 【本次会话已确认】事实 → 【会话摘要】 → 最近 N 轮原文 → 检索资料 → 本轮问题
```

> 前两块是"压缩后的高密度信息"，预算不足时**优先保它们、砍原文**。反过来（保原文砍摘要）会把记忆退化回 v3.0 的 5 轮裸窗口。

## 3.2 窗口长度：先定业界的基线，再定我们的取值

### 3.2.1 业界基线（有默认值的，照抄默认值）

| 来源 | 默认值 | 口径 |
| --- | --- | --- |
| LangChain `ConversationBufferWindowMemory` | `k=5`（5 轮 / 10 条消息） | 只按轮次，不看 token |
| LlamaIndex `ChatMemoryBuffer` | `token_limit=3000`；若构造时传了 llm，则取 `context_window × 0.75` | 只按 token，不看轮次 |
| LlamaIndex `Memory`（新版） | `token_limit=30000`，`chat_history_token_ratio=0.7`（历史占 70%） | 按预算比例分配 |
| LlamaIndex `ChatSummaryMemoryBuffer` | 触及 token 上限即摘要，原始消息丢弃 | 摘要 + 窗口 |
| ChatGPT / Claude 等产品 | 不设固定轮次，靠大上下文 + 超限自动压缩 | 容量优先 |

提炼出两条可复用原则：

1. **轮次与 token 必须同时管**——只按轮次，一条 3000 字长答案就能把窗口撑爆；只按 token，短对话又会莫名其妙丢掉上下文；
2. **历史预算占模型上下文的比例在 30%~70%**，余下留给系统提示、检索资料和答案。

### 3.2.2 本项目取值（含推导）

模型是 `deepseek-chat`（64K 上下文，`config.py:50`），答案上限 `LLM_MAX_TOKENS=1024`。**容量不是瓶颈，成本和"中间遗忘"（lost in the middle）才是**，所以取偏保守值：

| 参数 | 取值 | 依据 |
| --- | --- | --- |
| `HISTORY_WINDOW_ROUNDS` | **8 轮** | LangChain 默认 5 轮偏紧（第 6 轮就忘，正好是现在被吐槽的点）；20 轮偏松，多轮之后有效信息密度骤降、还稀释注意力。8 轮覆盖绝大多数"追问链" |
| `HISTORY_MAX_TOKENS` | **3000** | 对齐 LlamaIndex 默认；约占 64K 的 5%，给检索 5 条（≈2500）和答案 1024 留足余量，也符合"历史 ≤ 上下文 70%"的行业比例 |
| `HISTORY_TURN_MAX_TOKENS` | **400** | 单轮超预算就对答案做 digest（§3.2.3），保证一条长答案不会挤掉其余七轮 |
| `HISTORY_ANSWER_DIGEST` | 头 300 字 + 尾 150 字 | 现有"砍尾 800 字"会把结论段砍掉；头尾保留才能同时保住"问题背景"和"最后结论" |

**裁剪算法**（`memory.build_context`，从最近一轮往回取）：

```
remaining = HISTORY_MAX_TOKENS
for turn in reversed(recent_turns):
    cost = tokens(question) + tokens(answer_digest)
    if turns_selected == 8 or cost > remaining: break
    select(turn); remaining -= cost
# 剩余预算按优先级分配：会话事实(≤300) → 会话摘要(≤600) → 原文窗口(≤3000)
```

**为什么不再"按条数取 20 条"**：现有 `recent_records` 是 SQL `LIMIT 20`，20 条实际只有 10 轮，且不区分长短；换成 token 计量后，长短混合的对话才不会被一条长答案拖垮。

### 3.2.3 历史里存 digest，不存全文

- `chat_record.answer` 保留**全文**（前端展示、导出、排障都要）；
- 新增 `chat_record.answer_digest`，写入时生成：`前 300 字 + …（中间省略 N 字）… + 后 150 字`，**仅用于注入**；
- 收益：① 窗口 token 可控可预估；② 用户看到的历史仍是全文，不会觉得"答了一半"；
- 代价：追问若精确指向被省略的中段，模型可能答不准 —— 用会话摘要兜底（摘要提示词要求保留"已确认的数字与结论"）。

### 3.2.4 怎么证明"8 轮 / 3000 token"是对的（离线实测，不拍脑袋）

| 项 | 做法 |
| --- | --- |
| 样本 | 20 组追问型对话，每组 15 轮；第 15 轮的问题必须依赖前文（指代"上面那个方案" / 数字"刚才算的总额" / 约束"按我说的单位"） |
| 变量 | 4 组配置：5 轮无摘要（现状） / 8 轮无摘要 / 8 轮+摘要 / 12 轮+摘要 |
| 指标 | ① 指代消解正确率（人工判定） ② 每次请求注入 token ③ 首字延迟 ④ 单轮成本 |
| 判定 | 取「正确率 ≥ 95% 且 token 曲线开始走平」的拐点；若 8 轮已达 95%，就不再加到 12 轮 |
| 落地 | `scripts/eval_memory_window.py`（离线跑，不进主链路），结果直接抄进 M1 验收报告 |

> 这个脚本的价值不只是选参数：以后换模型、换业务、答案变长，都能重测一次再调参，而不是凭感觉改配置。

## 3.3 L2 会话摘要（滚动压缩）

**触发**：未摘要轮次超过 `SUMMARY_TRIGGER_ROUNDS`（默认 **12**）时，异步压缩**游标之前**的对话，保留最近 `SUMMARY_KEEP_ROUNDS`（默认 **8**，必须 ≥ 窗口轮次）轮原文不动。

```
[会话开始] ──轮1..8────> 全在窗口内，不压缩
            ──轮9..12───> 已滑出窗口，但仍在缓冲，原文完整保留
            ──轮13──────> 未摘要 13 > 12，触发压缩：轮1..5 → 摘要 S1（游标=5）
                          原文保留轮6..13（= 窗口 8 轮），摘要与窗口首尾相接
            ──轮14..17──> 未摘要 9~12 轮，不触发
            ──轮18──────> 未摘要 13 > 12，再压缩：S1 + 轮6..9 → S2（游标=10）
```

> **硬不变量：`SUMMARY_KEEP_ROUNDS ≥ HISTORY_WINDOW_ROUNDS`。**
> 否则游标会落在窗口左边界之外，出现"既不在摘要、也不在窗口"的空档，这几轮会**静默丢失且不报错**（草案里 KEEP=4 就是这个坑：第 18 轮时第 10、11 轮直接消失）。取 8 时摘要与窗口正好首尾相接——不重叠（不重复烧 token），也不留空档。

- **为什么设 4 轮缓冲**：触发阈值 12 = 保留 8 + 缓冲 4，所以每次压缩只吸收 4 轮，触发间隔约 5 轮（13 → 18 → 23…）。一次 LLM 调用摊到 5 轮上，而不是每问一次都压一次；
- 摘要用 LLM 生成（`purpose=summary`，可用小模型），目标 ≤ 400 字，**强制保留五类信息**：用户目标 / 已确认的事实与数字 / 已排除的方案 / 未完成事项 / **会话内约束（单位、口径、禁用方案）**；
- 摘要失败（超时/超预算）**不阻塞问答**，游标不动，下一轮再试；
- 幂等：摘要正文与游标同行更新，`UPDATE ... WHERE summarized_until_record_id < new_cursor`，并发下不会重复压缩同一段；
- 压缩是**异步**的（在 `done` 帧之后跑），不占首字延迟。

**配置**：`SUMMARY_ENABLED=true`、`SUMMARY_TRIGGER_ROUNDS=12`、`SUMMARY_KEEP_ROUNDS=8`（≥ `HISTORY_WINDOW_ROUNDS`）、`SUMMARY_MAX_CHARS=400`。

**验收**
- 连续 15 轮讨论同一份文档，第 15 轮问"最开始我们说要排除哪个方案"，能正确回答；
- 第 13 轮后 `chat_session.summary` 非空、`summarized_until_record_id` 递增且不重复推进；
- 关闭 `SUMMARY_ENABLED` 后，退化为"8 轮原文、无摘要"，不影响问答可用性（可回滚）。

## 3.4 L3 会话事实（P1）

### 3.4.1 为什么摘要之外还要这一层

摘要是有损压缩：400 字要塞进整个会话的脉络，像"金额单位是万元""不要给 MySQL 方案"这类**硬约束**，在第 2、3 次滚动压缩后大概率被磨掉——而这类信息的失败代价最高（答得流畅但口径全错）。所以再给一层**定点存储**：按 key 覆盖，不参与压缩，命中即注入。

### 3.4.2 记什么（全部限定在本会话内）

| kind | 含义 | 例 | 是否入库 |
| --- | --- | --- | --- |
| `constraint` | 本会话的口径与红线 | "这个会话里金额都用万元"、"别给 MySQL 方案" | ✅ |
| `fact` | 本会话已确认的数字/结论 | "Q3 营收 1.2 亿"（来自命中资料或用户确认） | ✅ |
| `entity` | 讨论对象 | "讨论的是订单中台的重构方案" | ✅ |
| `todo` | 本会话未完成事项 | "还要补一版英文摘要" | ✅ |
| — | 用户身份类 | "我叫张三"、"我是运营部的" | ❌ 不记（属跨会话能力，本期不做） |
| — | 一次性信息 | "这次查 10 月的订单" | ❌ 不记（下次问就过时） |

### 3.4.3 抽取与覆盖

**时机**（异步，绝不阻塞回答）：
1. 每 `SESSION_FACT_EVERY_ROUNDS`（默认 4）轮一次；
2. 会话结束时一次（切换会话 / 关闭页面，由前端 `navigator.sendBeacon` 触发）；
3. 用户显式说"这个会话里…""记住…"时立即抽取。

**方式**：小模型结构化输出（`purpose=session_fact`）
```json
{"items":[{"kind":"constraint","key":"金额单位","value":"万元","confidence":0.93}]}
```
- `confidence < 0.6` 丢弃；
- `mem_key` 归一化（"金额单位"≈"币种单位"≈"单位"），保证同一件事只有一条 active；
- 写 `session_memory` 时带上 `source_record_id`，面板上可点"来源"跳回那一轮；
- 抽取时**只喂本会话内容**，函数签名里没有 `user_id` 检索路径，从结构上杜绝跨会话取数。

**覆盖与删除**（去掉 v3.0 的衰减/命中计数——那些是为"长期跨会话"设计的，会话内不需要）：
- 同 key 新值写入 → 旧记录 `status='superseded'`，保留变更史（面板可显示"口径已从 元 → 万元"）；
- 用户删除 → 物理删除，"忘记"是真忘；
- 会话删除 → `ON DELETE CASCADE` 全清。

### 3.4.4 注入

```
【本次会话已确认的信息】（仅用于保持前后一致，不得据此编造未出现过的数据）
- [口径] 金额单位：万元
- [数字] Q3 营收：1.2 亿（来源：第 3 轮）
```

- 预算 ≤ `SESSION_FACT_MAX_TOKENS=300`、≤ 8 条；
- 排序优先级：`constraint` > `fact` > `entity` > `todo`（口径错了全盘皆错，待办可以后说）；
- 与 RAG 的关系：事实块**只保证表达一致**，不替代检索；涉及内部事实仍必须命中知识库，否则按 WP2 的路由规则处理。

## 3.5 隔离与生命周期（每一条都要有测试）

| 规则 | 实现方式 | 为什么必须有 |
| --- | --- | --- |
| 记忆只属于一个会话 | 表上只有 `session_id`；SQL 一律 `WHERE session_id=? AND user_id=?` | 双条件防越权，也防"只按 session_id 查"被猜到 id |
| **新建会话 = 空记忆** | 建会话时不复制任何摘要 / 事实 | 本版核心承诺：A 会话的口径不得流进 B |
| 删除会话 = 删除记忆 | `session_memory` 外键 `ON DELETE CASCADE`；摘要存在 `chat_session` 行上随之删除 | "忘了"必须是真忘 |
| 会话重命名 / 改标题 | 不影响记忆；摘要里的会话标题不同步（摘要只记内容） | 避免一次改名触发重新摘要 |
| 会话复制（若后续做） | 必须显式选择"带记忆复制"或"空记忆复制"，默认空 | 否则等于偷偷做成了跨会话记忆 |
| 临时会话 | `chat_session.memory_enabled=false` → 不写摘要、不抽事实、不注入；仅保留原文窗口 | 敏感讨论场景 |
| 越权访问 | 跨用户访问统一返回 `40402`（不泄露存在性） | 沿用现有风格 |

## 3.6 数据模型

```sql
-- 会话级记忆：没有 user 级表，没有 embedding，没有衰减字段
CREATE TABLE IF NOT EXISTS session_memory (
  mem_id           BIGSERIAL PRIMARY KEY,
  session_id       BIGINT NOT NULL REFERENCES chat_session(session_id) ON DELETE CASCADE,
  user_id          BIGINT NOT NULL,                     -- 冗余，仅用于越权校验
  kind             VARCHAR(16) NOT NULL,                -- constraint/fact/entity/todo
  mem_key          VARCHAR(128) NOT NULL,               -- 归一化键（同一会话内唯一）
  value            TEXT NOT NULL,
  confidence       REAL NOT NULL DEFAULT 0.7,
  source_record_id BIGINT NULL,                         -- 追溯"这条从哪轮来的"
  status           VARCHAR(16) NOT NULL DEFAULT 'active', -- active/superseded
  created_at       TIMESTAMP NOT NULL DEFAULT now(),
  updated_at       TIMESTAMP NOT NULL DEFAULT now()
);
-- 同一会话、同一 kind、同一 key 只允许一条 active
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_memory_active
  ON session_memory (session_id, kind, mem_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_session_memory_session ON session_memory (session_id, status);

ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summarized_until_record_id BIGINT DEFAULT NULL;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- 历史注入依赖这两个字段：digest 控 token，token 计数免去每轮重新分词
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS answer_digest TEXT;
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS prompt_tokens INT;
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS completion_tokens INT;
```

> `chat_record` 的 token 字段是**窗口裁剪的前置条件**：没有它，每次请求都要对全量历史重新分词，长会话下这本身就是一笔开销。

## 3.7 接口（全部以 session 为作用域）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sessions/{session_id}/memory` | 本会话记忆概览：摘要正文、事实列表（含来源轮次）、窗口占用（`turns` / `tokens` / `budget`） |
| DELETE | `/api/sessions/{session_id}/memory/facts/{mem_id}` | 删一条事实（用户纠错） |
| DELETE | `/api/sessions/{session_id}/memory` | 清空本会话记忆（只清摘要与事实，**不动原文记录**） |
| PUT | `/api/sessions/{session_id}/memory/settings` | `memory_enabled`（临时会话开关） |

> 权限：路径参数是 `session_id`，后端先校验 `chat_session.user_id = 当前用户`；会话不存在与越权都返回 `40402`（不泄露存在性）。**没有任何接收 `user_id` 参数的记忆接口**——这是本版"不跨会话"的接口级保证。

## 3.8 验收（WP1）

| # | 用例 | 期望 |
| --- | --- | --- |
| 1 | 同一会话内：第 1 轮说"金额都用万元"，第 12 轮问"Q3 营收多少" | 按万元口径作答；注入块里能看到该约束 |
| 2 | **反向用例**：A 会话说过"用 PG、别给 MySQL"，新建 B 会话问"分页查询怎么写" | B **不得出现任何 PG/MySQL 约束痕迹**；若出现，判定为跨会话串味（本版最高优先级回归项） |
| 3 | 连续 20 轮长对话 | 第 20 轮能答出第 3 轮确认的关键数字；摘要 ≤400 字；注入总量 ≤ 3000（窗口）+ 600（摘要）+ 300（事实） |
| 4 | 单轮答案 3000 字 | digest 生效（头 300 + 尾 150），窗口不因这一条而塌缩到 1 轮 |
| 5 | 关闭 `SUMMARY_ENABLED` | 退化为"8 轮原文、无摘要"，问答仍可用 |
| 6 | 删除会话 | `session_memory` 级联清空；再查 `/api/sessions/{id}/memory` 返回 40402 |
| 7 | 越权：用户 A 请求 B 的会话记忆 | 40402，B 数据无变化（审计可见） |
| 8 | 临时会话（`memory_enabled=false`） | 不写摘要、不抽事实、不注入；原文窗口照常多轮 |
| 9 | 跑 `scripts/eval_memory_window.py` | 输出 4 组配置的「正确率 / token / 首字」对照表，8 轮配置正确率 ≥95% |

---

# WP2 通用对话能力（P0）

## 4.1 目标

系统要能"**像聊天机器人一样聊天**"：写代码、解释概念、改写文案、翻译、做方案对比；同时在涉及**企业内部事实**时必须回到知识库，不能变成"绕过知识库乱答"的口子。

## 4.2 路由重构：从"默认检索"到"显式分流"

**现状问题**：`keyword_decision` 的默认分支是 `knowledge_search`，导致一切非工具问题都被当作检索。

**目标意图集（7 类）**

| 意图 | 触发 | 处理链路 | 前端标识 |
| --- | --- | --- | --- |
| `knowledge` | 问文档/制度/流程/内部资料 | RAG（现有） | 知识库 |
| `biz_query` | 客户/订单/库存/考勤/薪资 | 业务库（现有） | 业务库 |
| `calculator` | 纯算式 | 计算器（现有） | 工具 |
| `doc_stats` | 文档数/切片数 | 统计（现有） | 工具 |
| **`general`** | **代码、写作、解释、翻译、通用知识、闲聊** | **LLM 直答（新增）** | **通用** |
| `workflow` | 多步骤任务（"先…再…最后…"） | WP4 | 任务 |
| `clarify` | 置信度低 / 缺关键参数 | 反问澄清（现有 biz 逻辑复用） | — |

**路由优先级（从高到低）**

```
① 用户显式模式（模式选择器 / /命令）  ← 最高，避免"我以为" 
② 会话内上下文延续（上一轮是 general，本轮"再改短一点"仍为 general）
③ LLM 意图分类（结构化，confidence）
④ 关键词兜底（现有规则，补充 general 触发词）
⑤ 默认：knowledge（保持现状，保守）
```

**"内部事实优先"保护（关键设计）**

即使意图被判为 `general`，只要问题满足以下任一条件，**强制改走 knowledge**：
- 命中内部实体词典（公司名、产品名、部门名、制度名，来源：文档标题 + 手工配置 `INTERNAL_TERMS`）；
- 句式含"我们/公司/内部/这份文档/制度/流程/规范/报销/审批"等内部指向词；
- 上一轮是 knowledge 且本轮是追问（"那第三条呢"）。

这条是防止通用能力"抢答"内部问题，必须有，且要在日志里记录"general 被降级为 knowledge 的原因"。

## 4.3 通用回答链路

- 新增 `GENERAL_SYSTEM_PROMPT`：
  1. 可以使用自身知识回答，**但不得编造任何企业内部数据、制度、金额、人名**；
  2. 涉及内部事实且无参考资料时，明确说"这需要查内部资料"，并提示可切换知识库模式；
  3. 代码必须完整可运行，标注语言，必要时给输入输出示例；
  4. 不输出密钥、内网地址、可执行的危险命令。
- 流式：复用 `llm.chat_stream`，`answer_mode='general'`；
- 温度：通用创作类可用较高温度（`GENERAL_TEMPERATURE=0.7`），知识库仍 0.3（配置中心分组管理）；
- 与现有兜底的关系：`general` **不触发** `FALLBACK_MODE`（它不是"内部知识不足"，而是"本来就不查内部"）；只有 `knowledge` 意图命中不足时才走兜底。

## 4.4 前端

| 改造 | 说明 |
| --- | --- |
| 模式徽标 | 每条回答右上角小徽标：知识库（蓝）/ 通用（灰）/ 联网（橙）/ 业务库（绿）/ 工具（青）/ 任务（紫），hover 显示说明 |
| 通用提示 | 通用回答顶部一行淡提示："本条回答未使用企业内部资料"，可关闭（纯界面偏好，存 localStorage，不进会话记忆） |
| 模式选择器 | 输入框左侧下拉：自动 / 仅知识库 / 仅通用 / 业务库 / 任务；默认"自动"，选择存 sessionStorage |
| 代码块 | 语言标签、复制、下载为文件（`.py/.sql/.js`）、长代码折叠（>40 行）、行号开关 |
| 空状态 | 示例问题分三组：查资料 / 写代码 / 做任务，覆盖新能力 |

## 4.5 验收（WP2）

| # | 输入 | 期望 |
| --- | --- | --- |
| 1 | "用 Python 写一个快速排序，带注释" | 走 `general`，输出可运行代码块（含语法高亮），标识"通用" |
| 2 | "把这段话改得更正式：……" | 走 `general`，直接改写，不检索 |
| 3 | "公司报销流程是什么" | 走 `knowledge`（内部指向词），不因通用能力抢答 |
| 4 | "我们生产库用哪个版本" | 走 `knowledge`；未命中则按 `FALLBACK_MODE`，不得用模型自身知识编造版本号 |
| 5 | 选择"仅通用"后问"报销流程" | 允许通用回答，但必须声明"未使用内部资料"，且不给出具体金额/审批人 |
| 6 | 选择"仅知识库"后问"写个快排" | 明确回复"当前为仅知识库模式，无法生成代码"，并提供切换入口 |
| 7 | "你好" | 由 LLM 自然回应（不再是固定引导话术），且不触发联网 |
| 8 | `ALLOW_GENERAL_CHAT=false` | 通用意图回落 `guide` 话术，链路与当前一致（可一键关闭，满足合规） |

---

# WP3 输入输出日志承载（P0）

## 5.1 目标与边界

要解决的是**可追溯**：任何一次用户输入，无论成功、失败、被中断、被拦截，都能查到"谁、什么时候、问了什么、答了什么、走了哪条链路、花了多少 token、为什么失败"。

**边界（重要）**：
- 这是**运行日志**，不是审计日志。`operation_log` 的"不记录聊天内容"约束保持不变；I/O 日志单独建表、单独保留期、单独权限；
- I/O 日志**不参与任何业务判断**，删了不影响问答；
- 默认保留 90 天，可按配置调整；用户可删除自己的记录。

## 5.2 trace_id 全链路贯通

```
前端生成 trace_id（crypto.randomUUID 短写）
  → 请求头 X-Trace-Id
  → 后端中间件读取并写入 contextvar（缺失则后端生成）
  → SSE 每个帧可选带 trace_id（仅 done/error 必带）
  → done 帧返回 trace_id
  → 前端错误提示 / 消息详情展示"复制排障信息"（含 trace_id）
  → 一次问答的所有 llm_call_log / 工具调用 / 异常日志都带同一 trace_id
```

**现状差距**：`app_logging.py` 只把 `request_id` 写响应头，**没有 contextvar、没有落库、没有回传前端**。这是本次必须补的第一块基础设施。

## 5.3 数据模型

```sql
-- 轮次级：一次提问 = 一条（无论成功失败）
CREATE TABLE IF NOT EXISTS chat_io_log (
  log_id          BIGSERIAL PRIMARY KEY,
  trace_id        VARCHAR(32)  NOT NULL,
  user_id         BIGINT       NULL,
  session_id      BIGINT       NULL,
  record_id       BIGINT       NULL,          -- 成功落库时有值，失败/中断为 NULL
  turn_index      INT          NULL,          -- 会话内第几轮
  intent          VARCHAR(32)  NULL,          -- knowledge/general/biz_query/...
  answer_mode     VARCHAR(16)  NULL,          -- knowledge/general/web/.../workflow
  query           TEXT         NOT NULL,
  answer          TEXT         NULL,
  status          VARCHAR(16)  NOT NULL,      -- ok/abort/error/timeout/rejected
  error_code      VARCHAR(48)  NULL,
  error_msg       VARCHAR(512) NULL,
  model           VARCHAR(64)  NULL,
  prompt_tokens   INT          NULL,
  completion_tokens INT        NULL,
  total_tokens    INT          NULL,
  first_token_ms  INT          NULL,          -- 首字延迟
  elapsed_ms      INT          NULL,
  frame_count     INT          NULL,          -- 本次 SSE 发出帧数
  tool_chain      JSONB        NULL,          -- [{tool,status,ms,ok}]
  context_usage   JSONB        NULL,          -- 各块注入 token：{facts,summary,history,retrieval}；history_turns
  ua              VARCHAR(256) NULL,
  ip_masked       VARCHAR(64)  NULL,          -- 123.45.*.*
  created_at      TIMESTAMP    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_io_log_user_time ON chat_io_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_io_log_trace ON chat_io_log (trace_id);
CREATE INDEX IF NOT EXISTS idx_io_log_status ON chat_io_log (status, created_at DESC);

-- 模型调用级：一次问答可能 2~5 条（plan/answer/title/summary/memory_extract）
CREATE TABLE IF NOT EXISTS llm_call_log (
  call_id        BIGSERIAL PRIMARY KEY,
  trace_id       VARCHAR(32) NOT NULL,
  user_id        BIGINT      NULL,
  purpose        VARCHAR(24) NOT NULL,  -- plan/answer/replan/title/summary/memory_extract/workflow_plan/workflow_reflect
  model          VARCHAR(64) NOT NULL,
  temperature    REAL        NULL,
  prompt_tokens  INT         NULL,
  completion_tokens INT      NULL,
  latency_ms     INT         NULL,
  status         VARCHAR(16) NOT NULL,  -- ok/error/timeout
  error_code     VARCHAR(48) NULL,
  created_at     TIMESTAMP   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_log_trace ON llm_call_log (trace_id);
CREATE INDEX IF NOT EXISTS idx_llm_log_purpose ON llm_call_log (purpose, created_at DESC);
```

## 5.4 采集实现要点

| 点 | 做法 | 注意 |
| --- | --- | --- |
| 写入时机 | `event_gen` 的 `finally` 统一写一条（覆盖 ok/abort/error/timeout） | 必须放在 `finally`，否则中断场景丢失 |
| 非阻塞 | 写日志用 `asyncio.create_task` + 有界队列（容量 1000），满则丢弃并计数告警 | 日志绝不能拖慢首字 |
| token 统计 | `llm.py` 打开 `stream_options={"include_usage": True}`，从最后一个 chunk 取 `usage`；非流式直接取 `resp.usage` | 当前 `llm.py` 完全没取 usage，这是改动点 |
| 首字延迟 | 记录第一个 `token` 帧的时间戳 | 用于评估"体感慢" |
| 中断 | `CancelledError` 分支写 `status='abort'` + 已完成 token 数 | 现状是"什么都不写" |
| 拒绝/限流 | 进入 SSE 前的 4xx（如 40402、配额耗尽）也要写一条 `status='rejected'` | 这类目前只在 access log 里 |
| 脱敏 | 手机号/身份证/邮箱在写入前脱敏（复用 `biz_query` 的脱敏函数）；`query/answer` 全文保留（排障必需），但按保留期清理 | 需在文档中写明"日志含对话正文" |
| 失败降级 | 写库异常 → `logger.warning` + 计数，不影响主流程 | 沿用 `audit.py` 风格 |

## 5.5 管理端查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/admin/io-logs` | 分页筛选：user_id / 时间范围 / status / intent / answer_mode / 关键词（query 模糊）/ trace_id |
| GET | `/api/admin/io-logs/{trace_id}` | 详情：轮次记录 + 该 trace 下所有 LLM 调用时间线 + 工具链 |
| GET | `/api/admin/io-logs/export` | 导出 CSV（同筛选条件，上限 1 万行，异步生成 + 下载链接） |
| GET | `/api/admin/io-logs/stats` | 概览：今日请求数、成功率、P95 首字延迟、token 消耗、各链路占比 |

**前端页面**：`IoLogView.vue`（仅 admin）——顶部 4 个指标卡 + 筛选栏 + 表格（时间/用户/意图/状态/耗时/首字/token/摘要）+ 详情抽屉（时间线视图）。支持"按 trace_id 搜索"，这是排障主入口。

## 5.6 用户侧

| 能力 | 说明 |
| --- | --- |
| 消息详情 | 每条回答 hover → "详情"：链路（知识库/通用/联网）、耗时、引用数、trace_id、复制排障信息 |
| 错误提示 | 出错时 toast 附 trace_id，点"复制"得到 `trace_id + 时间 + 用户`，方便用户反馈 |
| 数据权利 | 设置页可"导出我的对话记录（JSON）"与"删除我的全部日志"；删除会话时级联删除对应 I/O 日志 |

## 5.7 验收（WP3）

| # | 用例 | 期望 |
| --- | --- | --- |
| 1 | 正常问答 | `chat_io_log` 有 1 条 `ok`，`llm_call_log` ≥1 条，token 数与供应商账单误差 ≤5% |
| 2 | 生成中点击"停止" | `status='abort'`，`answer` 保留已生成部分，`elapsed_ms` 有值 |
| 3 | 后端 LLM 超时 | `status='error'`，`error_code=LLM_UNAVAILABLE`，前端 toast 带同一 trace_id |
| 4 | 会话不存在（40402） | `status='rejected'` 有记录，`user_id` 正确 |
| 5 | 管理员按 trace_id 查 | 能看到这次问答的完整时间线：plan 0.8s → 检索 0.3s → answer 首字 1.9s → 总 3.4s |
| 6 | 日志表清空 | 问答功能完全不受影响（证明日志非强依赖） |
| 7 | 用户删除自己的日志 | 该用户记录不可查；管理员查询也查不到（物理删除） |
| 8 | 超过保留期 | 定时任务清理，磁盘/表体积可控 |

---

# WP4 工作流 Agent（P0）

## 6.1 目标

从"回答一个问题"升级为"完成一件事"：用户给一个目标，Agent **先规划、再执行、边做边校验**，中途可暂停、可确认、可恢复。典型场景：

- "把知识库里三份季度报告的关键指标提出来，和业务库 Q3 订单数据对一下，出一张汇总表"
- "先查这个客户的订单，再看他的考勤异常，最后给我一段风险说明"
- "帮我把这份文档的要点整理成周报，再翻译成英文"

## 6.2 与现有 Agent 的关系（不推翻，做上层）

```
现有：单轮路由（graph.run_once）  ── 作为「一个步骤的执行器」
新增：工作流层（planner → executor → reflector）── 负责「多个步骤怎么排、坏了怎么办」
```

- 工具复用：`knowledge_search / biz_query / calculator / doc_stats / web_search / general_chat`；
- 每一步就是一次"单轮 Agent 调用"，复用现有工具节点，避免两套实现；
- 现有 `graph` 保持不动（回归风险最小），工作流以**子图/编排器**形式新增。

## 6.3 状态机

```
                 ┌─────────────┐
用户目标 ──> planning │ 生成计划    │
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐   需要人确认
                 │ executing   │──────────────> awaiting_confirm ──用户确认──┐
                 └──────┬──────┘                                          │
                        │ 每步结束                                          │
                        ▼                                                 │
                 ┌─────────────┐  不满足/失败                              │
                 │ reflecting  │────────> 重试(≤2) 或 重规划(≤2) ──────────┘
                 └──────┬──────┘
                        │ 满足全部步骤 / 预算耗尽 / 不可恢复
                        ▼
              completed / failed / cancelled / budget_exceeded
```

**任务状态**：`planning / running / awaiting_confirm / paused / completed / failed / cancelled / budget_exceeded / interrupted`
**步骤状态**：`pending / running / done / failed / skipped / awaiting_confirm`

## 6.4 Planner（规划）

输入：用户目标 + 最近会话上下文 + 可用工具清单 + 用户权限（member 不能规划业务全量查询）。
输出（结构化 JSON，强校验）：

```json
{
  "goal": "对比三份季报与 Q3 订单，产出汇总表",
  "steps": [
    {"id": 1, "title": "检索季度报告要点", "tool": "knowledge_search",
     "args": {"query": "Q3 季度报告 关键指标"}, "depends_on": [], "requires_confirm": false,
     "expected": "至少 2 条来源，含营收/成本/毛利"},
    {"id": 2, "title": "查询 Q3 订单汇总", "tool": "biz_query",
     "args": {"query": "2026 年 Q3 订单汇总"}, "depends_on": [], "requires_confirm": false,
     "expected": "返回订单数与金额合计"},
    {"id": 3, "title": "生成对比汇总", "tool": "general_chat",
     "args": {"prompt": "基于步骤1、2结果生成对比表"}, "depends_on": [1,2], "requires_confirm": false,
     "expected": "Markdown 表格，含差异说明"}
  ]
}
```

**校验器（Planner 之后强制执行）**

| 校验 | 规则 | 失败处理 |
| --- | --- | --- |
| 步数 | 1 ≤ n ≤ `WORKFLOW_MAX_STEPS`（默认 6） | 拒绝并提示拆分任务 |
| 无环 | `depends_on` 拓扑排序可解 | 拒绝，报"计划非法" |
| 工具白名单 | 只能是注册工具 | 拒绝该步 |
| 参数 schema | 按工具定义校验 | 拒绝该步 |
| 权限 | 按用户角色裁剪（member 不能查全员薪资） | 该步标记 `skipped` + 说明 |
| 危险动作 | 本期无写操作，禁止生成可执行命令/SQL | 拒绝 |

**规划前先给用户看**：默认"先出计划，用户点『开始执行』"（`WORKFLOW_AUTO_RUN=false`）。这样避免"我只是问个问题，你却跑了 6 步"。

## 6.5 Executor（执行）

- 按拓扑序执行；无依赖的步骤可并行（`WORKFLOW_PARALLEL=true` 时，默认关闭，先保证确定性）；
- 每步开始写 `workflow_step.status='running'`，结束写 `output_json`（截断存前 8KB + 完整结果存对象存储/表？本期截断即可）；
- 每步复用现有工具，**用户身份与权限透传**（`state.user`），不得用管理员身份跑普通用户任务；
- 单步超时 `WORKFLOW_STEP_TIMEOUT=60s`，整体超时 `WORKFLOW_TASK_TIMEOUT=180s`。

## 6.6 Reflector（反思与重试）

每步结束后用**轻量规则 + 可选 LLM** 判断是否满足 `expected`：

| 情况 | 处理 |
| --- | --- |
| 成功且满足 | 进入下一步 |
| 工具报错（可重试，如超时） | 重试 1 次；仍失败 → 尝试用等价工具替代（如 web_search 替 knowledge_search） |
| 结果为空/明显不满足 | 触发**重规划**（最多 2 次），只重排剩余步骤，不回滚已完成步骤 |
| 权限不足 | 标记 `skipped`，在最终报告里说明"第 2 步因权限跳过" |
| 预算耗尽 | 中止，输出"部分完成"报告（含已完成步骤结果） |

> 反思默认用规则（避免每步多一次 LLM 调用）；`WORKFLOW_LLM_REFLECT=true` 时启用 LLM 判断，用于演示"自主性"。

## 6.7 持久化与恢复

```sql
CREATE TABLE IF NOT EXISTS workflow_task (
  task_id        BIGSERIAL PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
  session_id     BIGINT NULL REFERENCES chat_session(session_id) ON DELETE SET NULL,
  goal           TEXT NOT NULL,
  plan_json      JSONB NOT NULL,
  status         VARCHAR(20) NOT NULL,        -- planning/running/awaiting_confirm/paused/completed/failed/cancelled/budget_exceeded/interrupted
  current_step   INT NOT NULL DEFAULT 0,
  budget_json    JSONB NULL,                  -- {max_steps,max_seconds,max_tokens}
  used_json      JSONB NULL,                  -- {steps,seconds,tokens}
  result_summary TEXT NULL,
  error_msg      VARCHAR(512) NULL,
  trace_id       VARCHAR(32) NULL,
  created_at     TIMESTAMP NOT NULL DEFAULT now(),
  finished_at    TIMESTAMP NULL
);

CREATE TABLE IF NOT EXISTS workflow_step (
  step_id      BIGSERIAL PRIMARY KEY,
  task_id      BIGINT NOT NULL REFERENCES workflow_task(task_id) ON DELETE CASCADE,
  step_index   INT NOT NULL,
  title        VARCHAR(128) NOT NULL,
  tool         VARCHAR(32) NOT NULL,
  args_json    JSONB NULL,
  depends_on   INT[] NULL,
  status       VARCHAR(20) NOT NULL,          -- pending/running/done/failed/skipped/awaiting_confirm
  requires_confirm BOOLEAN NOT NULL DEFAULT FALSE,
  output_json  JSONB NULL,
  error_msg    VARCHAR(512) NULL,
  retry_count  INT NOT NULL DEFAULT 0,
  started_at   TIMESTAMP NULL,
  finished_at  TIMESTAMP NULL
);
CREATE INDEX IF NOT EXISTS idx_wf_task_user ON workflow_task (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wf_step_task ON workflow_step (task_id, step_index);
```

**恢复**：进程重启时，把所有 `running` 任务置为 `interrupted`；前端任务页显示"已中断，可继续"，继续时从 `current_step` 重跑该步（幂等：同一步重跑覆盖输出，不重复计费）。

## 6.8 异步运行与订阅

- `POST /api/workflow/plan`：只规划，返回计划（不执行）；
- `POST /api/workflow/run`：创建任务 + 后台 `asyncio.Task` 执行，立即返回 `task_id`；
- `GET /api/workflow/{task_id}/events`：SSE 订阅进度（`plan / step_start / step_done / step_error / confirm_required / task_done`）；
- `GET /api/workflow/{task_id}`：查任务与全部步骤（刷新页面恢复）；
- `POST /api/workflow/{task_id}/confirm`：`{step_id, action: continue|skip|cancel}`；
- `POST /api/workflow/{task_id}/cancel`；`POST /api/workflow/{task_id}/resume`；
- `GET /api/workflow/list`：当前用户任务列表（分页）。

> 为什么不用 Celery/Redis：任务量级是"单机 Demo、个位数并发"，`asyncio.Task` + DB 状态机足够；进程重启后可恢复，比引入队列更可控。文档中要写明这个权衡（面试也是加分项）。

## 6.9 前端

**`PlanCard.vue`（消息内嵌）**
```
┌──────────────────────────────────────────────────────┐
│ 🧭 任务：对比季报与 Q3 订单            运行中 2/3    │
├──────────────────────────────────────────────────────┤
│ ✓ 1. 检索季度报告要点          1.2s    [展开结果 ▸]  │
│ ✓ 2. 查询 Q3 订单汇总          0.8s    [展开结果 ▸]  │
│ ◐ 3. 生成对比汇总             进行中…                │
├──────────────────────────────────────────────────────┤
│ [暂停]  [取消]                   预算：0.9k/8k tokens│
└──────────────────────────────────────────────────────┘
```
- 每步可展开看中间结果（来源/表格/文本摘要）；
- 失败步骤红色 + "重试 / 跳过 / 改计划"；
- `awaiting_confirm` 步骤黄色 + "继续 / 跳过 / 取消"按钮；
- 任务完成后折叠为一行摘要 + "查看详情"。

**`WorkflowView.vue`（任务中心）**：任务列表（状态、耗时、步骤数、创建时间）+ 详情（时间线 + 每步输出 + 最终报告）。

## 6.10 触发方式（避免误触发）

| 方式 | 说明 |
| --- | --- |
| 显式模式 | 输入框模式选择"任务"，或在 `/` 面板选 `/任务` |
| 显式句式 | "帮我完成…""先…再…最后…""分几步做" |
| 建议式 | LLM 判定为多步骤但用户未显式要求 → **反问**："这需要 3 个步骤，要我按计划执行吗？"（不直接跑） |
| 明确不做 | 单步问题也强行走工作流（浪费 token、拖慢响应） |

## 6.11 验收（WP4）

| # | 用例 | 期望 |
| --- | --- | --- |
| 1 | "先查知识库里 Q3 报告要点，再查 Q3 订单汇总，最后出对比表" | 生成 3 步计划；用户确认后自动执行；产出含真实来源与真实数据，不编造 |
| 2 | 第 2 步业务库超时 | 自动重试 1 次；仍失败则改计划（如降级为知识库+提示），最终报告说明降级原因 |
| 3 | 执行中关闭页面再打开 | 任务继续在后台跑；重新进入能看到当前步骤与已完成结果 |
| 4 | 执行中取消 | 当前步中止，后续步 `skipped`，任务 `cancelled`，已产生的结果保留 |
| 5 | `awaiting_confirm` 步骤 | 未确认前不继续；确认后继续；跳过则标记 skipped 并继续 |
| 6 | 6 步上限 | 生成 8 步的计划被拒绝，提示"请拆分为两个任务" |
| 7 | member 规划"查全员薪资" | 该步被裁剪/拒绝，报告说明权限不足，不泄露他人数据 |
| 8 | 预算耗尽（把 `max_seconds` 调成 10s） | 任务 `budget_exceeded`，返回已完成步骤的部分结果 |
| 9 | 进程重启 | 运行中任务变 `interrupted`，可 resume 且不重复已完成的步骤 |
| 10 | 任务全程 | 每一步都有 `llm_call_log` / `chat_io_log`（trace 一致），可在管理端复盘 |

---

# WP5 附加优化点（建议，按需取舍）

| 编号 | 优化点 | 价值 | 优先级 |
| --- | --- | --- | --- |
| WP5-1 | **链路时间线查看器**：一次问答的 plan/检索/生成/首字时间线可视化 | 排障与调优的核心工具，比看日志快 10 倍 | P1 |
| WP5-2 | **模型分级路由**：标题/会话摘要/会话事实抽取用小模型（如 qwen-turbo），回答用主模型 | 直接降成本，且不牺牲体验 | P1 |
| WP5-3 | **反馈闭环**：赞/踩 + 原因 → 自动进评测集 | 让"改 prompt"有依据，不再拍脑袋 | P2 |
| WP5-4 | **上下文预算面板**：显示本次注入 token 构成（系统/会话事实/摘要/原文窗口/检索） | 让"为什么慢/为什么贵"可见，也是窗口调参的依据 | P2 |
| WP5-5 | **会话导出**：Markdown / JSON（含来源与工具记录） | 用户刚需，也便于汇报 | P2 |
| WP5-6 | **配额与限流**：每用户日 token 上限、每分钟请求数 | 防止单个用户打爆 Key，演示环境必需 | P2 |
| WP5-7 | **提示词版本化**：`prompt_template` 表 + 版本号 + 灰度开关 | 改 prompt 可回滚、可对比，避免"改坏了不知道" | P2 |
| WP5-8 | **会话事实冲突提示**：同 key 被改写时，面板显式提示"口径已变更：元 → 万元" | 让模型记错能被用户当场发现，而不是被悄悄带偏 | P2 |
| WP5-9 | **会话 pin / 归档**：重要会话置顶，长期不用的自动归档 | 侧栏在 200+ 会话时仍可用 | P2 |
| WP5-10 | **回答"依据强度"标注**：强（内部命中≥2）/中（1 条）/弱（通用/联网） | 让用户知道该不该信，比单纯标"通用"更有用 | P2 |

> 建议本期只做 WP5-1、WP5-2（成本与效率收益最直接），其余列入下一版。

---

## 7. 接口变更清单

| 变更 | 类型 | 说明 | 兼容策略 |
| --- | --- | --- | --- |
| `POST /api/chat/stream` 请求体 | 扩展 | 新增可选 `mode`（auto/knowledge/general/biz/workflow）、`trace_id` | 不传 = auto，行为同现在 |
| `answer_mode` 取值 | 扩展 | 新增 `general`、`workflow` | 旧值不变；前端未知值按"通用"灰标处理 |
| SSE 帧 | 扩展 | 新增 `plan`、`step`、`context`（可选）；`done` 增 `trace_id`、`intent`、`context_usage`（各块 token 占用） | 前端未知事件忽略（`sse.js` 已是 default: break） |
| 响应头 | 扩展 | `X-Trace-Id` 回传 | 无影响 |
| `chat_session` | 加列 | `summary`、`summarized_until_record_id`、`memory_enabled` | 默认 NULL/TRUE，旧会话不受影响 |
| `chat_record` | 加列 | `intent VARCHAR(32)`、`trace_id VARCHAR(32)`、`answer_digest TEXT`、`prompt_tokens INT`、`completion_tokens INT` | 旧行 NULL；digest 为空时回退用全文截断（兼容历史数据） |
| 新表 | 新增 | `session_memory`、`chat_io_log`、`llm_call_log`、`workflow_task`、`workflow_step` | 纯新增 |
| 新接口 | 新增 | `/api/sessions/{id}/memory*`、`/api/workflow*`、`/api/admin/io-logs*` | 纯新增 |

---

## 8. 数据模型变更汇总（DDL）

```sql
-- WP1（会话级记忆，无 user 级表）
CREATE TABLE IF NOT EXISTS session_memory (...);         -- 见 3.6
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summarized_until_record_id BIGINT DEFAULT NULL;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS answer_digest TEXT;
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS prompt_tokens INT;
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS completion_tokens INT;

-- WP2
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS intent VARCHAR(32) DEFAULT NULL;

-- WP3
CREATE TABLE IF NOT EXISTS chat_io_log (...);            -- 见 5.3
CREATE TABLE IF NOT EXISTS llm_call_log (...);           -- 见 5.3
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS trace_id VARCHAR(32) DEFAULT NULL;

-- WP4
CREATE TABLE IF NOT EXISTS workflow_task (...);          -- 见 6.7
CREATE TABLE IF NOT EXISTS workflow_step (...);          -- 见 6.7
```

> 全部走 `init_db.sql` 的 `CREATE ... IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`，与现有增量建表方式一致；历史数据不需要回填（NULL 即"旧行为"）。

---

## 9. 技术方案与改造清单

### 9.1 后端

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `services/memory.py` | 重构 | 核心是 `build_context()`：按 token 预算装配「会话事实 → 摘要 → 原文窗口」，并产出 `answer_digest`；所有入参只有 `session_id` |
| `services/session_memory_store.py` | 新增 | `session_memory` 的 SQL 全集中（沿用 chat_store 模式），每条语句强制带 `session_id` |
| `services/session_summary.py` | 新增 | 触发判断（12 轮）、压缩、幂等写回游标 |
| `services/session_fact.py` | 新增 | LLM 结构化抽取 + 规则兜底（"这个会话里…""记住…"）+ key 归一化 |
| `agent/decision.py` | 改造 | 意图集扩到 7 类；新增 `general` 触发；内部事实降级规则 |
| `prompts.py` | 改造 | 新增 `GENERAL_SYSTEM_PROMPT`、`SUMMARY_PROMPT`、`SESSION_FACT_PROMPT`、`WORKFLOW_PLAN_PROMPT`、`WORKFLOW_REFLECT_PROMPT` |
| `routers/chat.py` | 改造 | `general` 分支（流式直答，不触发 fallback）；trace_id 贯穿；`finally` 写 I/O 日志；会话记忆注入 + `done` 后异步触发摘要/事实抽取 |
| `llm.py` | 改造 | 记录 usage（流式开 `include_usage`）、latency、purpose；写 `llm_call_log` |
| `app_logging.py` | 改造 | `trace_id` contextvar + 日志字段；响应头回传 |
| `services/io_log.py` | 新增 | 有界队列 + 批量异步写入 + 脱敏 + 降级 |
| `services/workflow/`（包） | 新增 | `planner.py` / `executor.py` / `reflector.py` / `store.py` / `state.py` |
| `routers/workflow.py` | 新增 | plan/run/events/confirm/cancel/resume/list |
| `routers/session.py` | 改造 | 新增 `/api/sessions/{id}/memory`（查看/清空/删单条/开关）；先校验会话归属 |
| `routers/admin_io_log.py` | 新增 | 日志查询/详情/导出/统计 |
| `services/app_config.py` | 改造 | 新增会话记忆/通用对话/workflow/日志相关参数（沿用热生效机制） |
| `init_db.sql` | 改造 | 新表与加列 |
| `scripts/init_db.py` | 改造 | 建表校验（新增表不存在时提示运行 SQL） |
| `scripts/cleanup_io_log.py` | 新增 | 保留期清理（定时/手动） |
| `scripts/eval_memory_window.py` | 新增 | 窗口长度离线 A/B（5/8/8+摘要/12+摘要），产出正确率与 token 对照表 |

### 9.2 前端

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `components/PlanCard.vue` | 新增 | 工作流计划/步骤进度卡 |
| `components/ModeBadge.vue` | 新增 | 链路徽标（知识库/通用/联网/业务库/任务） |
| `components/ModeSelector.vue` | 新增 | 输入框模式下拉 |
| `components/SessionMemoryPanel.vue` | 新增 | 会话记忆抽屉：摘要正文、事实列表（含来源轮次）、窗口占用条（`8 轮 / 3000 token` 实际用量）、临时会话开关 |
| `components/MemoryItem.vue` | 新增 | 面板内的事实条目（删除 / 跳回来源轮次） |
| `components/TraceDrawer.vue` | 新增 | 消息详情时间线 |
| `views/WorkflowView.vue` | 新增 | 任务中心 |
| `views/IoLogView.vue` | 新增 | 管理端日志查询 |
| `components/MessageBubble.vue` | 改造 | 徽标、详情按钮、通用提示行 |
| `components/MarkdownRenderer.vue` | 改造 | 代码下载/行号/长代码折叠 |
| `composables/useChat.js` | 改造 | mode/trace_id、plan/step 事件、会话记忆状态 |
| `api/sse.js` | 改造 | 新事件、trace_id 透传 |
| `api/sessionMemory.js`、`api/workflow.js`、`api/ioLog.js` | 新增 | 新接口封装 |
| `views/ConfigView.vue` | 改造 | 新增"会话记忆/通用/工作流/日志"参数分组 |

### 9.3 新增依赖

- 前端：**无新增**（Markdown/高亮已具备）；
- 后端：**无新增**（`asyncio` + `psycopg` 足够；不引入 celery/redis/apscheduler，清理任务用脚本 + 手动/系统计划任务）。

---

## 10. 里程碑

| 里程碑 | 内容 | 交付验收 |
| --- | --- | --- |
| M1 | 会话记忆底座：token 窗口治理 + digest + 滚动摘要 + 会话事实 + 注入装配 + 面板 + 开关 | WP1 用例 1~9；`eval_memory_window.py` 报告；`pytest` 全绿；关闭开关行为不变 |
| M2 | 通用对话：意图扩展 + `general` 直答 + 内部事实降级 + 前端徽标/选择器 | WP2 用例 1~8；"报销流程"不被通用抢答 |
| M3 | I/O 日志：trace_id + 双表 + 中断/失败覆盖 + 管理端查询 + 保留期 | WP3 用例 1~8；中断请求可查 |
| M4 | 工作流：plan/execute/reflect + 持久化 + 异步订阅 + 前端 PlanCard | WP4 用例 1~10；刷新可恢复 |
| M5 | 打磨：HITL 确认门、Trace 查看器、模型分级、任务中心列表 | 全量回归 + 一次端到端演示脚本 |

> 每个里程碑单独出验收报告（沿用 `M{n} …验收报告.md` 的既有格式），并回归：后端 `pytest`、前端 `npm run build`、SSE 五类旧事件不破坏。

---

## 11. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| **会话事实抽错**：把错的数字/口径记下来并反复注入 | 答得流畅但全错，用户很难察觉 | 置信度门槛 0.6 + 面板可见可删 + 注入块在消息详情展开 + 同 key 冲突提示 |
| **跨会话串味**：A 会话的约束漏进 B 会话 | 直接违反本版核心承诺，用户会认为"记忆不可控" | 表结构里没有 user 级路径、注入只按 `session_id` 取；用例 2 作为**负向回归**，每次发版必跑 |
| **通用对话绕过知识库** | 编造内部制度/数字，比"没答"更糟 | 内部事实优先降级 + 通用提示行 + 可全局关闭（`ALLOW_GENERAL_CHAT`）+ 编造检测提示词约束 |
| **成本失控**：摘要/事实抽取/工作流每步都调模型 | token 费用翻倍 | 模型分级（小模型做辅助任务）+ 预算上限 + 缓存（摘要按游标幂等，不重复生成） |
| **窗口调大导致成本与延迟上升** | 每轮多烧 token、首字变慢 | 轮次 + token 双阈值 + digest 控单轮预算 + 先跑 `eval_memory_window.py` 再调参 |
| **延迟变差**：摘要与事实抽取各多一次 LLM 调用 | 首字变慢 | 全部在 `done` 帧之后异步执行，不阻塞首字；摘要每 4 轮才触发一次，成本摊薄；首字延迟进监控 |
| **工作流死循环/跑飞** | 一直调工具、烧钱 | 步数上限 + 重试上限 + 重规划上限 + 整体超时 + 预算中止 + 可随时取消 |
| **工作流越权** | member 通过多步间接查全量数据 | 每步权限校验（不是只在规划时校验一次）+ 工具内部原有闸门不变 |
| **日志隐私** | 对话正文落库 | 保留期（默认 90 天）+ 访问仅 admin + 脱敏 + 用户可删除 + 文档明示"日志含正文" |
| **日志拖慢主链路** | 首字变慢 | 有界队列 + 异步批量写 + 队列满丢弃计数 + 绝不 await 写库 |
| **改动面过大破坏现有链路** | 回归失败 | 所有新能力默认开关可控（`SUMMARY_ENABLED` / `SESSION_FACT_ENABLED` / `ALLOW_GENERAL_CHAT` / `WORKFLOW_ENABLED` / `IO_LOG_ENABLED`），关闭即回到当前行为 |

---

## 12. 明确不做（本期）

- **跨会话长期记忆**（用户画像、跨会话偏好、跨会话事实复用）——明确不做；如后续要做，另立需求，需 mem0 / Zep 一类组件，不在本期范围；
- 跨会话检索历史对话（"我问过你什么"这类全局搜索）；
- 记忆的向量专用库（会话内事实用结构化召回即可，不引入 pgvector / Milvus）；
- 自动执行模型生成的代码/SQL（只生成、不执行）；
- 工作流中的写操作（下单、改数据、发邮件）；
- 多智能体协作（planner/executor 之外的 agent 角色、agent 间对话）；
- 分布式任务队列（Celery/Redis/RQ）；
- 语音/多模态记忆。

---

## 13. 待确认决策（请审核时拍板）

| # | 问题 | 选项 | 我的建议 |
| --- | --- | --- | --- |
| 1 | 通用对话是否默认开启？ | 默认开 / 默认关（需管理员开） | **默认开**（演示价值大），但加 `ALLOW_GENERAL_CHAT` 一键关闭，并在通用回答上明确标注"未使用内部资料" |
| 2 | **会话窗口取 8 轮 / 3000 token 是否认可？** | 5 轮（最省成本）/ 8 轮（推荐）/ 12 轮（最连贯） | **8 轮 + 3000 token，先到者为准**。取 LangChain 5 轮与 LlamaIndex 3000 的折中；上线前用 `eval_memory_window.py` 实测校准，不满意只改配置不动代码 |
| 3 | 历史注入用 digest（头 300 + 尾 150）还是原文？ | digest / 原文砍尾 800 字（现状）/ 不截断 | **digest**。砍尾会丢结论段；不截断则一条长答案就挤掉整个窗口 |
| 4 | 会话事实抽取用 LLM 还是纯规则？ | LLM / 规则 / 混合 | **混合**：LLM 结构化抽取 + 规则兜底（正则识别"这个会话里…""记住…"），置信度门槛 0.6 |
| 5 | 会话摘要触发轮次 | 8 / 12 / 16 | **12 轮**（保留 8 = 窗口 + 缓冲 4）：每 5 轮才压缩一次，摘要成本摊薄；保留轮次必须 ≥ 窗口，否则中间会留空档 |
| 6 | I/O 日志是否记录对话全文？ | 全文 / 仅摘要 / 不记录 | **全文 + 90 天保留 + 仅 admin 可查**；排障和演示都需要原文，靠权限和保留期控风险 |
| 7 | 工作流默认"先出计划再执行"还是"直接执行"？ | 先计划 / 直接跑 | **先计划**（`WORKFLOW_AUTO_RUN=false`），避免误触发；熟练后可开自动 |
| 8 | 工作流本期是否支持并行步骤？ | 支持 / 串行 | **先串行**（确定性高、好排障），并行留到下一版（`WORKFLOW_PARALLEL` 预留） |
| 9 | 是否本期就做 HITL 人工确认门？ | 是 / 否 | **是**（P1，但只做"继续/跳过/取消"三态，不做复杂审批流） |
| 10 | 日志保留期 | 30 / 90 / 180 天 | **90 天**，配置项 `IO_LOG_RETENTION_DAYS` |
| 11 | WP5 附加项本期做哪些？ | 全做 / 只做 1-2 个 / 不做 | **只做 WP5-1（时间线）与 WP5-2（模型分级）**，其余下一版 |

---

## 附录 A：一次完整请求的目标形态（四块能力串起来）

```
用户（member）："这个会话里报表都用万元。先查一下 Q3 订单总额，再和知识库里 Q3 季报的营收对一下，最后给我结论。"

① 记忆：抽取 constraint「金额单位=万元」→ session_memory（绑定当前 session，换会话即失效）
② 意图：workflow（"先…再…最后"）
③ 计划：
   1) biz_query：Q3 订单汇总（requires_confirm=false）
   2) knowledge_search：Q3 季报营收
   3) general_chat：对比 + 结论（依赖 1,2）
④ 执行：每步 running/done 帧 → 前端 PlanCard 进度
⑤ 生成：注入块「本次会话已确认 —— 金额单位：万元」，按此口径输出对比表 + 差异说明
⑥ 落库：chat_record（intent=workflow, answer_mode=workflow, answer_digest 同步生成）
⑦ 日志：chat_io_log（1 条 ok，含 3 步 tool_chain）+ llm_call_log（plan/answer/会话事实抽取各 1 条）
⑧ 前端：徽标「任务」+ 计划卡 + 来源卡 + 复制/详情（trace_id）
⑨ 后续：done 之后异步判断是否触发摘要（未摘要轮次 > 12 才压），不占首字延迟
```
