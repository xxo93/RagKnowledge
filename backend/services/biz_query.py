"""业务库查询服务（M4）。

结构：意图注册表（`backend/biz_intents.json`） + 五层闸门 + 服务端脱敏 + 审计。

五层闸门（逐层拒绝，错误码沿用全局约定）：
① 意图识别：关键词预筛 → LLM 结构化输出（confidence < 0.6 或参数缺失 → 反问澄清）；
② 权限：角色级 allowed_roles；member 只能走注册表里的 `sql_self`（强制绑定 user_id），
   绝不拼姓名参数，从结构上排除"查别人数据"；
③ 合规：只允许单条 SELECT；表/列/JOIN 全部来自注册表；参数一律 %s 绑定；
   禁 `SELECT *`、禁系统表、禁注释与分号、语句超时、每用户限流；
④ 数据边界：默认时间范围、行数上限（内联 100 / 硬上限 1000）、敏感字段服务端脱敏；
⑤ 返回：≤上限内联表格，超过只给汇总 + 截断提示（导出链接属 M5）。

设计取舍：
- SQL 模板放在注册表文件里（部署期维护），运行时只做参数绑定，模型无法自造 SQL；
- 本模块不抛 BizError 给 Agent，工具调用统一返回 `{ok, code, message, table}`，便于前端按帧展示；
  仅管理/直达接口用 `query_or_raise` 转换错误码。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from . import app_config, audit, export_store

logger = logging.getLogger("rag.biz_query")

_INTENTS_FILE = Path(__file__).resolve().parent.parent / settings.BIZ_INTENTS_FILE
_registry_cache: Optional[dict] = None
_registry_mtime: float = -1.0
_call_log: dict[int, list[float]] = {}

# 合规红线：出现任一即判非法（校验的是"最终 SQL"，模板本身也过一遍）
_FORBIDDEN_SQL = (
    re.compile(r";"),
    re.compile(r"--"),
    re.compile(r"/\*"),
    re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|vacuum)\b", re.I),
    re.compile(r"\b(pg_[a-z_]+|information_schema|pg_catalog)\b", re.I),
    re.compile(r"select\s+\*", re.I),
    re.compile(r"\bunion\b", re.I),
)
_TABLE_REF = re.compile(r"\b(?:biz_a|biz_b)\.([a-z_][a-z0-9_]*)\b", re.I)
_SCHEMA_TABLE_REF = re.compile(r"\b(biz_a|biz_b)\.([a-z_][a-z0-9_]*)\b", re.I)
_ALLOWED_SCHEMAS = ("biz_a", "biz_b")

CAPABILITY_HINT = (
    "业务库当前支持这几类查询：按客户查订单、按 SKU 查库存、查客户资料、"
    "查员工考勤、查员工薪资、按员工查其名下客户订单、近 N 天订单列表（大结果可导出 CSV）。"
    "请说明对象与范围，例如「查客户张三最近90天的订单」。"
)


# ---------------- 注册表 ----------------
def _validate_intent(raw: dict) -> dict:
    """校验单条意图定义；缺字段或引用越界直接抛 ValueError（启动即失败，不带病运行）。"""
    required = (
        "intent_id", "title", "examples", "schema", "tables", "sql",
        "params", "allowed_roles", "max_rows", "columns",
    )
    for key in required:
        if key not in raw:
            raise ValueError(f"意图缺少字段 {key}: {raw.get('intent_id')}")
    if raw["schema"] not in _ALLOWED_SCHEMAS:
        raise ValueError(f"schema 非法: {raw['schema']}")
    # 跨 schema 意图：声明 schemas 列表（可含 biz_a / biz_b），未声明时视为单 schema
    schemas = raw.get("schemas") or [raw["schema"]]
    for schema in schemas:
        if schema not in _ALLOWED_SCHEMAS:
            raise ValueError(f"意图 {raw['intent_id']} 声明了非法 schema: {schema}")
    if raw["schema"] not in schemas:
        raise ValueError(f"意图 {raw['intent_id']} 的 schema 未包含在 schemas 中")
    sql_all = raw["sql"] + " " + raw.get("sql_self", "")
    for schema, table in _SCHEMA_TABLE_REF.findall(sql_all):
        if schema.lower() not in {s.lower() for s in schemas}:
            raise ValueError(f"意图 {raw['intent_id']} 引用了未声明的 schema: {schema}.{table}")
    for table in raw["tables"]:
        if table not in _TABLE_REFS(sql_all):
            raise ValueError(f"意图 {raw['intent_id']} 使用了未声明表 {table}")
    if "%(limit)s" not in raw["sql"]:
        raise ValueError(f"意图 {raw['intent_id']} 的 SQL 必须带 %(limit)s 限制行数")
    if "sql_self" in raw and "%(user_id)s" not in raw["sql_self"]:
        raise ValueError(f"意图 {raw['intent_id']} 的 sql_self 必须绑定 %(user_id)s")
    if "sql_self" in raw and "member_self" not in raw["allowed_roles"]:
        raise ValueError(f"意图 {raw['intent_id']} 有 sql_self 但未允许 member_self")
    for col in raw["columns"]:
        if "key" not in col or "title" not in col:
            raise ValueError(f"意图 {raw['intent_id']} 的 columns 定义不完整")
    for param in raw["params"]:
        if param.get("name") not in ("customer_name", "sku", "employee_name", "since_days", "period"):
            raise ValueError(f"意图 {raw['intent_id']} 含未支持的参数 {param.get('name')}")
    return raw


def _TABLE_REFS(sql: str) -> set[str]:
    return {m.group(1).lower() for m in _TABLE_REF.finditer(sql)}


def load_registry(force: bool = False) -> dict:
    """加载并校验注册表；文件变更后自动重载（改 JSON 不必重启）。"""
    global _registry_cache, _registry_mtime
    try:
        mtime = _INTENTS_FILE.stat().st_mtime
    except OSError as exc:
        raise ValueError(f"意图注册表不存在：{_INTENTS_FILE.name}") from exc
    if _registry_cache is not None and not force and mtime == _registry_mtime:
        return _registry_cache

    data = json.loads(_INTENTS_FILE.read_text(encoding="utf-8"))
    intents = [_validate_intent(item) for item in (data.get("intents") or [])]
    ids = [item["intent_id"] for item in intents]
    if len(ids) != len(set(ids)):
        raise ValueError("意图注册表存在重复 intent_id")
    index = {item["intent_id"]: item for item in intents}
    _registry_cache = {"version": data.get("version"), "global_limits": data.get("global_limits") or {}, "intents": index}
    _registry_mtime = mtime
    logger.info("意图注册表已加载：%d 个意图", len(index))
    return _registry_cache


def list_intents() -> list[dict]:
    """只读列表（供 /api/biz/intents 与 /帮助 文案）。"""
    reg = load_registry()
    return [
        {
            "intent_id": item["intent_id"],
            "title": item["title"],
            "examples": item["examples"],
            "schema": item["schema"],
            "tables": item["tables"],
            "allowed_roles": item["allowed_roles"],
            "max_rows": item["max_rows"],
            "params": item["params"],
            "columns": item["columns"],
        }
        for item in reg["intents"].values()
    ]


def get_intent(intent_id: str) -> Optional[dict]:
    return load_registry()["intents"].get(intent_id)


def _cfg(key: str, fallback: Any) -> Any:
    """读配置中心当前值（同步），未加载/失败时退回 .env 默认值。"""
    try:
        value = app_config.get_cached(key)
        return fallback if value is None else value
    except Exception:  # noqa: BLE001
        return fallback


def _limits() -> dict:
    reg = load_registry()["global_limits"]
    return {
        "inline": min(int(reg.get("inline_max_rows") or 100), int(_cfg("BIZ_QUERY_INLINE_MAX_ROWS", settings.BIZ_QUERY_INLINE_MAX_ROWS))),
        "hard": min(int(reg.get("hard_max_rows") or 1000), int(_cfg("BIZ_QUERY_HARD_MAX_ROWS", settings.BIZ_QUERY_HARD_MAX_ROWS))),
        "default_days": int(reg.get("default_time_range_days") or 90),
    }


def is_ready() -> bool:
    """业务库能力是否可用：注册表可加载 + 有可用的 DSN。"""
    try:
        load_registry()
    except Exception:  # noqa: BLE001
        return False
    return bool(dsn())


def dsn() -> str:
    """只读 DSN：优先专用只读账号，未配置时复用主库 DSN（仍在 READ ONLY 事务里跑）。"""
    return (settings.BIZDB_DSN_READONLY or settings.DB_DSN or "").strip()


# ---------------- ① 意图识别 ----------------
def _keyword_candidates(query: str) -> list[dict]:
    scored: list[tuple[int, dict]] = []
    for item in load_registry()["intents"].values():
        hits = sum(1 for kw in item.get("keywords") or [] if kw.lower() in query.lower())
        if hits:
            scored.append((hits, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


_ARG_PATTERNS = {
    # 客户/员工姓名：只取 2-4 个中文字，避免把"订单""库存"当姓名
    "customer_name": re.compile(r"客户[：: ]*([\u4e00-\u9fa5]{2,4})"),
    "employee_name": re.compile(r"(?:员工|姓名)[：: ]*([\u4e00-\u9fa5]{2,4})"),
    "sku": re.compile(r"\b(P\d{3,6})\b", re.I),
    "period": re.compile(r"(\d{4}-\d{2})"),
    "since_days": re.compile(r"(\d{1,3})\s*天"),
}

_KNOWN_NAMES = ("张三", "李四", "王五", "张伟", "李娜", "王芳", "刘洋", "陈静")

_NAME_CUT = ("的", "最近", "过去", "本月", "上月", "这个月", "上个月", "在", "买", "有", "名下")
_SELF_WORDS = ("我", "本人", "自己", "咱")


def _clean_name(text: str) -> str:
    """从"张三的订单"这类片段里切出姓名，避免把后缀词一起当参数。"""
    value = (text or "").strip()
    for token in _NAME_CUT:
        index = value.find(token)
        if index > 0:
            value = value[:index]
    return value[:4]


def _keyword_args(intent: dict, query: str) -> dict:
    """关键词兜底抽取参数（LLM 不可用时的确定性路径）。"""
    args: dict = {}
    for param in intent["params"]:
        name = param["name"]
        if name == "since_days":
            match = _ARG_PATTERNS["since_days"].search(query)
            if match:
                args[name] = int(match.group(1))
            continue
        pattern = _ARG_PATTERNS.get(name)
        if pattern:
            match = pattern.search(query)
            if match:
                value = match.group(1)
                args[name] = _clean_name(value) if name in ("customer_name", "employee_name") else value
        if name == "customer_name" and "customer_name" not in args:
            for known in _KNOWN_NAMES:
                if known in query:
                    args[name] = known
                    break
        if name == "employee_name" and "employee_name" not in args:
            match = re.search(r"\b(E\d{4,6})\b", query, re.I)
            if match:
                args[name] = match.group(1)
    return args


def llm_recognize(query: str, candidates: list[dict]) -> Optional[dict]:
    """LLM 结构化意图识别；异常/解析失败返回 None，由关键词兜底。"""
    try:
        from ..llm import chat
        from ..prompts import BIZ_INTENT_SYSTEM_PROMPT

        payload = {
            "query": query,
            "intents": [
                {
                    "intent_id": item["intent_id"],
                    "title": item["title"],
                    "examples": item["examples"],
                    "params": item["params"],
                }
                for item in (candidates or load_registry()["intents"].values())
            ],
        }
        raw = chat(
            [
                {"role": "system", "content": BIZ_INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            json_mode=True,
            temperature=0.0,
        )
        data = json.loads(raw or "{}")
        intent_id = data.get("intent")
        if intent_id in (None, "", "null"):
            return {"intent": None, "args": {}, "confidence": 0.0, "reason": "无可匹配意图"}
        if not get_intent(intent_id):
            return None
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "intent": intent_id,
            "args": data.get("args") or {},
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(data.get("reason") or "LLM 意图识别"),
        }
    except Exception as exc:  # noqa: BLE001 - 识别降级为关键词，不击穿主链路
        logger.warning("LLM 意图识别失败，回落关键词识别：%s", exc)
        return None


def recognize(query: str, user: dict | None = None) -> dict:
    """识别用户问题对应的业务意图（不执行 SQL）。

    返回 {"intent": str|None, "args": dict, "confidence": float, "reason": str, "source": str}
    member 问"我/本人"时属于天然的本人范围，置信度抬高到可执行，避免反复反问。
    """
    q = (query or "").strip()
    candidates = _keyword_candidates(q)
    # 关键词一个都没命中时也让 LLM 看一眼（例如"张三买了什么"没有关键词但语义明确）
    llm = llm_recognize(q, candidates)
    result = None
    if llm is not None and llm["intent"]:
        llm["source"] = "llm"
        result = llm
    elif candidates:
        intent = candidates[0]
        result = {
            "intent": intent["intent_id"],
            "args": _keyword_args(intent, q),
            "confidence": 0.7,
            "reason": "关键词命中（LLM 不可用或未给出意图）",
            "source": "keyword",
        }
    else:
        result = {"intent": None, "args": {}, "confidence": 0.0, "reason": "无匹配意图", "source": "keyword"}

    role = (user or {}).get("role") or ""
    intent_obj = get_intent(result["intent"]) if result.get("intent") else None
    if (
        intent_obj
        and role != "admin"
        and "member_self" in (intent_obj.get("allowed_roles") or [])
        and any(word in q for word in _SELF_WORDS)
    ):
        # "我的/本人"指向唯一主体，作用域由 sql_self 强制为本人，不需要再让用户补姓名
        result["confidence"] = max(float(result.get("confidence") or 0), 0.85)
        result["reason"] = (result.get("reason") or "") + "；本人范围（member_self）"
        result["args"] = {}
    return result


# ---------------- ②③ 参数校验 / 权限 / 合规 ----------------
def _coerce_param(param: dict, value: Any) -> tuple[Any, Optional[str]]:
    name = param["name"]
    expected = param.get("type", "string")
    if expected == "number":
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, f"参数 {name} 需要整数"
        if "min" in param and number < int(param["min"]):
            return None, f"参数 {name} 不能小于 {param['min']}"
        if "max" in param and number > int(param["max"]):
            return None, f"参数 {name} 不能大于 {param['max']}"
        return number, None
    text = str(value).strip()
    if "min_length" in param and len(text) < int(param["min_length"]):
        return None, f"参数 {name} 太短"
    if "max_length" in param and len(text) > int(param["max_length"]):
        return None, f"参数 {name} 太长（≤{param['max_length']} 字）"
    pattern = param.get("pattern")
    if pattern and not re.match(pattern, text):
        return None, f"参数 {name} 格式不正确（示例 {param.get('example')}）"
    return text, None


def normalize_args(intent: dict, raw_args: dict) -> tuple[dict, list[str]]:
    """逐参数校验并补默认值；返回 (clean_args, 问题列表)。"""
    clean: dict = {}
    problems: list[str] = []
    for param in intent["params"]:
        name = param["name"]
        value = (raw_args or {}).get(name)
        if value in (None, ""):
            if "default" in param:
                clean[name] = param["default"]
            elif param.get("required"):
                problems.append(f"缺少{name}")
            continue
        coerced, error = _coerce_param(param, value)
        if error:
            problems.append(error)
        else:
            clean[name] = coerced
    return clean, problems


def role_allows(intent: dict, role: str) -> bool:
    """角色闸门：admin 全量；member 仅 allowed_roles 含 member；member_self 仅本人。"""
    allowed = intent.get("allowed_roles") or []
    if role == "admin":
        return "admin" in allowed
    return "member" in allowed or "member_self" in allowed


def use_self_scope(intent: dict, role: str) -> bool:
    """member 且意图声明 member_self（有 sql_self）时，强制走本人范围。"""
    if role == "admin":
        return False
    return "member_self" in (intent.get("allowed_roles") or []) and bool(intent.get("sql_self"))


def guard_sql(sql: str, intent: dict) -> Optional[str]:
    """合规闸门：返回拒绝原因，None 表示通过。"""
    for pattern in _FORBIDDEN_SQL:
        if pattern.search(sql):
            return f"SQL 含禁止成分：{pattern.pattern}"
    refs = _TABLE_REFS(sql)
    allowed = {table.lower() for table in intent["tables"]}
    if not refs:
        return "SQL 未引用注册表内的表"
    if refs - allowed:
        return f"SQL 引用了未授权表：{sorted(refs - allowed)}"
    schemas = {s.lower() for s in (intent.get("schemas") or [intent["schema"]])}
    used_schemas = {schema.lower() for schema, _ in _SCHEMA_TABLE_REF.findall(sql)}
    if used_schemas - schemas:
        return f"SQL 引用了未授权 schema：{sorted(used_schemas - schemas)}"
    if not re.search(r"\bselect\b", sql, re.I):
        return "SQL 必须是 SELECT"
    return None


def _build_sql(intent: dict, args: dict, user: dict) -> tuple[str, dict, list[str]]:
    """按角色选择模板并构造绑定参数；返回 (sql, params, 问题列表)。"""
    role = user.get("role") or "member"
    problems: list[str] = []
    self_scope = use_self_scope(intent, role)
    sql = intent.get("sql_self") if self_scope else intent["sql"]
    params = dict(args)
    limit = min(int(intent["max_rows"]), int(_limits()["hard"]))
    params["limit"] = max(1, limit)

    if self_scope:
        params["user_id"] = int(user["user_id"])
    else:
        # 非本人范围必须有姓名/SKU 等定位参数，禁止"查全部"
        locate = [p for p in intent["params"] if p["name"] in ("customer_name", "sku", "employee_name")]
        if locate and not any(params.get(p["name"]) for p in locate):
            problems.append("缺少查询对象（请给出姓名或 SKU）")
    if intent["intent_id"] == "inventory_by_sku":
        params["sku_like"] = f"%{params.get('sku', '')}%"
    if intent["intent_id"] == "payroll_by_employee" and not params.get("period"):
        today = date.today()
        params["period_since"] = f"{today.year}-{(today.month - 3) if today.month > 3 else 1:02d}"
    if "since_days" in params and params["since_days"] is None:
        params["since_days"] = _limits()["default_days"]
    return sql, params, problems


def _count_sql(sql: str) -> str:
    """把投影换成 COUNT(*)：去掉 ORDER BY / LIMIT 后只保留 FROM 之后的部分。

    注意必须一并剥掉 `ORDER BY ... LIMIT %(limit)s`，否则占位符没有对应参数会直接报
    ProgrammingError，导致大结果场景永远拿不到真实总数（也就不会触发导出）。
    """
    body = re.sub(r"\s+ORDER\s+BY\s+.+$", "", sql, flags=re.I | re.S)
    idx = body.find(" FROM ")
    if idx < 0:
        return ""
    return "SELECT COUNT(*) AS n" + body[idx:]


# ---------------- ④ 脱敏 ----------------
def mask_value(kind: str, value: Any) -> str:
    """敏感字段脱敏：手机前3后4、身份证前6后4、邮箱前2+域名、姓名保留姓。"""
    text = "" if value is None else str(value)
    if not text:
        return text
    if kind == "phone":
        return text[:3] + "****" + text[-4:] if len(text) >= 8 else "***"
    if kind == "id_card":
        return text[:6] + "********" + text[-4:] if len(text) >= 12 else "***"
    if kind == "email":
        local, _, domain = text.partition("@")
        return (local[:2] + "***@" + domain) if domain else "***"
    if kind == "name":
        return text[0] + "*" if len(text) <= 2 else text[0] + "*" * (len(text) - 1)
    return text


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


# ---------------- 限流 ----------------
def rate_limit_ok(user_id: int, per_minute: Optional[int] = None) -> bool:
    limit = int(per_minute or _cfg("BIZ_QUERY_RATE_PER_MIN", settings.BIZ_QUERY_RATE_PER_MIN))
    now = time.monotonic()
    stamps = [t for t in _call_log.get(user_id, []) if now - t < 60]
    if len(stamps) >= limit:
        _call_log[user_id] = stamps
        return False
    stamps.append(now)
    _call_log[user_id] = stamps
    return True


def reset_rate_limit(user_id: Optional[int] = None) -> None:
    if user_id is None:
        _call_log.clear()
    else:
        _call_log.pop(user_id, None)


# ---------------- 执行 ----------------
async def _fetch(sql: str, params: dict, timeout: float) -> tuple[list[dict], str | None]:
    import psycopg

    conn = await psycopg.AsyncConnection.connect(
        dsn(), autocommit=False, connect_timeout=3, options=f"-c statement_timeout={int(timeout * 1000)}"
    )
    try:
        await conn.set_read_only(True)
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            columns = [desc.name for desc in (cur.description or [])]
            rows = await cur.fetchall()
        await conn.rollback()
        return [dict(zip(columns, row)) for row in rows], None
    except Exception as exc:  # noqa: BLE001 - 统一转成可展示原因，不泄露连接串
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("业务库 SQL 执行失败：%s", type(exc).__name__)
        if "statement_timeout" in str(exc).lower() or "canceling statement" in str(exc).lower():
            return [], "查询超时，请缩小时间范围或减少返回字段"
        return [], "业务库查询失败，请稍后重试或联系管理员"
    finally:
        await conn.close()


async def execute(intent_id: str, args: dict, user: dict) -> dict:
    """执行一次业务查询（已完成权限/参数/合规/边界校验），返回表格载荷。"""
    intent = get_intent(intent_id)
    if intent is None:
        return {"ok": False, "code": "BIZ_NO_INTENT", "message": "未登记的业务意图"}

    started = time.monotonic()
    role = (user or {}).get("role") or "member"
    if not role_allows(intent, role):
        await audit.log_operation(
            user.get("user_id"), audit.BIZ_QUERY, target_type="biz_intent", target_id=intent_id,
            result=0, detail={"reason": "role", "role": role, "stage": "permission"},
        )
        return {
            "ok": False,
            "code": "BIZ_FORBIDDEN",
            "message": "你的账号无权查询该类业务数据（客户订单/客户资料仅管理员可查，员工考勤与薪资仅限本人）。",
            "intent": intent_id,
        }
    clean_args, problems = normalize_args(intent, args)
    sql, params, build_problems = _build_sql(intent, clean_args, user)
    problems.extend(build_problems)
    if problems:
        return {
            "ok": False,
            "code": "BIZ_NEED_CLARIFY",
            "message": "请补充或修正：" + "；".join(problems),
            "intent": intent_id,
        }

    denied = guard_sql(sql, intent)
    if denied:
        logger.warning("业务查询被合规闸门拦截 intent=%s reason=%s", intent_id, denied)
        await audit.log_operation(
            user.get("user_id"), audit.BIZ_QUERY, target_type="biz_intent", target_id=intent_id,
            result=0, detail={"reason": denied, "stage": "compliance"},
        )
        return {"ok": False, "code": "BIZ_ILLEGAL_SQL", "message": "查询被合规策略拦截"}

    inline_max = int(_limits()["inline"])
    limit = int(params["limit"])
    fetch_limit = min(limit, inline_max)

    # 先按内联上限取数；命中上限时再单独 COUNT，得到总行数用于"是否截断"
    params_exec = dict(params)
    params_exec["limit"] = fetch_limit + 1
    rows_raw, error = await _fetch(sql, params_exec, float(_cfg("BIZ_QUERY_TIMEOUT", settings.BIZ_QUERY_TIMEOUT)))
    if error:
        await audit.log_operation(
            user.get("user_id"), audit.BIZ_QUERY, target_type="biz_intent", target_id=intent_id,
            result=0, detail={"reason": error, "stage": "execute", "args": clean_args},
        )
        return {"ok": False, "code": "BIZ_EXEC_ERROR", "message": error, "intent": intent_id}

    truncated_more = len(rows_raw) > fetch_limit
    rows_raw = rows_raw[:fetch_limit]

    total = len(rows_raw)
    if truncated_more:
        count_sql = _count_sql(sql)
        if count_sql:
            count_rows, count_error = await _fetch(count_sql, {k: v for k, v in params.items() if k != "limit"},
                                                   float(_cfg("BIZ_QUERY_TIMEOUT", settings.BIZ_QUERY_TIMEOUT)))
            if not count_error and count_rows:
                total = int(count_rows[0].get("n") or len(rows_raw))
    truncated = total > len(rows_raw)

    mask_map = {col["key"]: col.get("mask") for col in intent["columns"] if col.get("mask")}

    def _shape(raw_rows: list[dict]) -> list[dict]:
        shaped: list[dict] = []
        for raw in raw_rows:
            row = {}
            for key, value in raw.items():
                if mask_map.get(key):
                    row[key] = mask_value(mask_map[key], _jsonable(value))
                else:
                    row[key] = _jsonable(value)
            shaped.append(row)
        return shaped

    rows = _shape(rows_raw)
    column_defs = [
        {"key": col["key"], "title": col["title"], "masked": bool(col.get("mask")), "type": col.get("type")}
        for col in intent["columns"]
    ]

    # ---- 超过内联上限：生成 CSV 导出（脱敏后的同一份数据，可重复下载），只回汇总 + 链接 ----
    export_info: Optional[dict] = None
    hard_max = int(_limits()["hard"])
    if total > inline_max:
        export_params = dict(params)
        export_params["limit"] = hard_max
        export_rows_raw, export_error = await _fetch(
            sql, export_params, float(_cfg("BIZ_QUERY_TIMEOUT", settings.BIZ_QUERY_TIMEOUT))
        )
        if export_error:
            logger.warning("导出取数失败 intent=%s", intent_id)
        else:
            export_rows = _shape(export_rows_raw[:hard_max])
            export_info = await export_store.create_export(
                int(user["user_id"]), intent["title"], column_defs, export_rows
            )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    await audit.log_operation(
        user.get("user_id"), audit.BIZ_QUERY, target_type="biz_intent", target_id=intent_id,
        result=1,
        detail={
            "intent": intent_id,
            "args": clean_args,
            "self_scope": use_self_scope(intent, user.get("role") or "member"),
            "rows": len(rows),
            "total": total,
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
            "exported": bool(export_info),
            "export_rows": (export_info or {}).get("rows", 0),
        },
    )

    table = {
        "intent": intent_id,
        "title": intent["title"],
        "columns": column_defs,
        "rows": rows,
        "total": total,
        "truncated": truncated,
        # 总数超过硬上限时，CSV 里也只有前 hard_max 行；为 False 表示导出已包含全部结果
        "hard_truncated": total > hard_max,
        "inline_max": inline_max,
        "elapsed_ms": elapsed_ms,
        # 超过内联上限时给出可重复下载的链接；未超过时为 None，前端不渲染下载按钮
        "download_url": (export_info or {}).get("url"),
        "download_rows": (export_info or {}).get("rows"),
        "download_expires_at": (export_info or {}).get("expires_at"),
        "download_ttl_minutes": (export_info or {}).get("ttl_minutes"),
    }
    return {"ok": True, "table": table, "intent": intent_id, "args": clean_args}


async def decide_and_execute(query: str, user: dict) -> dict:
    """工具入口：识别 → 权限 → 限流 → 执行，统一返回工具结果结构。"""
    if not dsn():
        return {"ok": False, "code": "BIZ_DISABLED", "message": "业务库未配置，请联系系统管理员"}

    recognition = recognize(query, user)
    intent_id = recognition.get("intent")
    if not intent_id:
        return {"ok": False, "code": "BIZ_NO_INTENT", "message": CAPABILITY_HINT, "confidence": 0}

    intent = get_intent(intent_id)
    confidence = float(recognition.get("confidence") or 0)
    if confidence < 0.6:
        hint = (intent.get("examples") or ["请说明查询对象与时间范围"])[0]
        return {
            "ok": False,
            "code": "BIZ_NEED_CLARIFY",
            "message": f"没完全理解要查的内容（置信度 {confidence:.2f}）。请补充查询对象与范围，例如「{hint}」。",
            "intent": intent_id,
        }
    if not role_allows(intent, user.get("role") or "member"):
        await audit.log_operation(
            user.get("user_id"), audit.BIZ_QUERY, target_type="biz_intent", target_id=intent_id,
            result=0, detail={"reason": "role_denied", "role": user.get("role")},
        )
        return {"ok": False, "code": "BIZ_FORBIDDEN", "message": "当前账号无权查询该类业务数据"}

    if not rate_limit_ok(int(user["user_id"])):
        return {"ok": False, "code": "BIZ_RATE_LIMITED", "message": "业务查询过于频繁，请稍后再试（每分钟上限）"}

    return await execute(intent_id, recognition.get("args") or {}, user)

