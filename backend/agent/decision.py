"""Agent 意图决策（v3 / WP2：7 类意图 + 通用对话 + 内部事实优先保护）。

决策契约（Tool 层只认 tool，Chat 层再看 intent）::

    {
      "intent": "knowledge|biz_query|calculator|doc_stats|general|workflow|clarify",
      "tool": "knowledge_search|calculator|doc_stats|biz_query|None",
      "args": dict,
      "confidence": 0.0~1.0,
      "reason": str,
      "degraded_reason": str | None,   # general 被强制降级为 knowledge 的原因
      "suppressed_intent": str | None, # 显式"仅知识库"模式下被压制的通用意图
    }

路由优先级（需求 §4.2）：
① 用户显式模式（knowledge/general/biz/workflow）→ 最高；
② 会话内上下文延续（上一轮 general 的追问仍走 general；上一轮 knowledge 的追问仍走知识库）；
③ LLM 意图分类（结构化 JSON + confidence）；
④ 关键词兜底；
⑤ 默认 knowledge（保守）。

内部事实优先：即使意图是 general，命中内部实体词/内部指向词/知识库追问时强制改走 knowledge，
并在 `degraded_reason` 里记录原因（供日志与 done 帧观测）。显式"仅通用"模式不降级，但回答必须声明未使用内部资料。
"""
from __future__ import annotations

import json
import logging
import re

from ..prompts import CALC_EXPR_PATTERN, DOC_STATS_KEYWORDS
from ..config import settings

logger = logging.getLogger("rag.agent.decision")

# 意图 → 工具映射；None 表示不需要工具（通用直答/工作流/澄清）
INTENT_TOOL = {
    "knowledge": "knowledge_search",
    "biz_query": "biz_query",
    "calculator": "calculator",
    "doc_stats": "doc_stats",
    "general": None,
    "workflow": None,
    "clarify": None,
}

# stat_type 中文语义 -> 枚举白名单（与 vector_store._STAT_SQL / doc_stats_tool.ALLOWED_STATS 对齐）
_STAT_HINTS = (
    ("failed_docs", ("失败", "失败文档")),
    ("success_docs", ("成功", "成功文档")),
    ("total_chunks", ("切片", "chunk", "分段")),
)

# 业务库关键词：命中即进入 biz_query（内部还会做意图识别与权限/合规校验）
_BIZ_KEYWORDS = (
    "订单", "下单记录", "我的订单", "库存", "仓库", "sku", "客户资料", "客户信息",
    "手机号", "邮箱", "考勤", "打卡记录", "迟到记录", "薪资", "工资条", "奖金明细", "提成",
    "员工工资", "工资明细", "排班记录", "名下客户", "归属客户",
)

# 内部指向词：命中即视为"问企业内部事实"，通用能力不得抢答
_INTERNAL_PATTERN = re.compile(
    r"我们|我司|公司|本司|集团|内部|企业内部|这份文档|该文档|知识库|制度|流程|规范|"
    r"报销|审批|员工手册|组织架构|考勤|薪资|工资|部门|岗位职责|合同模板"
)

# 通用对话触发（代码/写作/解释/翻译/通用知识/闲聊）
_GENERAL_PATTERNS = (
    # 代码
    re.compile(r"写(一个|个|段)?\s*(python|java|javascript|js|sql|shell|bash|c\+\+|go|rust|代码|函数|脚本|程序|算法|正则|接口|组件)"),
    re.compile(r"(快速排序|二分查找|递归|爬虫|正则表达式|设计模式|算法题|leetcode)"),
    re.compile(r"(代码|函数|脚本|程序|组件).*(怎么写|如何写|示例|demo|帮我写)"),
    re.compile(r"(用|使用)\s*(python|java|javascript|js|sql|go|rust|c\+\+)\s*(写|实现|生成)"),
    # 写作 / 改写 / 翻译
    re.compile(r"(改写|润色|续写|扩写|缩写|起标题|写文案|写一段|写一篇|起草|总结成|翻译|译成|中译英|英译中)"),
    re.compile(r"(把|将)这段话.*(改|写|翻译|润色)"),
    # 解释 / 通用知识
    re.compile(r"(解释|说明|科普|讲讲|介绍)(一下)?(什么|为什么|原理|区别|概念|机制)?"),
    re.compile(r"(什么是|为什么|有什么区别|怎么理解|原理|机制|优缺点|对比一下)"),
    # 闲聊
    re.compile(r"^(你好|您好|hi|hello|嗨|在吗|谢谢|感谢|再见|拜拜)"),
    re.compile(r"(你是谁|你能做什么|你会什么|介绍一下你自己)"),
)

# 追问句式：用于"上一轮是 general，本轮继续改"的上下文延续
_FOLLOWUP_PATTERN = re.compile(
    r"^(那|这|它|他|她|再|还|继续|然后|另外|第[一二三四五六七八九十]+|还是|能不能|可以再)|"
    r"(再|更)?.{0,6}(短一点|长一点|简单点|详细点|换个|改成|调整|优化一下)"
)


def infer_stat_type(query: str) -> str:
    """从统计类问句推断统计枚举；无法细分时默认文档总数 total_docs。"""
    q = query
    for stat_type, hints in _STAT_HINTS:
        if any(h in q for h in hints):
            return stat_type
    return "total_docs"


def looks_general(query: str) -> bool:
    """关键词层判断是否像通用对话（写代码/写作/解释/通用知识/闲聊）。"""
    q = (query or "").strip()
    if not q:
        return False
    return any(p.search(q) for p in _GENERAL_PATTERNS)


def looks_followup(query: str) -> bool:
    """是否是承接上一轮的短追问（用于上下文延续）。"""
    q = (query or "").strip()
    if not q:
        return False
    if len(q) <= 30 and _FOLLOWUP_PATTERN.search(q):
        return True
    return False


def internal_guard(query: str, internal_terms: list[str] | None = None) -> str | None:
    """内部事实保护：返回降级原因；None 表示可以走通用。

    三个条件（需求 §4.2）：内部实体词典 / 内部指向词 / 知识库追问（后者由调用方结合上一轮判断）。
    """
    q = (query or "").strip()
    for term in internal_terms or []:
        term = (term or "").strip()
        if term and term in q:
            return f"命中内部实体「{term}」"
    match = _INTERNAL_PATTERN.search(q)
    if match:
        return f"含内部指向词「{match.group(0)}」"
    return None


def keyword_decision(query: str) -> dict:
    """纯规则兜底决策（不联网、无外部依赖）。

    1. 仅由数字/四则运算符/括号/空白组成且含数字 → calculator；
    2. 含统计关键词（多少份/统计/数量/条数/失败/切片数等）→ doc_stats；
    3. 含业务数据关键词 → biz_query；
    4. 通用触发词 → general（是否需要降级由 decide 统一处理）；
    5. 其余 → knowledge（保守默认）。
    """
    q = (query or "").strip()

    if re.match(CALC_EXPR_PATTERN, q) and any(ch.isdigit() for ch in q):
        return {
            "intent": "calculator",
            "tool": "calculator",
            "args": {"expression": q},
            "confidence": 0.95,
            "reason": "纯数字与四则运算，命中计算器关键词兜底",
        }

    if any(k in q for k in DOC_STATS_KEYWORDS):
        return {
            "intent": "doc_stats",
            "tool": "doc_stats",
            "args": {"stat_type": infer_stat_type(q)},
            "confidence": 0.9,
            "reason": "含统计关键词，命中文档统计工具兜底",
        }

    if any(k in q.lower() for k in _BIZ_KEYWORDS):
        return {
            "intent": "biz_query",
            "tool": "biz_query",
            "args": {"query": q},
            "confidence": 0.85,
            "reason": "含业务数据关键词，路由到业务库查询（内部做意图识别与闸门校验）",
        }

    if looks_general(q):
        return {
            "intent": "general",
            "tool": None,
            "args": {},
            "confidence": 0.7,
            "reason": "命中通用对话触发词（代码/写作/解释/闲聊）",
        }

    return {
        "intent": "knowledge",
        "tool": "knowledge_search",
        "args": {"query": q},
        "confidence": 0.6,
        "reason": "非算式、非统计、非通用，默认走知识库检索兜底",
    }


def _normalize_llm(data: dict, query: str) -> dict | None:
    """校验 LLM 决策：intent 与 tool 必须自洽；不自洽时以 intent 为准重建 tool。"""
    intent = str(data.get("intent") or "").strip()
    tool = data.get("tool")
    if intent not in INTENT_TOOL:
        # 兼容旧版只有 tool 的输出
        legacy_map = {
            "knowledge_search": "knowledge",
            "calculator": "calculator",
            "doc_stats": "doc_stats",
            "biz_query": "biz_query",
            None: "general",
        }
        intent = legacy_map.get(tool, "")
    if intent not in INTENT_TOOL:
        return None
    tool = INTENT_TOOL[intent]
    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    if intent == "knowledge":
        args = {"query": str(args.get("query") or query)}
    elif intent == "biz_query":
        args = {"query": str(args.get("query") or query)}
    elif intent == "calculator":
        args = {"expression": str(args.get("expression") or query)}
    elif intent == "doc_stats":
        args = {"stat_type": str(args.get("stat_type") or infer_stat_type(query))}
    else:
        args = {}
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "intent": intent,
        "tool": tool,
        "args": args,
        "confidence": confidence,
        "reason": data.get("reason") or "LLM 结构化意图判断",
    }


def llm_plan_decision(query: str) -> dict | None:
    """尝试 LLM 结构化决策；不可用或解析失败时返回 None，由调用方兜底。"""
    try:
        from ..llm import chat
        from ..prompts import PLAN_SYSTEM_PROMPT

        raw = chat(
            [
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            json_mode=True,
            purpose="plan",
        )
        data = json.loads(raw or "{}")
        return _normalize_llm(data, query)
    except Exception as exc:  # noqa: BLE001 - 决策降级，不击穿图
        logger.warning("LLM 意图决策失败，回落关键词兜底：%s", exc)
        return None


def _explicit_mode(query: str, mode: str, raw: dict) -> dict:
    """用户显式模式：覆盖意图，并记录被压制的通用意图。"""
    if mode == "knowledge":
        out = {
            "intent": "knowledge",
            "tool": "knowledge_search",
            "args": {"query": query},
            "confidence": 1.0,
            "reason": "用户选择「仅知识库」模式",
        }
        # 明确是通用问题却选了仅知识库：不回检索，直接告知模式限制（聊天层处理）
        if raw.get("intent") == "general":
            out["suppressed_intent"] = "general"
            out["reason"] = "用户选择「仅知识库」模式，通用问题被模式限制"
        return out
    if mode == "general":
        return {
            "intent": "general",
            "tool": None,
            "args": {},
            "confidence": 1.0,
            "reason": "用户选择「仅通用」模式",
            "forced_general": True,
        }
    if mode == "biz":
        return {
            "intent": "biz_query",
            "tool": "biz_query",
            "args": {"query": query},
            "confidence": 1.0,
            "reason": "用户选择「业务库」模式",
        }
    if mode == "workflow":
        return {
            "intent": "workflow",
            "tool": None,
            "args": {},
            "confidence": 1.0,
            "reason": "用户选择「任务」模式",
        }
    return dict(raw)


def decide(
    query: str,
    mode: str = "auto",
    last_intent: str | None = None,
    internal_terms: list[str] | None = None,
) -> dict:
    """统一决策入口：显式模式 → 上下文延续 → LLM → 关键词 → 默认 knowledge。

    `internal_terms` 由调用方（chat 路由）异步取回：配置中心词表 + 知识库文档标题派生词。
    """
    q = (query or "").strip()
    if not q:
        return keyword_decision(q)

    keyword = keyword_decision(q)

    # ① 显式模式优先
    if mode in ("knowledge", "general", "biz", "workflow"):
        return _explicit_mode(q, mode, keyword)

    # ② 上下文延续（短追问跟随上一轮链路，避免"再改短一点"被拉回知识库）
    if last_intent and looks_followup(q):
        if last_intent in ("general", "knowledge"):
            template = dict(keyword)
            template["intent"] = last_intent
            template["tool"] = INTENT_TOOL[last_intent]
            template["args"] = {"query": q} if last_intent == "knowledge" else {}
            template["confidence"] = 0.8
            template["reason"] = f"承接上一轮 {last_intent} 的追问"
            return template

    # ③ LLM 意图分类
    decision = None
    if settings.LLM_PLAN_ENABLED:
        decision = llm_plan_decision(q)
    # ④ 关键词兜底
    if decision is None:
        decision = keyword

    # ⑤ 内部事实优先：general 被强制降级为 knowledge
    if decision.get("intent") == "general":
        reason = internal_guard(q, internal_terms)
        if reason is None and last_intent == "knowledge" and looks_followup(q):
            reason = "承接上一轮知识库追问"
        if reason:
            return {
                "intent": "knowledge",
                "tool": "knowledge_search",
                "args": {"query": q},
                "confidence": max(float(decision.get("confidence") or 0), 0.6),
                "reason": f"通用意图被内部事实保护降级：{reason}",
                "degraded_reason": reason,
            }

    return decision
