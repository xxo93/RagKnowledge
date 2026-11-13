"""运行时配置中心：参数落库 + 进程内缓存，可在线热生效。

设计要点：
1. **只存被显式改过的项**：`app_config` 里没有的 key 一律回退 `.env` / 代码默认值，
   因此「恢复默认」= 删除该行，不会把默认值写死进库。
2. **取值优先级**：`app_config` > `.env`（即 `settings`）> 代码默认。`ParamSpec.default`
   直接取 `settings` 的当前值，保证 `.env` 调过之后「恢复默认」回到的是 .env 语义。
3. **热生效**：进程内缓存 TTL 5s；保存接口写库后立即刷新缓存，所以改完下一条请求就生效，
   不用重启（Windows 上 `run_dev.py` 是 `reload=False`，重启成本高）。
4. **同步读取**：`llm.py` 的 `chat()` 是同步函数（在线程里跑），不能 await 数据库，
   因此提供 `get_cached()` 只读缓存；缓存由异步入口（检索、配置接口）负责刷新。
5. **数据库故障不阻断问答**：读缓存失败时退回默认值并只告警一次；写失败则明确报 50001。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from ..config import settings
from ..db import get_pool
from ..errors import BizError, ERR_PARAM, ERR_DB_UNAVAILABLE
from . import audit

logger = logging.getLogger("rag.app_config")

# 缓存有效期：多实例部署时，其他进程最迟 5s 后感知到变更
CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class ParamSpec:
    """一个可在配置中心调整的参数。"""

    key: str
    group: str
    label: str
    type: str  # int / float / bool / str / enum
    default: Any
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    options: tuple[str, ...] = ()
    desc: str = ""
    effect: str = "下次请求生效"


GROUP_LABELS: dict[str, str] = {
    "retrieval": "检索",
    "generation": "生成",
    "general": "通用对话",
    "memory": "会话记忆",
    "session": "会话管理",
    "quota": "配额与限流",
    "upload": "上传",
    "fallback": "兜底",
    "biz": "业务库",
    "logs": "运行日志",
    "workflow": "任务编排",
}

# 已接入的参数分组：检索 / 生成（M1）+ 上传（M2）+ 兜底（M3）。
# 业务库查询相关参数在 M4/M5 接入，避免出现"配了不生效"的开关。
PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        key="RETRIEVAL_MIN_SCORE",
        group="retrieval",
        label="相似度阈值",
        type="float",
        default=float(settings.RETRIEVAL_MIN_SCORE),
        min_value=0.0,
        max_value=1.0,
        desc="作用于余弦相似度。低于该值的召回不展示、不参与生成、不进来源卡片；0 表示不过滤。重排分只用于排序，不做绝对过滤。",
    ),
    ParamSpec(
        key="RETRIEVAL_CANDIDATES",
        group="retrieval",
        label="候选召回数",
        type="int",
        default=int(settings.RETRIEVAL_CANDIDATES),
        min_value=5,
        max_value=50,
        desc="向量召回的候选条数，即重排的输入规模。建议不小于展示 Top-K。",
    ),
    ParamSpec(
        key="RERANK_ENABLED",
        group="retrieval",
        label="重排开关",
        type="bool",
        default=bool(settings.RERANK_ENABLED),
        desc="开启后按 rerank 分数排序；关闭或调用失败时按相似度排序。",
    ),
    ParamSpec(
        key="RERANK_TOP_N",
        group="retrieval",
        label="重排保留数",
        type="int",
        default=int(settings.RERANK_TOP_N),
        min_value=1,
        max_value=20,
        desc="重排后保留的条数，再按阈值过滤并截断到展示 Top-K。",
    ),
    ParamSpec(
        key="TOP_K",
        group="retrieval",
        label="展示 Top-K",
        type="int",
        default=int(settings.TOP_K),
        min_value=1,
        max_value=10,
        desc="最终交给模型生成并展示来源的片段条数。",
    ),
    ParamSpec(
        key="QUERY_REWRITE_ENABLED",
        group="retrieval",
        label="多轮 query 改写",
        type="bool",
        default=bool(settings.QUERY_REWRITE_ENABLED),
        desc="仅知识库链路生效。开启后短追问/代词句用 LLM_SMALL_MODEL 改写成独立检索句，只用于检索与 rerank；原句仍用于意图判断、回答与展示。关闭后与旧版完全一致。",
    ),
    ParamSpec(
        key="QUERY_REWRITE_TIMEOUT_MS",
        group="retrieval",
        label="改写超时(ms)",
        type="int",
        default=int(settings.QUERY_REWRITE_TIMEOUT_MS),
        min_value=1000,
        max_value=15000,
        desc="query 改写调用超时；超时、报错或结果不合法时回落原句，不影响回答。",
    ),
    ParamSpec(
        key="LLM_TEMPERATURE",
        group="generation",
        label="温度 temperature",
        type="float",
        default=float(settings.LLM_TEMPERATURE),
        min_value=0.0,
        max_value=1.5,
        desc="越低越稳定保守，越高越发散。RAG 问答建议 0.2–0.5。",
    ),
    ParamSpec(
        key="LLM_MAX_TOKENS",
        group="generation",
        label="最大输出 tokens",
        type="int",
        default=int(settings.LLM_MAX_TOKENS),
        min_value=128,
        max_value=4096,
        desc="单次回答的 token 上限，过小会导致回答被截断。",
    ),
    ParamSpec(
        key="LLM_ROUTING_ENABLED",
        group="generation",
        label="模型分级路由",
        type="bool",
        default=bool(settings.LLM_ROUTING_ENABLED),
        desc="开启后，摘要/会话事实/标题等轻任务可走小模型；关闭则全部走主模型。",
    ),
    ParamSpec(
        key="LLM_SMALL_PURPOSES",
        group="generation",
        label="走小模型的用途",
        type="str",
        default=str(settings.LLM_SMALL_PURPOSES),
        desc="逗号分隔，例如 summary,memory_extract,title；回答与工作流规划始终走主模型。",
    ),
    ParamSpec(
        key="UPLOAD_CHUNK_MAX_MB",
        group="upload",
        label="单片大小上限（MB）",
        type="int",
        default=int(settings.UPLOAD_CHUNK_MAX_MB),
        min_value=1,
        max_value=512,
        desc="分块上传时单个分片允许的最大体积；前端可选的分块大小不会超过该值。",
    ),
    ParamSpec(
        key="UPLOAD_TOTAL_MAX_MB",
        group="upload",
        label="单文件总大小上限（MB）",
        type="int",
        default=int(settings.UPLOAD_TOTAL_MAX_MB),
        min_value=1,
        max_value=512,
        desc="分块上传合并后的文件总大小上限；必须不小于单片上限。",
    ),
    ParamSpec(
        key="UPLOAD_DEFAULT_CHUNK_MB",
        group="upload",
        label="默认分块大小（MB）",
        type="int",
        default=int(settings.UPLOAD_DEFAULT_CHUNK_MB),
        min_value=1,
        max_value=32,
        desc="知识库页首次打开时的分块大小默认值；用户仍可自行调整，不能超过单片上限。",
    ),
    ParamSpec(
        key="UPLOAD_DEFAULT_CONCURRENCY",
        group="upload",
        label="默认并发数",
        type="int",
        default=int(settings.UPLOAD_DEFAULT_CONCURRENCY),
        min_value=1,
        max_value=6,
        desc="知识库页首次打开时的分片并发数默认值（1–6）。",
    ),
    ParamSpec(
        key="UPLOAD_CHUNK_STALE_HOURS",
        group="upload",
        label="分片过期清理（小时）",
        type="int",
        default=int(settings.UPLOAD_CHUNK_STALE_HOURS),
        min_value=1,
        max_value=168,
        desc="超过该时长仍未合并的分片目录会在下次上传时被清理。",
    ),
    ParamSpec(
        key="FALLBACK_MODE",
        group="fallback",
        label="知识不足时的兜底方式",
        type="enum",
        default=str(settings.FALLBACK_MODE),
        options=("web", "guide", "contact_admin"),
        desc="web＝联网检索后回答（会标注非内部知识）；guide＝引导用户补充描述重问；contact_admin＝直接提示联系管理员。联网失败时无论选哪种都回落 contact_admin。",
    ),
    ParamSpec(
        key="FALLBACK_MIN_HITS",
        group="fallback",
        label="判定知识不足的命中条数",
        type="int",
        default=int(settings.FALLBACK_MIN_HITS),
        min_value=1,
        max_value=5,
        desc="阈值过滤后有效命中少于该条数即判定内部知识不足、走兜底，不再用残片作答。设为 1 表示只要有命中就正常回答。",
    ),
    ParamSpec(
        key="WEB_SEARCH_ENABLED",
        group="fallback",
        label="允许联网检索",
        type="bool",
        default=bool(settings.WEB_SEARCH_ENABLED),
        desc="关闭后 web 兜底模式不会真的联网，按“联网不可用”回落联系管理员；语义方向不变。",
    ),
    ParamSpec(
        key="WEB_SEARCH_PROVIDER",
        group="fallback",
        label="联网搜索来源",
        type="enum",
        default=str(settings.WEB_SEARCH_PROVIDER),
        options=("bailian", "bocha", "tavily"),
        desc="bailian＝阿里百炼（DashScope 联网问答，返回结构化来源）；bocha＝博查；tavily＝Tavily。切换后 Key 归属随之切换，密钥本身不在本页维护。",
    ),
    ParamSpec(
        key="WEB_SEARCH_MODEL",
        group="fallback",
        label="百炼联网模型",
        type="str",
        default=str(settings.WEB_SEARCH_MODEL),
        desc="仅 bailian 生效，需选支持 enable_search 的模型（默认 qwen-plus）；留空表示用默认模型。",
    ),
    ParamSpec(
        key="WEB_SEARCH_BASE_URL",
        group="fallback",
        label="搜索服务地址",
        type="str",
        default=str(settings.WEB_SEARCH_BASE_URL),
        desc="留空＝使用该供应商默认地址；百炼默认 https://dashscope.aliyuncs.com/api/v1。只有换私有网关/地域时才需要改。",
    ),
    ParamSpec(
        key="WEB_SEARCH_MAX_RESULTS",
        group="fallback",
        label="联网结果条数",
        type="int",
        default=int(settings.WEB_SEARCH_MAX_RESULTS),
        min_value=1,
        max_value=10,
        desc="参与来源展示与答案生成的最大网页条数。条数越多越全，但更慢、更占上下文。",
    ),
    ParamSpec(
        key="WEB_SEARCH_TIMEOUT",
        group="fallback",
        label="联网超时（秒）",
        type="float",
        default=float(settings.WEB_SEARCH_TIMEOUT),
        min_value=3.0,
        max_value=60.0,
        desc="超过该时长按“联网不可用”回落联系管理员话术。百炼是“搜索+生成”一次完成，建议不低于 15 秒。",
    ),
    ParamSpec(
        key="FALLBACK_TEXT_GUIDE",
        group="fallback",
        label="引导重问话术",
        type="str",
        default=str(settings.FALLBACK_TEXT_GUIDE),
        desc="guide 模式返回给用户的话术，建议包含“请补充哪类背景信息”的引导。",
    ),
    ParamSpec(
        key="FALLBACK_TEXT_CONTACT",
        group="fallback",
        label="联系管理员话术",
        type="str",
        default=str(settings.FALLBACK_TEXT_CONTACT),
        desc="contact_admin 模式与联网失败时统一返回的话术。",
    ),
    # ---------- 会话记忆（v3 / WP1）----------
    ParamSpec(
        key="HISTORY_WINDOW_ROUNDS",
        group="memory",
        label="原文窗口轮数",
        type="int",
        default=int(settings.HISTORY_WINDOW_ROUNDS),
        min_value=1,
        max_value=20,
        desc="每轮注入最近多少轮原文（一问一答为一轮）；超出预算时从最旧的一轮开始砍。",
    ),
    ParamSpec(
        key="HISTORY_MAX_TOKENS",
        group="memory",
        label="上下文 token 预算",
        type="int",
        default=int(settings.HISTORY_MAX_TOKENS),
        min_value=500,
        max_value=8000,
        desc="事实 + 摘要 + 原文的总预算（保守估算）。超预算优先砍原文，事实与摘要始终保留。",
    ),
    ParamSpec(
        key="HISTORY_TURN_MAX_TOKENS",
        group="memory",
        label="单轮 token 上限",
        type="int",
        default=int(settings.HISTORY_TURN_MAX_TOKENS),
        min_value=100,
        max_value=2000,
        desc="单轮 user+assistant 摘要体超过该值会再压缩，避免单条长回答挤占窗口。",
    ),
    ParamSpec(
        key="SUMMARY_ENABLED",
        group="memory",
        label="滚动摘要开关",
        type="bool",
        default=bool(settings.SUMMARY_ENABLED),
        desc="关闭后不做自动摘要，只保留最近窗口原文。",
    ),
    ParamSpec(
        key="SUMMARY_TRIGGER_ROUNDS",
        group="memory",
        label="摘要触发轮数",
        type="int",
        default=int(settings.SUMMARY_TRIGGER_ROUNDS),
        min_value=6,
        max_value=60,
        desc="未摘要轮次超过该值才压缩；必须大于保留轮数。",
    ),
    ParamSpec(
        key="SUMMARY_KEEP_ROUNDS",
        group="memory",
        label="摘要保留轮数",
        type="int",
        default=int(settings.SUMMARY_KEEP_ROUNDS),
        min_value=2,
        max_value=40,
        desc="最近多少轮保持原文、不进入摘要；必须不小于原文窗口轮数。",
    ),
    ParamSpec(
        key="SESSION_FACT_ENABLED",
        group="memory",
        label="会话事实开关",
        type="bool",
        default=bool(settings.SESSION_FACT_ENABLED),
        desc="关闭后不再自动抽取事实，已有事实仍会注入。临时会话对该会话整体关闭记忆。",
    ),
    ParamSpec(
        key="SESSION_FACT_EVERY_ROUNDS",
        group="memory",
        label="事实抽取间隔（轮）",
        type="int",
        default=int(settings.SESSION_FACT_EVERY_ROUNDS),
        min_value=1,
        max_value=20,
        desc="每累计 N 轮抽取一次事实；调小更及时但会增加模型调用。",
    ),
    ParamSpec(
        key="SESSION_FACT_MIN_CONFIDENCE",
        group="memory",
        label="事实置信度门槛",
        type="float",
        default=float(settings.SESSION_FACT_MIN_CONFIDENCE),
        min_value=0.0,
        max_value=1.0,
        desc="低于该置信度的抽取结果不入库，避免把含糊信息沉淀成长期记忆。",
    ),
    # ---------- 会话管理（M8 / WP5-9）----------
    ParamSpec(
        key="SESSION_ARCHIVE_DAYS",
        group="session",
        label="自动归档天数",
        type="int",
        default=int(settings.SESSION_ARCHIVE_DAYS),
        min_value=1,
        max_value=365,
        desc="最近一次对话距今超过该天数的会话自动归档，从主列表移入「已归档」；置顶会话不归档。",
    ),
    ParamSpec(
        key="SESSION_LIST_PAGE_SIZE",
        group="session",
        label="会话列表页大小",
        type="int",
        default=int(settings.SESSION_LIST_PAGE_SIZE),
        min_value=10,
        max_value=200,
        desc="会话列表单次加载条数；200+ 会话时配合虚拟滚动，避免首屏渲染过多节点。",
    ),
    ParamSpec(
        key="SESSION_TITLE_LLM_ENABLED",
        group="session",
        label="会话标题小模型",
        type="bool",
        default=bool(settings.SESSION_TITLE_LLM_ENABLED),
        desc="首轮问答后用 LLM_SMALL_MODEL 提炼 8–16 字标题；关闭时完全沿用规则截断。失败自动回落规则标题。",
    ),
    # ---------- 通用对话（M2 / WP2）----------
    ParamSpec(
        key="ALLOW_GENERAL_CHAT",
        group="general",
        label="允许通用对话",
        type="bool",
        default=bool(settings.ALLOW_GENERAL_CHAT),
        desc="关闭后通用意图（写代码/写作/解释/闲聊）回落引导话术，不调用模型，链路与旧版一致；用于合规一键关闭。",
    ),
    ParamSpec(
        key="GENERAL_TEMPERATURE",
        group="general",
        label="通用回答温度",
        type="float",
        default=float(settings.GENERAL_TEMPERATURE),
        min_value=0.0,
        max_value=1.5,
        desc="仅作用于通用对话直答；知识库回答仍使用生成组温度（默认 0.3），避免检索答案发散。",
    ),
    ParamSpec(
        key="INTERNAL_TERMS",
        group="general",
        label="内部实体词表",
        type="str",
        default=str(settings.INTERNAL_TERMS),
        desc="逗号分隔。命中这些词的问题即使被判为通用，也会强制改走知识库检索，防止通用能力抢答内部事实；文档标题会作为额外词表自动参与匹配。",
    ),
    # ---------- 业务库查询（M4）----------
    ParamSpec(
        key="BIZ_QUERY_INLINE_MAX_ROWS",
        group="biz",
        label="内联展示行数",
        type="int",
        default=int(settings.BIZ_QUERY_INLINE_MAX_ROWS),
        min_value=10,
        max_value=100,
        desc="结果不超过该行数时在对话里直接展示表格；超过则只给汇总 + CSV 下载链接（可重复下载，行数受硬上限约束）。",
    ),
    ParamSpec(
        key="BIZ_QUERY_HARD_MAX_ROWS",
        group="biz",
        label="单次查询硬上限",
        type="int",
        default=int(settings.BIZ_QUERY_HARD_MAX_ROWS),
        min_value=100,
        max_value=3000,
        desc="任何意图的实际行数都不会超过该值；意图注册表里的 max_rows 与它取小（默认 3000）。",
    ),
    ParamSpec(
        key="BIZ_QUERY_TIMEOUT",
        group="biz",
        label="语句超时（秒）",
        type="float",
        default=float(settings.BIZ_QUERY_TIMEOUT),
        min_value=1.0,
        max_value=30.0,
        desc="单条查询的执行超时，超时按参数错误提示用户缩小范围，不会拖住连接。",
    ),
    ParamSpec(
        key="BIZ_QUERY_RATE_PER_MIN",
        group="biz",
        label="每分钟次数上限",
        type="int",
        default=int(settings.BIZ_QUERY_RATE_PER_MIN),
        min_value=1,
        max_value=60,
        desc="按用户维度限流，超过后返回“查询过于频繁”，防止误用与刷接口。",
    ),
    ParamSpec(
        key="BIZ_EXPORT_TTL_MINUTES",
        group="biz",
        label="导出链接有效期（分钟）",
        type="int",
        default=int(settings.BIZ_EXPORT_TTL_MINUTES),
        min_value=0,
        max_value=720,
        desc="CSV 下载链接有效期，0 表示永不过期；链接绑定当前账号，可在有效期内重复下载，过期后返回失效提示。",
    ),
    # ---------- 输入输出运行日志（v3 / WP3）----------
    ParamSpec(
        key="IO_LOG_ENABLED",
        group="logs",
        label="I/O 日志开关",
        type="bool",
        default=bool(settings.IO_LOG_ENABLED),
        desc="关闭后不再记录问答输入输出与 LLM 调用日志；不影响问答本身，仅影响排障与统计。",
        effect="下一轮问答生效",
    ),
    ParamSpec(
        key="IO_LOG_RETENTION_DAYS",
        group="logs",
        label="保留天数",
        type="int",
        default=int(settings.IO_LOG_RETENTION_DAYS),
        min_value=1,
        max_value=365,
        desc="超过该天数的运行日志由每日清理任务物理删除；用户删除自己的日志优先于保留期。",
        effect="下一轮清理生效",
    ),
    ParamSpec(
        key="IO_LOG_QUEUE_MAX",
        group="logs",
        label="写入队列容量",
        type="int",
        default=int(settings.IO_LOG_QUEUE_MAX),
        min_value=100,
        max_value=5000,
        desc="异步写库队列长度；队满时丢弃并计数告警，绝不让日志拖慢首字。修改后需重启生效。",
        effect="重启后生效",
    ),
    # ---------- 工作流 Agent（v3 / WP4）----------
    ParamSpec(
        key="WORKFLOW_MAX_STEPS",
        group="workflow",
        label="最大步数",
        type="int",
        default=int(settings.WORKFLOW_MAX_STEPS),
        min_value=1,
        max_value=10,
        desc="计划超过该步数直接拒绝并提示拆分任务；验收基线为 6 步。",
    ),
    ParamSpec(
        key="WORKFLOW_STEP_TIMEOUT",
        group="workflow",
        label="单步超时（秒）",
        type="int",
        default=int(settings.WORKFLOW_STEP_TIMEOUT),
        min_value=5,
        max_value=300,
        desc="单步执行超时；超时按可重试失败处理，重试 1 次后再降级或改计划。",
    ),
    ParamSpec(
        key="WORKFLOW_TASK_TIMEOUT",
        group="workflow",
        label="整体超时（秒）",
        type="int",
        default=int(settings.WORKFLOW_TASK_TIMEOUT),
        min_value=10,
        max_value=900,
        desc="整任务超时；与时间预算共同决定何时中止并输出部分完成报告。",
    ),
    ParamSpec(
        key="WORKFLOW_AUTO_RUN",
        group="workflow",
        label="计划后自动执行",
        type="bool",
        default=bool(settings.WORKFLOW_AUTO_RUN),
        desc="关闭（默认）时只生成计划，用户点「开始执行」才跑；开启后计划通过校验即自动执行。",
        effect="下一任务生效",
    ),
    ParamSpec(
        key="WORKFLOW_PARALLEL",
        group="workflow",
        label="无依赖步骤并行",
        type="bool",
        default=bool(settings.WORKFLOW_PARALLEL),
        desc="总开关。开启后按 depends_on 构建 DAG，同一层无依赖步骤并行；关闭等同并发 1，行为与 M8 串行一致。",
        effect="下一任务生效",
    ),
    ParamSpec(
        key="WORKFLOW_MAX_PARALLEL",
        group="workflow",
        label="最大并行步骤数",
        type="int",
        default=int(settings.WORKFLOW_MAX_PARALLEL),
        min_value=1,
        max_value=4,
        desc="并行执行上限，默认 2；设为 1 时回到串行。仅在“无依赖步骤并行”开启时生效。",
        effect="下一任务生效",
    ),
    ParamSpec(
        key="WORKFLOW_LLM_REFLECT",
        group="workflow",
        label="LLM 反思",
        type="bool",
        default=bool(settings.WORKFLOW_LLM_REFLECT),
        desc="开启后每步结束用一次 LLM 判断结果是否满足 expected；默认用规则，避免成本与延迟。",
        effect="下一任务生效",
    ),
    ParamSpec(
        key="WORKFLOW_MAX_REPLANS",
        group="workflow",
        label="改计划次数上限",
        type="int",
        default=int(settings.WORKFLOW_MAX_REPLANS),
        min_value=0,
        max_value=3,
        desc="失败或结果为空时尝试等价工具降级/改计划的次数上限。",
    ),
    ParamSpec(
        key="WORKFLOW_BUDGET_SECONDS",
        group="workflow",
        label="时间预算（秒）",
        type="int",
        default=int(settings.WORKFLOW_BUDGET_SECONDS),
        min_value=10,
        max_value=900,
        desc="任务预算中的 max_seconds；耗尽后任务置为 budget_exceeded 并返回已完成步骤。",
        effect="下一任务生效",
    ),
    ParamSpec(
        key="WORKFLOW_BUDGET_TOKENS",
        group="workflow",
        label="token 预算",
        type="int",
        default=int(settings.WORKFLOW_BUDGET_TOKENS),
        min_value=500,
        max_value=100000,
        desc="任务预算中展示的 token 上限；当前用于进度展示与超预算提示。",
        effect="下一任务生效",
    ),
    # ---------- 配额与限流（v3 / WP5-6）----------
    ParamSpec(
        key="QUOTA_ENABLED",
        group="quota",
        label="启用配额与限流",
        type="bool",
        default=bool(settings.QUOTA_ENABLED),
        desc="关闭后不检查日 token 上限与每分钟请求数；演示环境建议保持开启。",
    ),
    ParamSpec(
        key="QUOTA_DAILY_TOKEN_LIMIT",
        group="quota",
        label="每用户每日 token 上限",
        type="int",
        default=int(settings.QUOTA_DAILY_TOKEN_LIMIT),
        min_value=1,
        max_value=10000000,
        desc="按 chat_io_log 每轮总 token 的当日汇总（带 user_id 口径）；达到上限后当日拒绝新问答。",
    ),
    ParamSpec(
        key="QUOTA_RATE_LIMIT_PER_MINUTE",
        group="quota",
        label="每用户每分钟请求上限",
        type="int",
        default=int(settings.QUOTA_RATE_LIMIT_PER_MINUTE),
        min_value=1,
        max_value=600,
        desc="按自然分钟计数，超限请求同样计数（避免靠重试绕过）；被拒绝的请求会落一条 rejected 日志。",
    ),
)

SPECS: dict[str, ParamSpec] = {p.key: p for p in PARAMS}

# 进程内缓存
_cache: dict[str, Any] = {}
_overridden: set[str] = set()
_cache_loaded_at: float = 0.0
_cache_error_logged = False
# 显式加载标记：库里可能一条覆盖都没有，此时 _cache 为空字典，
# 不能用 `if _cache` 判断"是否加载过"，否则每次读配置都会重查数据库。
_cache_loaded = False


# ---------------------------------------------------------------- 存储层（测试可 monkeypatch）
async def _load_from_db() -> dict[str, Any]:
    """读取库里显式改过的参数；数据库不可用时由调用方决定如何降级。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SELECT key, value_json FROM app_config")
                rows = await cur.fetchall()
    return {row[0]: row[1] for row in rows}


async def _upsert_rows(items: Iterable[tuple[ParamSpec, Any]], actor_user_id: Optional[int]) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for spec, value in items:
                    await cur.execute(
                        """
                        INSERT INTO app_config
                            (key, value_json, group_name, value_type, default_json,
                             min_value, max_value, updated_by, updated_at)
                        VALUES
                            (%(key)s, %(value)s, %(group)s, %(type)s, %(default)s,
                             %(min)s, %(max)s, %(actor)s, now())
                        ON CONFLICT (key) DO UPDATE SET
                            value_json = EXCLUDED.value_json,
                            group_name = EXCLUDED.group_name,
                            value_type = EXCLUDED.value_type,
                            default_json = EXCLUDED.default_json,
                            min_value = EXCLUDED.min_value,
                            max_value = EXCLUDED.max_value,
                            updated_by = EXCLUDED.updated_by,
                            updated_at = now()
                        """,
                        {
                            "key": spec.key,
                            "value": Jsonb(value),
                            "group": spec.group,
                            "type": spec.type,
                            "default": Jsonb(spec.default),
                            "min": spec.min_value,
                            "max": spec.max_value,
                            "actor": actor_user_id,
                        },
                    )


async def _delete_rows(keys: Iterable[str]) -> None:
    keys = list(keys)
    if not keys:
        return
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM app_config WHERE key = ANY(%s)", (keys,))


# ---------------------------------------------------------------- 缓存与读取
def _default(key: str) -> Any:
    spec = SPECS.get(key)
    return spec.default if spec else None


async def refresh(force: bool = False) -> None:
    """按 TTL 刷新缓存；数据库异常时保留旧缓存并告警一次。"""
    global _cache_loaded_at, _cache_error_logged, _cache_loaded
    now = time.monotonic()
    if not force and _cache_loaded and (now - _cache_loaded_at) < CACHE_TTL_SECONDS:
        return
    try:
        rows = await _load_from_db()
    except Exception as exc:  # noqa: BLE001 - 读配置失败不得阻断问答
        if not _cache_error_logged:
            logger.warning("读取运行时配置失败，暂用 .env/默认值：%s", exc)
            _cache_error_logged = True
        _cache_loaded_at = now
        _cache_loaded = True
        return
    _cache.clear()
    _cache.update(rows)
    _overridden.clear()
    _overridden.update(rows.keys())
    _cache_loaded_at = now
    _cache_loaded = True
    _cache_error_logged = False


async def get_value(key: str) -> Any:
    """异步取有效值（先刷新缓存），供检索等异步链路使用。"""
    await refresh()
    return _cache.get(key, _default(key))


def get_cached(key: str) -> Any:
    """同步取有效值（只读缓存，不访问数据库），供 llm.py 等同步函数使用。"""
    return _cache.get(key, _default(key))


def runtime_value(key: str, fallback: Any) -> Any:
    """缓存已加载时返回运行时值，否则返回调用方给的 .env/代码默认。

    与 get_cached 的差别：注册表里的 default 是"导入时的快照"，
    而 .env 可能在导入后才变（测试里还会被 monkeypatch），同步链路要优先认 .env。
    """
    if _cache_loaded and key in _cache:
        return _cache[key]
    return fallback


def effective(key: str) -> Any:
    """同步别名，语义更贴近"当前生效值"。"""
    return get_cached(key)


# ---------------------------------------------------------------- 校验
def _coerce(spec: ParamSpec, raw: Any) -> Any:
    """严格校验并归一化；类型不符或越界统一抛 40001，杜绝静默夹取。"""
    if spec.type == "bool":
        if not isinstance(raw, bool):
            raise BizError(ERR_PARAM, f"{spec.label} 必须为布尔值（true / false）")
        return raw

    if spec.type in ("int", "float"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BizError(ERR_PARAM, f"{spec.label} 必须为数字")
        value = int(raw) if spec.type == "int" else float(raw)
        if spec.type == "int" and float(raw) != value:
            raise BizError(ERR_PARAM, f"{spec.label} 必须为整数")
        if spec.min_value is not None and value < spec.min_value:
            raise BizError(ERR_PARAM, f"{spec.label} 不能小于 {spec.min_value}")
        if spec.max_value is not None and value > spec.max_value:
            raise BizError(ERR_PARAM, f"{spec.label} 不能大于 {spec.max_value}")
        return value

    if spec.type == "enum":
        if not isinstance(raw, str) or raw not in spec.options:
            raise BizError(ERR_PARAM, f"{spec.label} 只能取 {', '.join(spec.options)}")
        return raw

    if not isinstance(raw, str):
        raise BizError(ERR_PARAM, f"{spec.label} 必须为字符串")
    value = raw.strip()
    if len(value) > 500:
        raise BizError(ERR_PARAM, f"{spec.label} 长度不能超过 500 字")
    return value


# ---------------------------------------------------------------- 对外读写
def snapshot() -> dict:
    """当前配置全量快照（含默认值、范围、是否被库覆盖），供配置页渲染。"""
    groups: list[dict] = []
    for group_key, group_label in GROUP_LABELS.items():
        items = []
        for spec in PARAMS:
            if spec.group != group_key:
                continue
            items.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "type": spec.type,
                    "default": spec.default,
                    "current": _cache.get(spec.key, spec.default),
                    "min": spec.min_value,
                    "max": spec.max_value,
                    "options": list(spec.options),
                    "desc": spec.desc,
                    "effect": spec.effect,
                    "overridden": spec.key in _overridden,
                }
            )
        if items:
            groups.append({"key": group_key, "label": group_label, "params": items})
    return {
        "groups": groups,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "priority": "app_config > .env > 代码默认",
    }


async def load_snapshot() -> dict:
    """异步版快照：先刷新缓存再返回。"""
    await refresh()
    return snapshot()


def _cross_validate(candidate: dict[str, Any]) -> None:
    """跨字段一致性校验：只在同一批或与当前生效值组合后判断，避免保存出自相矛盾的配置。

    当前约束：
    - 上传组：单片上限 ≤ 单文件总上限；默认分块大小 ≤ 单片上限与总上限；
    - 业务库组：内联展示行数 ≤ 单次查询硬上限。
    """
    def value(key: str) -> int:
        return int(candidate.get(key, _cache.get(key, _default(key))))

    total = value("UPLOAD_TOTAL_MAX_MB")
    chunk = value("UPLOAD_CHUNK_MAX_MB")
    default_chunk = value("UPLOAD_DEFAULT_CHUNK_MB")

    if chunk > total:
        raise BizError(ERR_PARAM, f"单片上限（{chunk}MB）不能大于单文件总上限（{total}MB）")
    if default_chunk > chunk:
        raise BizError(ERR_PARAM, f"默认分块大小（{default_chunk}MB）不能大于单片上限（{chunk}MB）")
    if default_chunk > total:
        raise BizError(ERR_PARAM, f"默认分块大小（{default_chunk}MB）不能大于单文件总上限（{total}MB）")

    inline = value("BIZ_QUERY_INLINE_MAX_ROWS")
    hard = value("BIZ_QUERY_HARD_MAX_ROWS")
    if inline > hard:
        raise BizError(ERR_PARAM, f"内联展示行数（{inline}）不能大于单次查询硬上限（{hard}）")

    # 会话记忆：保留轮数 ≥ 原文窗口，触发轮数 > 保留轮数（否则摘要永不触发或最近上下文丢失）
    window = value("HISTORY_WINDOW_ROUNDS")
    keep = value("SUMMARY_KEEP_ROUNDS")
    trigger = value("SUMMARY_TRIGGER_ROUNDS")
    if keep < window:
        raise BizError(ERR_PARAM, f"摘要保留轮数（{keep}）不能小于原文窗口轮数（{window}）")
    if trigger <= keep:
        raise BizError(ERR_PARAM, f"摘要触发轮数（{trigger}）必须大于保留轮数（{keep}）")


async def set_values(updates: dict[str, Any], actor_user_id: Optional[int] = None) -> dict:
    """批量保存：全部通过校验才落库，任一非法整批拒绝；保存后立即热生效。"""
    if not isinstance(updates, dict) or not updates:
        raise BizError(ERR_PARAM, "没有需要保存的配置项")
    unknown = [k for k in updates if k not in SPECS]
    if unknown:
        raise BizError(ERR_PARAM, f"不支持的配置项：{', '.join(sorted(unknown))}")

    validated: list[tuple[ParamSpec, Any]] = []
    for key, raw in updates.items():
        spec = SPECS[key]
        validated.append((spec, _coerce(spec, raw)))

    # 单字段校验通过后，再做跨字段一致性校验（整批拒绝，不落库）
    _cross_validate({spec.key: value for spec, value in validated})

    old = {spec.key: _cache.get(spec.key, spec.default) for spec, _ in validated}
    try:
        await _upsert_rows(validated, actor_user_id)
    except BizError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("保存运行时配置失败：%s", exc)
        raise BizError(ERR_DB_UNAVAILABLE, "配置保存失败：数据库不可用", http_status=503) from exc

    await refresh(force=True)

    changes = {
        spec.key: {"old": old[spec.key], "new": value}
        for spec, value in validated
        if old[spec.key] != value
    }
    if changes:
        await audit.log_operation(
            actor_user_id,
            audit.CONFIG_UPDATE,
            target_type="app_config",
            target_id=",".join(sorted(changes)),
            detail={"changes": changes},
        )
    return {"changed": sorted(changes), "snapshot": snapshot()}


async def reset(keys: Optional[Iterable[str]] = None, group: Optional[str] = None,
                actor_user_id: Optional[int] = None) -> dict:
    """恢复默认：删除库里的覆盖行，回退到 .env / 代码默认值。"""
    targets: list[str]
    if keys:
        targets = list(keys)
        unknown = [k for k in targets if k not in SPECS]
        if unknown:
            raise BizError(ERR_PARAM, f"不支持的配置项：{', '.join(sorted(unknown))}")
    elif group:
        if group not in GROUP_LABELS:
            raise BizError(ERR_PARAM, f"未知配置分组：{group}")
        targets = [spec.key for spec in PARAMS if spec.group == group]
    else:
        targets = [spec.key for spec in PARAMS]

    try:
        await _delete_rows(targets)
    except Exception as exc:  # noqa: BLE001
        logger.error("恢复默认配置失败：%s", exc)
        raise BizError(ERR_DB_UNAVAILABLE, "恢复默认失败：数据库不可用", http_status=503) from exc

    await refresh(force=True)
    await audit.log_operation(
        actor_user_id,
        audit.CONFIG_UPDATE,
        target_type="app_config",
        target_id="reset",
        detail={"reset_keys": targets},
    )
    return {"reset": targets, "snapshot": snapshot()}
