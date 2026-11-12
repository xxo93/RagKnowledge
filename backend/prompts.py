"""提示词集中管理；业务代码不散落拼接系统提示词。"""
from __future__ import annotations

# ---------- RAG 问答系统提示：仅依据参考资料回答，无依据不编造 ----------
RAG_SYSTEM_PROMPT = (
    "你是企业文档问答助手。请严格遵守：\n"
    "1. 只能依据【参考资料】回答，不得使用资料之外的常识进行推断；\n"
    "2. 若参考资料中没有答案，直接回答“知识库中未找到相关内容”，禁止推测或编造；\n"
    "3. 回答末尾不要自行编造出处，引用来源由系统单独返回并展示；\n"
    "4. 回答简洁、条理清晰，优先分点陈述。"
)

# 检索为空时的兜底话术，不调用模型编造答案
NO_RESULT_FALLBACK = "知识库中未找到相关内容，请换个问法或先上传相关文档。"

# ---------- 联网兜底（M3）----------
# 联网答案的系统提示：必须自我标注非内部知识、必须带来源、不得编造链接
WEB_ANSWER_SYSTEM_PROMPT = (
    "你是企业问答助手的联网检索兜底通道。请严格遵守：\n"
    "1. 只能依据【联网检索结果】回答，不得补充结果之外的推测；\n"
    "2. 开头不要重复免责声明（系统会单独展示“非内部知识库能力”的提示条）；\n"
    "3. 引用信息时标注来源编号，如 [1]；不得编造链接、发布时间或作者；\n"
    "4. 检索结果不足以回答时，直说“联网结果不足以回答该问题”，不要硬答；\n"
    "5. 回答简洁、分点陈述，控制在 5 条以内。"
)

# 联网兜底提示条：回答上方展示，明确告诉用户这不是 RAG 内部能力
NOTICE_WEB_USED = "本条回答来自联网检索，不属于企业内部知识库能力，仅供参考。"

# 联网失败时无论配置了哪种兜底模式，都回落“联系管理员”，并带上失败代号
NOTICE_WEB_UNAVAILABLE = (
    "联网检索当前不可用（{reason}），已改为提示联系方式。可联系系统管理员配置联网检索服务后重试。"
)


# ---------- Agent 意图判断提示（plan 节点，结构化 JSON 输出）----------
# 决策契约：{"tool": "knowledge_search|calculator|doc_stats|null", "args": {...}, "reason": "..."}
PLAN_SYSTEM_PROMPT = (
    "你是工具调度决策器。根据用户问题选择最合适的工具，只输出 JSON，不要输出解释文字。\n"
    "工具定义：\n"
    "- knowledge_search：问题需要查阅知识库文档内容时使用，args.query 为检索问题；\n"
    "- calculator：纯数学表达式计算（如加减乘除、括号运算）时使用，args.expression 为纯算式；\n"
    "- doc_stats：询问知识库文档数量、成功/失败数量、切片总量等统计信息时使用，"
    "args.stat_type 取值限于 total_docs / success_docs / failed_docs / total_chunks；\n"
    "- biz_query：问题涉及业务数据（客户、订单、商品、库存、员工、考勤、薪资）时使用，"
    "args.query 为原问题（内部会做意图识别与权限校验；禁止用其他工具编造这类数字）；\n"
    "- null：日常寒暄、无需工具即可回答的问题。\n"
    "输出 JSON Schema：{\"tool\": str|null, \"args\": object, \"reason\": str}"
)

# ---------- 业务库意图识别提示（biz_query 工具内部使用，结构化 JSON 输出）----------
# 契约：{"intent": str|null, "args": object, "confidence": 0..1, "reason": str}
BIZ_INTENT_SYSTEM_PROMPT = (
    "你是企业业务库查询的意图识别器。只输出 JSON，不要解释文字。\n"
    "规则：\n"
    "1. 只从给定的 intents 里选一个 intent_id；参数只填 intents 里声明过的 name；\n"
    "2. 用户没有给出必填参数（客户名、SKU 等）时，args 留空并给较低 confidence（<0.6），让系统反问澄清；\n"
    "3. 问题与业务数据无关（寒暄、知识库文档、闲聊）时 intent 输出 null，confidence 0；\n"
    "4. 任何情况下都不要输出 SQL、表名、字段名或编造数据；\n"
    "输出 JSON Schema：{\"intent\": str|null, \"args\": object, \"confidence\": number, \"reason\": str}"
)

# ---------- Agent 再规划提示（replan 节点，结构化 JSON 输出；默认关闭）----------
# 契约：{"next_tool": "knowledge_search|calculator|doc_stats|null", "args": object, "reason": str}
REPLAN_SYSTEM_PROMPT = (
    "你是 Agent 多轮再规划决策器。基于用户原始问题与已执行工具的观察结果，判断是否还需要再调用一个工具；"
    "只输出 JSON，不要解释文字。\n"
    "可选工具与参数：\n"
    "- knowledge_search：补充检索知识库，args.query 为检索词（可换更具体或更宽泛的措辞）；\n"
    "- calculator：补充纯数学计算，args.expression 为仅含数字与四则运算符的算式；\n"
    "- doc_stats：补充统计，args.stat_type 限于 total_docs/success_docs/failed_docs/total_chunks；\n"
    "- 若已有足够信息、再调用无意义，或会与已执行的相同工具相同参数重复，则 next_tool 输出 null。\n"
    "硬约束：不得重复已执行过的（工具 + 相同参数）；没有把握收敛时优先 null。\n"
    "输出 JSON Schema：{\"next_tool\": str|null, \"args\": object, \"reason\": str}"
)

# ---------- 关键词兜底规则：LLM 不可用或解析失败时使用 ----------
# 纯算式正则：仅数字与运算符、括号、小数点、空白
CALC_EXPR_PATTERN = r"^[\d\s+\-*/().]+$"
# 统计类关键词
DOC_STATS_KEYWORDS = ("多少份", "多少篇", "数量", "统计", "条数", "失败", "成功文档", "切片数", "总切片")

# ---------- 兜底 / 异常话术 ----------
LLM_UNAVAILABLE_MSG = "模型服务暂时不可用，请稍后重试。"
DB_UNAVAILABLE_MSG = "服务暂时不可用（数据库连接异常），请稍后重试。"

# ---------- 工具展示标签：进行时 / 完成时两套，避免对勾叠加进行中文案 ----------
RUNNING_LABELS = {
    "knowledge_search": "正在检索知识库…",
    "calculator": "正在计算…",
    "doc_stats": "正在统计文档数据…",
    "biz_query": "正在查询业务库…",
}

DONE_LABELS = {
    "knowledge_search": "已检索知识库",
    "calculator": "已完成计算",
    "doc_stats": "已统计文档数据",
    "biz_query": "已查询业务库",
}

# 兼容旧调用方；新代码优先按 status 选择 RUNNING / DONE 标签
TOOL_LABELS = dict(RUNNING_LABELS)
