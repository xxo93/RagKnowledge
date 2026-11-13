"""工作流 Agent（v3 / WP4）。

定位：在现有多轮单轮 Agent（`agent/graph.py`）之上加一层「计划 → 执行 → 反思」编排，
不改 graph 本身，每一步复用已有工具（knowledge_search / biz_query / calculator /
doc_stats / web_search / general_chat）。

边界：
- planner 只产出结构化计划，强校验（步数 / 环 / 工具白名单 / 参数 schema / 权限 / 危险动作）；
- executor 按拓扑序执行，单步超时 + 整任务超时 + 预算，全部落 `workflow_task/step`；
- reflector 规则优先：工具报错重试 1 次 → 等价工具降级 → 仍失败则任务失败；
- 事件通过内存发布会话推给 `/api/workflow/{id}/events`，断线重连以 DB 快照恢复。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import settings
from ..db import get_pool
from ..errors import BizError, ERR_PARAM, ERR_LLM_UNAVAILABLE
from .. import prompts
from . import app_config, io_log, web_search
from ..llm import chat as llm_chat

logger = logging.getLogger("rag.workflow")

# ---------------------------------------------------------------------------
# 状态常量（与需求 §6.3 / §6.7 保持一致，前端直接展示原始值）
# ---------------------------------------------------------------------------
TASK_PLANNING = "planning"
TASK_RUNNING = "running"
TASK_AWAITING = "awaiting_confirm"
TASK_PAUSED = "paused"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_BUDGET = "budget_exceeded"
TASK_INTERRUPTED = "interrupted"

TASK_TERMINAL = {TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED, TASK_BUDGET}

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_AWAITING = "awaiting_confirm"

TOOL_LABELS = {
    "knowledge_search": "知识库检索",
    "biz_query": "业务库查询",
    "calculator": "计算器",
    "doc_stats": "知识库统计",
    "web_search": "联网检索",
    "general_chat": "通用生成",
}

# 工具参数 schema：required 字段 + 类型说明（planner 强校验用）
TOOL_ARGS = {
    "knowledge_search": {"required": ("query",), "desc": "query 为检索词"},
    "biz_query": {"required": ("query",), "desc": "query 为业务问题原句"},
    "calculator": {"required": ("expression",), "desc": "expression 仅数字与四则运算"},
    "doc_stats": {"required": ("stat_type",), "desc": "stat_type 四选一"},
    "web_search": {"required": ("query",), "desc": "query 为联网检索词"},
    "general_chat": {"required": ("prompt",), "desc": "prompt 为完整生成指令"},
}
DOC_STAT_TYPES = {"total_docs", "success_docs", "failed_docs", "total_chunks"}
DOC_STAT_LABELS = {
    "total_docs": "文档总数",
    "success_docs": "成功入库文档数",
    "failed_docs": "失败文档数",
    "total_chunks": "切片总数",
}

# 明显危险动作：SQL / Shell / 删改数据。planner 输出命中即整份计划拒绝。
DANGEROUS_RE = re.compile(
    r"(?i)(\b(select|insert|update|delete|drop|alter|truncate|create\s+table|grant|revoke)\b"
    r"|\brm\s+-|\bdel\s+/|\bformat\s+[a-z]:|powershell|cmd\.exe|os\.system|subprocess)"
)
# member 不能查全员薪资（需求 §6.4 权限裁剪）；命中改为 skipped 并在报告说明
SALARY_RE = re.compile(r"(全员|所有员工|全部员工|全体成员|各员工|每个人的|员工)\s*(薪资|工资|薪酬|工资表|salary)")

_ensure_cache: Optional[dict] = None
_ensure_lock: Optional[asyncio.Lock] = None

# 运行中的任务：task_id -> asyncio.Task
RUNNING: dict[int, asyncio.Task] = {}
# 等待人工确认：task_id -> Event
_CONFIRM_EVENTS: dict[int, asyncio.Event] = {}
# 订阅者与最近事件（内存级；断线后由 DB 快照兜底）
_SUBSCRIBERS: dict[int, set[asyncio.Queue]] = {}
_RECENT: dict[int, list[dict]] = {}
_RECENT_MAX = 200

_DDL = """
CREATE TABLE IF NOT EXISTS workflow_task (
  task_id        BIGSERIAL PRIMARY KEY,
  user_id        BIGINT       NOT NULL,
  session_id     BIGINT       NULL,
  goal           TEXT         NOT NULL,
  plan_json      JSONB        NOT NULL DEFAULT '{}'::jsonb,
  status         VARCHAR(24)  NOT NULL DEFAULT 'planning',
  current_step   INT          NOT NULL DEFAULT 0,
  budget_json    JSONB        NULL,
  used_json      JSONB        NULL,
  result_summary TEXT         NULL,
  error_msg      TEXT         NULL,
  trace_id       VARCHAR(32)  NULL,
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ  NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_task_user ON workflow_task (user_id, task_id DESC);

CREATE TABLE IF NOT EXISTS workflow_step (
  step_id         BIGSERIAL PRIMARY KEY,
  task_id         BIGINT       NOT NULL REFERENCES workflow_task(task_id) ON DELETE CASCADE,
  step_index      INT          NOT NULL,
  title           TEXT         NOT NULL,
  tool            VARCHAR(32)  NOT NULL,
  args_json       JSONB        NOT NULL DEFAULT '{}'::jsonb,
  depends_on      INT[]        NOT NULL DEFAULT '{}',
  status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
  requires_confirm BOOLEAN     NOT NULL DEFAULT false,
  expected        TEXT         NULL,
  output_json     JSONB        NULL,
  error_msg       TEXT         NULL,
  retry_count     INT          NOT NULL DEFAULT 0,
  started_at      TIMESTAMPTZ  NULL,
  finished_at     TIMESTAMPTZ  NULL,
  UNIQUE (task_id, step_index)
);
CREATE INDEX IF NOT EXISTS idx_workflow_step_task ON workflow_step (task_id, step_index);
"""


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def _uid() -> int:
    return int(io_log.get_user_id() or 0)


async def ensure_schema() -> None:
    """启动时幂等建表；数据库不可用只告警，不阻断应用。"""
    global _ensure_cache, _ensure_lock
    if _ensure_cache is not None:
        return
    if _ensure_lock is None:
        _ensure_lock = asyncio.Lock()
    async with _ensure_lock:
        if _ensure_cache is not None:
            return
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(_DDL)
            _ensure_cache = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("工作流建表失败：%s", type(exc).__name__)
            _ensure_cache = {"ok": False, "error": type(exc).__name__}


async def recover_interrupted() -> int:
    """进程重启：把遗留 running/planning 置 interrupted，保留已完成步骤与输出。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                UPDATE workflow_step SET status = 'pending', started_at = NULL
                WHERE status = 'running'
                  AND task_id IN (SELECT task_id FROM workflow_task WHERE status IN ('running','planning'))
                """
            )
            affected_steps = cur.rowcount if cur is not None else 0
            cur = await conn.execute(
                """
                UPDATE workflow_task
                SET status = 'interrupted',
                    error_msg = COALESCE(error_msg, '服务重启导致任务中断，可点「继续」从当前步骤恢复'),
                    finished_at = now()
                WHERE status IN ('running','planning')
                """
            )
            affected = cur.rowcount if cur is not None else 0
    if affected:
        logger.warning("检测到 %d 个中断任务（%d 个运行中步骤），已置为 interrupted", affected, affected_steps)
    return int(affected or 0)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _task_row(row) -> dict:
    plan = row[4] if isinstance(row[4], (dict, list)) else (json.loads(row[4]) if row[4] else {})
    budget = row[6] if isinstance(row[6], (dict, list)) else (json.loads(row[6]) if row[6] else {})
    used = row[7] if isinstance(row[7], (dict, list)) else (json.loads(row[7]) if row[7] else {})
    return {
        "task_id": int(row[0]),
        "user_id": int(row[1]),
        "session_id": int(row[2]) if row[2] is not None else None,
        "goal": row[3],
        "plan": plan,
        "status": row[5],
        "current_step": int(row[8] or 0),
        "budget": budget,
        "used": used,
        "result_summary": row[9],
        "error_msg": row[10],
        "trace_id": row[11],
        "created_at": _iso(row[12]),
        "finished_at": _iso(row[13]),
    }


def _step_row(row) -> dict:
    deps = list(row[6] or [])
    output = row[10]
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except Exception:  # noqa: BLE001
            output = {"summary": output}
    args = row[5] if isinstance(row[5], dict) else (json.loads(row[5]) if row[5] else {})
    return {
        "step_id": int(row[0]),
        "task_id": int(row[1]),
        "step_index": int(row[2]),
        "title": row[3],
        "tool": row[4],
        "tool_label": TOOL_LABELS.get(row[4], row[4]),
        "args": args,
        "depends_on": [int(x) for x in deps],
        "status": row[7],
        "requires_confirm": bool(row[8]),
        "expected": row[9],
        "output": output,
        "error_msg": row[11],
        "retry_count": int(row[12] or 0),
        "started_at": _iso(row[13]),
        "finished_at": _iso(row[14]),
    }


_TASK_COLS = """
task_id, user_id, session_id, goal, plan_json, status, budget_json, used_json,
current_step, result_summary, error_msg, trace_id, created_at, finished_at
"""
_STEP_COLS = """
step_id, task_id, step_index, title, tool, args_json, depends_on, status,
requires_confirm, expected, output_json, error_msg, retry_count, started_at, finished_at
"""


async def get_task(task_id: int, user: Optional[dict] = None) -> dict:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {_TASK_COLS} FROM workflow_task WHERE task_id = %(tid)s", {"tid": int(task_id)}
        )
        row = await cur.fetchone()
        if row is None:
            raise BizError(40404, "任务不存在或已被删除", http_status=404)
        task = _task_row(row)
        if user and int(user.get("user_id") or 0) != task["user_id"]:
            raise BizError(40303, "无权查看该任务", http_status=403)
        cur = await conn.execute(
            f"SELECT {_STEP_COLS} FROM workflow_step WHERE task_id = %(tid)s ORDER BY step_index",
            {"tid": int(task_id)},
        )
        rows = await cur.fetchall()
    task["steps"] = [_step_row(r) for r in rows]
    task["progress"] = _progress(task)
    return task


async def list_tasks(user: dict, status: Optional[str] = None, keyword: Optional[str] = None,
                     page: int = 1, size: int = 20) -> tuple[int, list[dict]]:
    params: dict[str, Any] = {"uid": int(user.get("user_id") or 0)}
    where = "WHERE t.user_id = %(uid)s"
    if status:
        where += " AND t.status = %(status)s"
        params["status"] = status
    if keyword:
        where += " AND t.goal ILIKE %(kw)s"
        params["kw"] = f"%{keyword.strip()}%"
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) FROM workflow_task t {where}", params)
        total = int((await cur.fetchone())[0])
        params["lim"] = max(1, min(int(size), 100))
        params["off"] = (max(1, int(page)) - 1) * params["lim"]
        cur = await conn.execute(
            f"""
            SELECT t.task_id, t.goal, t.status, t.current_step, t.created_at, t.finished_at,
                   t.result_summary, t.trace_id, t.plan_json,
                   (SELECT count(*) FROM workflow_step s WHERE s.task_id = t.task_id),
                   (SELECT count(*) FROM workflow_step s WHERE s.task_id = t.task_id AND s.status = 'done')
            FROM workflow_task t {where}
            ORDER BY t.task_id DESC LIMIT %(lim)s OFFSET %(off)s
            """,
            params,
        )
        rows = await cur.fetchall()
    items = []
    for r in rows:
        plan = r[8] if isinstance(r[8], dict) else (json.loads(r[8]) if r[8] else {})
        items.append({
            "task_id": int(r[0]),
            "goal": r[1],
            "status": r[2],
            "current_step": int(r[3] or 0),
            "created_at": _iso(r[4]),
            "finished_at": _iso(r[5]),
            "result_summary": r[6],
            "trace_id": r[7],
            "step_total": int(r[9] or 0),
            "step_done": int(r[10] or 0),
            "budget": plan.get("budget") if isinstance(plan, dict) else None,
        })
    return total, items


def _progress(task: dict) -> dict:
    steps = task.get("steps") or []
    total = len(steps)
    done = len([s for s in steps if s["status"] in (STEP_DONE, STEP_SKIPPED)])
    failed = len([s for s in steps if s["status"] == STEP_FAILED])
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "percent": int(round(done * 100 / total)) if total else 0,
    }


async def _update_task(task_id: int, **fields) -> None:
    if not fields:
        return
    sets = []
    params: dict[str, Any] = {"tid": int(task_id)}
    for key, value in fields.items():
        sets.append(f"{key} = %({key})s")
        params[key] = value
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE workflow_task SET {', '.join(sets)} WHERE task_id = %(tid)s", params
            )


async def _update_step(step_id: int, **fields) -> None:
    if not fields:
        return
    sets = []
    params: dict[str, Any] = {"sid": int(step_id)}
    for key, value in fields.items():
        sets.append(f"{key} = %({key})s")
        params[key] = value
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE workflow_step SET {', '.join(sets)} WHERE step_id = %(sid)s", params
            )


async def _mark_step_running(task_id: int, step_id: int) -> bool:
    """并行执行专用：仅 pending→running 的条件更新，防止取消/暂停后被重新置 running。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE workflow_step SET status = 'running', started_at = now()
                WHERE step_id = %(sid)s AND task_id = %(tid)s AND status = 'pending'
                RETURNING step_id
                """,
                {"sid": int(step_id), "tid": int(task_id)},
            )
            row = await result.fetchone()
    return row is not None


async def _finish_step_running(step_id: int, **fields) -> bool:
    """并行执行专用：仅 running→done/failed 的条件更新，取消后不再写半截结果。"""
    if not fields:
        return False
    sets = []
    params: dict[str, Any] = {"sid": int(step_id)}
    for key, value in fields.items():
        sets.append(f"{key} = %({key})s")
        params[key] = value
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                f"UPDATE workflow_step SET {', '.join(sets)} WHERE step_id = %(sid)s "
                "AND status = 'running' RETURNING step_id",
                params,
            )
            row = await result.fetchone()
    return row is not None


def _json(value) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _truncate_output(output: Any, limit: int = 8192) -> dict:
    """单步输出截断 8KB：保留 summary 与关键字段，超长明细改摘要 + 标记。"""
    if isinstance(output, dict):
        payload = dict(output)
    else:
        payload = {"summary": str(output or "")}
    raw = _json(payload) or ""
    if len(raw) <= limit:
        return payload
    summary = str(payload.get("summary") or "")[: limit // 2]
    compact = {k: v for k, v in payload.items() if k in ("ok", "summary", "sources", "row_count", "error")}
    compact["summary"] = summary
    compact["truncated"] = True
    compact["original_chars"] = len(raw)
    return compact


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------
def _publish(task_id: int, event: dict) -> None:
    event = {**event, "task_id": int(task_id), "ts": time.time()}
    _RECENT.setdefault(int(task_id), []).append(event)
    if len(_RECENT[int(task_id)]) > _RECENT_MAX:
        _RECENT[int(task_id)] = _RECENT[int(task_id)][-_RECENT_MAX:]
    for queue in list(_SUBSCRIBERS.get(int(task_id), ())):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


def subscribe(task_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _SUBSCRIBERS.setdefault(int(task_id), set()).add(queue)
    return queue


def unsubscribe(task_id: int, queue: asyncio.Queue) -> None:
    _SUBSCRIBERS.get(int(task_id), set()).discard(queue)


def recent_events(task_id: int) -> list[dict]:
    return list(_RECENT.get(int(task_id), []))


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
def _extract_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except Exception:  # noqa: BLE001
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise BizError(ERR_PARAM, "计划生成失败：模型未返回合法 JSON，请调整描述后重试")
        try:
            value = json.loads(text[start:end + 1])
        except Exception as exc:  # noqa: BLE001
            raise BizError(ERR_PARAM, "计划生成失败：模型返回格式不合法，请调整描述后重试") from exc
    if not isinstance(value, dict):
        raise BizError(ERR_PARAM, "计划生成失败：模型返回的不是对象")
    return value


def _arg_text(args: dict) -> str:
    return " ".join(str(v) for v in (args or {}).values() if isinstance(v, (str, int, float)))


def _validate_plan(raw_plan: dict, user: dict, max_steps: int) -> tuple[dict, list[dict]]:
    """强校验计划；返回 (plan, steps)。权限不足的步骤直接标 skipped，其余非法输入整份拒绝。"""
    if raw_plan.get("error"):
        if str(raw_plan.get("error")) == "too_many_steps":
            raise BizError(
                ERR_PARAM,
                f"该任务超过 {max_steps} 步上限，请拆分为两个更小的任务后分别执行。",
            )
        raise BizError(ERR_PARAM, "计划生成失败：模型声明无法规划该任务，请补充更明确的目标")

    goal = str(raw_plan.get("goal") or "").strip()
    if not goal:
        raise BizError(ERR_PARAM, "计划生成失败：缺少任务目标")
    raw_steps = raw_plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise BizError(ERR_PARAM, "计划生成失败：没有可执行步骤")
    if len(raw_steps) > max_steps:
        raise BizError(
            ERR_PARAM,
            f"该任务需要 {len(raw_steps)} 步，超过 {max_steps} 步上限，请拆分为两个更小的任务后分别执行。",
        )

    role = (user or {}).get("role") or "member"
    ids: dict[str, int] = {}
    steps: list[dict] = []
    for pos, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise BizError(ERR_PARAM, f"计划第 {pos} 步格式不合法")
        sid = str(item.get("id") or f"s{pos}").strip()
        if sid in ids:
            raise BizError(ERR_PARAM, f"计划步骤 id 重复：{sid}")
        ids[sid] = pos
        tool = str(item.get("tool") or "").strip()
        if tool not in TOOL_ARGS:
            raise BizError(ERR_PARAM, f"计划第 {pos} 步使用了不支持的工具：{tool or '空'}")
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        missing = [k for k in TOOL_ARGS[tool]["required"] if not str(args.get(k) or "").strip()]
        if missing:
            raise BizError(ERR_PARAM, f"计划第 {pos} 步参数缺失：{', '.join(missing)}")
        if tool == "calculator" and not re.fullmatch(prompts.CALC_EXPR_PATTERN, str(args.get("expression") or "")):
            raise BizError(ERR_PARAM, f"计划第 {pos} 步计算表达式非法，只允许数字与四则运算")
        if tool == "doc_stats" and str(args.get("stat_type")) not in DOC_STAT_TYPES:
            raise BizError(ERR_PARAM, f"计划第 {pos} 步统计类型非法")
        text = _arg_text(args)
        if DANGEROUS_RE.search(text) or DANGEROUS_RE.search(str(item.get("title") or "")):
            raise BizError(ERR_PARAM, "计划中包含 SQL/命令/删改数据等危险动作，已拒绝执行")
        deps_raw = item.get("depends_on") or []
        if not isinstance(deps_raw, list):
            raise BizError(ERR_PARAM, f"计划第 {pos} 步依赖格式不合法")
        deps: list[int] = []
        for dep in deps_raw:
            dep_s = str(dep)
            if dep_s not in ids:
                raise BizError(ERR_PARAM, f"计划第 {pos} 步依赖了不存在的步骤：{dep_s}")
            idx = ids[dep_s]
            if idx >= pos:
                raise BizError(ERR_PARAM, "计划依赖关系非法：存在环或依赖了后续步骤")
            deps.append(idx)
        # 权限裁剪：普通成员不能查全员薪资；标 skipped 而不是让整份计划失败
        skipped_reason = None
        if role != "admin" and tool == "biz_query" and SALARY_RE.search(text):
            skipped_reason = "当前账号无权限查询全员薪资，已跳过该步骤（管理员可执行）"
        steps.append({
            "id": sid,
            "step_index": pos,
            "title": str(item.get("title") or f"{TOOL_LABELS[tool]} {pos}").strip()[:120],
            "tool": tool,
            "args": args,
            "depends_on": deps,
            "requires_confirm": bool(item.get("requires_confirm")),
            "expected": str(item.get("expected") or "").strip()[:300],
            "skipped_reason": skipped_reason,
        })
    plan = {"goal": goal[:2000], "steps": steps}
    return plan, steps


async def _recent_context(session_id: Optional[int]) -> str:
    """把会话最近几轮拼成规划上下文；读取失败不影响规划。"""
    if not session_id:
        return ""
    try:
        from . import chat_store

        _, records = await chat_store.list_records(int(session_id), limit=4)
        lines = []
        for r in records[-4:]:
            lines.append(f"用户：{r.get('query') or ''}\n助手：{(r.get('answer') or '')[:300]}")
        return "\n\n".join(lines)[:2000]
    except Exception:  # noqa: BLE001
        return ""


async def plan_task(goal: str, user: dict, session_id: Optional[int] = None,
                    trace_id: Optional[str] = None, auto_run: Optional[bool] = None) -> dict:
    """生成计划并落库；默认 status=awaiting_confirm，等待用户点「开始执行」。"""
    clean = (goal or "").strip()
    if not clean:
        raise BizError(ERR_PARAM, "任务目标不能为空")
    if len(clean) > 2000:
        raise BizError(ERR_PARAM, "任务目标过长，请控制在 2000 字以内")
    trace = trace_id or io_log.get_trace_id()
    io_log.set_trace_id(trace)
    io_log.set_user_id(int(user.get("user_id") or 0))
    max_steps = int(app_config.get_cached("WORKFLOW_MAX_STEPS") or settings.WORKFLOW_MAX_STEPS)
    context = await _recent_context(session_id)

    messages = [
        {"role": "system", "content": prompts.WORKFLOW_PLAN_SYSTEM_PROMPT.replace("{max_steps}", str(max_steps))},
        {"role": "user", "content": f"任务目标：{clean}\n\n会话上下文（可能为空）：\n{context or '无'}"},
    ]
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(llm_chat, messages, json_mode=True, purpose="workflow_plan"),
            timeout=30.0,
        )
    except BizError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("工作流规划失败：%s", exc)
        raise BizError(ERR_LLM_UNAVAILABLE, "计划生成失败：模型服务暂时不可用，请稍后重试", http_status=503) from exc

    raw_plan = _extract_json(raw)
    plan, steps = _validate_plan(raw_plan, user, max_steps)

    budget_seconds = int(app_config.get_cached("WORKFLOW_BUDGET_SECONDS") or settings.WORKFLOW_BUDGET_SECONDS)
    budget_tokens = int(app_config.get_cached("WORKFLOW_BUDGET_TOKENS") or settings.WORKFLOW_BUDGET_TOKENS)
    budget = {"max_seconds": budget_seconds, "max_tokens": budget_tokens}
    plan["budget"] = budget
    status = TASK_PLANNING

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                INSERT INTO workflow_task
                    (user_id, session_id, goal, plan_json, status, current_step, budget_json, used_json, trace_id)
                VALUES (%(uid)s, %(sid)s, %(goal)s, %(plan)s::jsonb, %(status)s, 0,
                        %(budget)s::jsonb, '{}'::jsonb, %(trace)s)
                RETURNING task_id
                """,
                {
                    "uid": int(user.get("user_id") or 0),
                    "sid": int(session_id) if session_id else None,
                    "goal": plan["goal"],
                    "plan": _json(plan),
                    "status": status,
                    "budget": _json(budget),
                    "trace": trace,
                },
            )
            task_id = int((await cur.fetchone())[0])
            for step in steps:
                step_status = STEP_SKIPPED if step["skipped_reason"] else STEP_PENDING
                await conn.execute(
                    """
                    INSERT INTO workflow_step
                        (task_id, step_index, title, tool, args_json, depends_on, status,
                         requires_confirm, expected, output_json, error_msg, finished_at)
                    VALUES (%(tid)s, %(idx)s, %(title)s, %(tool)s, %(args)s::jsonb, %(deps)s,
                            %(status)s::varchar, %(confirm)s, %(expected)s, %(out)s::jsonb, %(err)s,
                            CASE WHEN %(status)s::text = 'skipped' THEN now() ELSE NULL END)
                    """,
                    {
                        "tid": task_id,
                        "idx": step["step_index"],
                        "title": step["title"],
                        "tool": step["tool"],
                        "args": _json(step["args"]),
                        "deps": step["depends_on"],
                        "status": step_status,
                        "confirm": step["requires_confirm"],
                        "expected": step["expected"],
                        "out": _json({"summary": "权限不足，已跳过"}) if step["skipped_reason"] else None,
                        "err": step["skipped_reason"],
                    },
                )

    task = await get_task(task_id)
    await _update_task(task_id, status=TASK_AWAITING)
    task["status"] = TASK_AWAITING
    _publish(task_id, {"type": "plan", "task": task})

    auto = bool(app_config.get_cached("WORKFLOW_AUTO_RUN") if auto_run is None else auto_run)
    if auto:
        await start_task(task_id, user)
        return await get_task(task_id)
    return task


# ---------------------------------------------------------------------------
# Executor / Reflector
# ---------------------------------------------------------------------------
def _step_result_ok(output: dict) -> bool:
    return bool(output.get("ok")) and bool(output.get("summary") or output.get("data"))


def _empty_result(output: dict) -> bool:
    if not output.get("ok"):
        return False
    if output.get("empty"):
        return True
    summary = str(output.get("summary") or "").strip()
    return not summary and not output.get("data") and not output.get("sources")


async def _run_tool(step: dict, user: dict, goal: str, context: str = "") -> dict:
    """按工具分派；所有工具调用保持用户身份透传，输出统一为 {ok, summary, ...}。"""
    tool = step["tool"]
    args = step.get("args") or {}
    if tool == "knowledge_search":
        from ..agent.tools import search_tool

        result = await search_tool.run(str(args.get("query") or ""))
        if not result.get("ok"):
            return {"ok": False, "summary": result.get("error") or "知识库检索失败", "error": result.get("error")}
        hits = result.get("hits") or []
        return {
            "ok": True,
            "summary": str(result.get("answer") or "").strip(),
            "sources": (result.get("sources") or [])[:5],
            "hit_count": len(hits),
            "empty": not bool(result.get("answer") or hits),
        }
    if tool == "biz_query":
        from ..agent.tools import biz_query_tool

        result = await biz_query_tool.run(str(args.get("query") or ""), user)
        if not result.get("ok"):
            return {
                "ok": False,
                "summary": result.get("message") or "业务库查询失败",
                "error": result.get("code") or result.get("message"),
            }
        table = result.get("table") or {}
        rows = table.get("rows") or []
        return {
            "ok": True,
            "summary": result.get("message") or f"业务库查询完成，返回 {len(rows)} 行",
            "table": table,
            "row_count": len(rows),
            "data": {"intent": result.get("intent"), "table": result.get("table")},
            "empty": len(rows) == 0,
        }
    if tool == "calculator":
        from ..agent.tools import calc_tool

        result = await asyncio.to_thread(calc_tool.calculate, str(args.get("expression") or ""))
        if not result.get("ok"):
            return {"ok": False, "summary": result.get("error") or "计算失败", "error": result.get("error")}
        return {"ok": True, "summary": f"{args.get('expression')} = {result.get('result')}", "data": result}
    if tool == "doc_stats":
        from ..agent.tools import doc_stats_tool

        result = await doc_stats_tool.run(str(args.get("stat_type") or ""))
        if not result.get("ok"):
            return {"ok": False, "summary": result.get("error") or "统计失败", "error": result.get("error")}
        st = str(args.get("stat_type") or "")
        return {
            "ok": True,
            "summary": f"{DOC_STAT_LABELS.get(st, st)}：{result.get('value')}",
            "data": result,
        }
    if tool == "web_search":
        result = await web_search.ask(str(args.get("query") or ""))
        if not result.ok:
            return {"ok": False, "summary": result.message or "联网检索不可用", "error": result.error_code}
        return {
            "ok": True,
            "summary": result.answer or f"联网检索返回 {len(result.items)} 条结果",
            "sources": result.items[:5],
            "empty": not bool(result.items or result.answer),
        }
    if tool == "general_chat":
        system = (
            "你是企业任务执行器。根据任务目标和上游步骤结果完成当前步骤，"
            "不得编造企业内部数据；输出可直接进入最终报告的正文。"
        )
        user_msg = f"任务目标：{goal}\n当前步骤：{step['title']}\n要求：{args.get('prompt')}"
        if context:
            user_msg += f"\n\n上游步骤结果（生成时必须引用这些真实结果，不要复述占位符）：\n{context}"
        try:
            text = await asyncio.to_thread(llm_chat, [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ], purpose="workflow_step")
        except BizError as exc:
            return {"ok": False, "summary": exc.msg, "error": "LLM_UNAVAILABLE"}
        return {"ok": True, "summary": text, "empty": not bool(text.strip())}
    return {"ok": False, "summary": f"不支持的工具：{tool}", "error": "UNSUPPORTED_TOOL"}


async def _execute_step(step: dict, user: dict, goal: str, trace: str, context: str = "") -> dict:
    """单步执行：调用 → 超时 → 失败重试 1 次 → 等价工具降级。"""
    io_log.set_trace_id(trace)
    io_log.set_user_id(int(user.get("user_id") or 0))
    timeout = float(app_config.get_cached("WORKFLOW_STEP_TIMEOUT") or settings.WORKFLOW_STEP_TIMEOUT)
    attempts = 0
    last: dict = {}
    while attempts < 2:
        attempts += 1
        try:
            last = await asyncio.wait_for(_run_tool(step, user, goal, context), timeout=timeout)
        except asyncio.TimeoutError:
            last = {"ok": False, "summary": f"单步执行超时（>{int(timeout)}s）", "error": "STEP_TIMEOUT"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("步骤 %s 执行异常：%s", step["step_index"], type(exc).__name__)
            last = {"ok": False, "summary": "工具执行异常", "error": type(exc).__name__}
        if last.get("ok") and not _empty_result(last):
            if attempts > 1:
                last["retry_note"] = f"首次失败后重试成功（第 {attempts} 次）"
            return last
        if attempts == 1 and last.get("ok") and _empty_result(last):
            # 空结果触发规则式重试：换更宽的检索词，算一次反思重规划
            if step["tool"] in ("knowledge_search", "web_search"):
                base = str((step.get("args") or {}).get("query") or "")
                step["args"] = {**(step.get("args") or {}), "query": f"{base} 相关制度 流程"}
                last["replan_note"] = "结果为空，已扩大检索范围重试"
            else:
                break
    # 等价工具替代：biz_query→knowledge_search，knowledge_search→web_search
    fallback = {"biz_query": "knowledge_search", "knowledge_search": "web_search"}.get(step["tool"])
    if fallback and not last.get("ok") and last.get("error") not in ("BIZ_FORBIDDEN", "BIZ_RATE_LIMITED"):
        alt = dict(step)
        alt["tool"] = fallback
        alt["args"] = {"query": str((step.get("args") or {}).get("query") or step["title"])}
        try:
            alt_result = await asyncio.wait_for(_run_tool(alt, user, goal, context), timeout=timeout)
            alt_result["fallback_from"] = step["tool"]
            alt_result["fallback_tool"] = fallback
            note = f"{TOOL_LABELS.get(step['tool'], step['tool'])}不可用，已降级为{TOOL_LABELS.get(fallback, fallback)}"
            alt_result["fallback_note"] = note
            if alt_result.get("summary"):
                alt_result["summary"] = f"{note}：{alt_result['summary']}"
            return alt_result
        except Exception:  # noqa: BLE001
            pass
    return last


def _log_step(task: dict, step: dict, output: dict) -> None:
    """每步都写一条 chat_io_log（同一 trace），管理端可逐步复盘；不抢任务级 token 汇总。"""
    try:
        io_log.enqueue_turn(
            trace_id=task.get("trace_id") or io_log.get_trace_id(),
            user_id=task.get("user_id"),
            session_id=task.get("session_id"),
            intent=f"workflow:{step.get('tool')}",
            answer_mode="workflow",
            query=f"第{step.get('step_index')}步：{step.get('title')}",
            answer=str(output.get("summary") or "")[:3000],
            status="ok" if output.get("ok") else "error",
            error_code=None if output.get("ok") else str(output.get("error") or "STEP_ERROR")[:64],
            error_msg=None if output.get("ok") else str(output.get("summary") or "")[:500],
            prompt_tokens=0,
            completion_tokens=0,
            frame_count=1,
            tool_chain=[{"tool": step.get("tool"), "status": step.get("status")}],
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响任务
        pass


def _deterministic_report(task: dict) -> str:
    lines = [f"## 任务{'已完成' if task['status'] == TASK_COMPLETED else '部分完成'}：{task['goal']}", ""]
    for step in task.get("steps") or []:
        icon = {
            STEP_DONE: "✅", STEP_SKIPPED: "⏭️", STEP_FAILED: "❌", STEP_AWAITING: "⏸️",
        }.get(step["status"], "⏳")
        summary = ""
        if step.get("output"):
            summary = str(step["output"].get("summary") or "").strip()
        if step.get("error_msg"):
            summary = f"{summary}（{step['error_msg']}）".strip()
        lines.append(f"{icon} **{step['step_index']}. {step['title']}** — {summary or '无输出'}")
    failed = [s for s in task.get("steps") or [] if s["status"] == STEP_FAILED]
    skipped = [s for s in task.get("steps") or [] if s["status"] == STEP_SKIPPED]
    if skipped:
        lines += ["", "### 权限说明", "以下步骤因权限不足被跳过：" + "、".join(f"第 {s['step_index']} 步" for s in skipped)]
    if failed:
        lines += ["", "### 失败说明", "失败步骤：" + "、".join(f"第 {s['step_index']} 步" for s in failed)]
    return "\n".join(lines)


async def _compose_report(task: dict, trace: str) -> str:
    """优先 LLM 汇总；模型不可用时退回确定性报告，报告本身必须始终产出。"""
    outputs = []
    for step in task.get("steps") or []:
        if step.get("output"):
            outputs.append(f"第{step['step_index']}步「{step['title']}」：{str(step['output'].get('summary') or '')[:600]}")
    if not outputs:
        return _deterministic_report(task)
    prompt = (
        "你是任务报告撰写器。根据任务目标与各步骤结果写一份简明中文报告，"
        "必须保留关键数字与来源，不得编造未出现的数据；结构：结论 → 关键发现 → 逐步骤结果 → 风险/未完成事项。\n\n"
        f"任务目标：{task['goal']}\n" + "\n".join(outputs)
    )
    try:
        io_log.set_trace_id(trace)
        text = await asyncio.wait_for(
            asyncio.to_thread(llm_chat, [
                {"role": "system", "content": "只输出报告正文。"},
                {"role": "user", "content": prompt[:6000]},
            ], purpose="workflow_summary"),
            timeout=20.0,
        )
        if text.strip():
            return text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("任务报告 LLM 汇总失败，改用确定性报告：%s", type(exc).__name__)
    return _deterministic_report(task)


def _context_of(steps: list[dict]) -> str:
    """把已完成步骤的真实结果拼成上下文，供后续 general_chat 等步骤引用。"""
    lines = []
    for step in steps:
        if step.get("status") != STEP_DONE or not step.get("output"):
            continue
        summary = str((step["output"] or {}).get("summary") or "").strip()
        if summary:
            lines.append(f"第{step['step_index']}步「{step['title']}」：{summary[:800]}")
    return "\n".join(lines)[:4000]


def _next_pending(steps: list[dict]) -> Optional[dict]:
    for step in steps:
        if step["status"] != STEP_PENDING:
            continue
        deps = step.get("depends_on") or []
        dep_steps = [s for s in steps if s["step_index"] in deps]
        if any(s["status"] in (STEP_FAILED,) for s in dep_steps):
            continue
        if all(s["status"] in (STEP_DONE, STEP_SKIPPED) for s in dep_steps):
            return step
    return None


async def _finish(task_id: int, status: str, error: Optional[str] = None) -> dict:
    task = await get_task(task_id)
    used = dict(task.get("used") or {})
    report = task.get("result_summary") or ""
    if not report or status != TASK_CANCELLED:
        try:
            report = await _compose_report(task, task.get("trace_id") or io_log.get_trace_id())
        except Exception:  # noqa: BLE001
            report = _deterministic_report(task)
    prompt_tokens, completion_tokens = io_log.take_usage(task.get("trace_id") or "")
    used.update({
        "steps_done": task.get("progress", {}).get("done", 0),
        "steps_total": task.get("progress", {}).get("total", 0),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    })
    await _update_task(
        task_id,
        status=status,
        result_summary=report,
        error_msg=error,
        used_json=_json(used),
        finished_at=_now(),
    )
    # finished_at 是 SQL 片段，需要单独执行一次，避免参数化后写入字符串
    task = await get_task(task_id)
    _publish(task_id, {"type": "task_done", "status": status, "task": task})
    io_log.enqueue_turn(
        trace_id=task.get("trace_id") or io_log.get_trace_id(),
        user_id=task.get("user_id"),
        session_id=task.get("session_id"),
        intent="workflow",
        answer_mode="workflow",
        query=task.get("goal"),
        answer=report,
        status="ok" if status == TASK_COMPLETED else "error",
        error_code=None if status == TASK_COMPLETED else status.upper(),
        error_msg=error,
        elapsed_ms=int((used.get("elapsed_ms") or 0)),
        frame_count=len(task.get("steps") or []),
        tool_chain=[{"name": s["tool"], "label": s["tool_label"], "status": s["status"]} for s in task.get("steps") or []],
        context_usage={"workflow_task_id": task_id, "status": status, "budget": task.get("budget")},
    )
    return task


def _parallel_limit() -> int:
    """N3：并行上限；总开关关闭或配置为 1 时保持 M8 串行。"""
    try:
        if not bool(app_config.get_cached("WORKFLOW_PARALLEL")):
            return 1
        value = app_config.get_cached("WORKFLOW_MAX_PARALLEL")
        if value is None:
            value = settings.WORKFLOW_MAX_PARALLEL
        value = int(value)
    except Exception:  # noqa: BLE001
        value = int(settings.WORKFLOW_MAX_PARALLEL)
    return max(1, min(4, value))


def _ready_steps(steps: list[dict], exclude: Optional[set[int]] = None) -> list[dict]:
    """返回依赖已完成的 pending 步骤；依赖失败/被跳过的不进入 ready。"""
    exclude = exclude or set()
    ready: list[dict] = []
    for step in steps:
        if step["status"] != STEP_PENDING or int(step["step_id"]) in exclude:
            continue
        deps = step.get("depends_on") or []
        dep_steps = [s for s in steps if s["step_index"] in deps]
        if any(s["status"] == STEP_FAILED for s in dep_steps):
            continue
        if any(s["status"] == STEP_SKIPPED and "上游" in str(s.get("error_msg") or "") for s in dep_steps):
            continue
        if all(s["status"] in (STEP_DONE, STEP_SKIPPED) for s in dep_steps):
            ready.append(step)
    return ready


async def _skip_blocked_steps(task_id: int, steps: list[dict]) -> list[dict]:
    """把依赖失败/被阻塞的 pending 步骤级联标记 skipped，返回仍 pending 的步骤。"""
    changed = True
    while changed:
        changed = False
        for step in steps:
            if step["status"] != STEP_PENDING:
                continue
            deps = [s for s in steps if s["step_index"] in (step.get("depends_on") or [])]
            blocked = any(s["status"] == STEP_FAILED for s in deps) or any(
                s["status"] == STEP_SKIPPED and "上游" in str(s.get("error_msg") or "") for s in deps
            )
            if not blocked:
                continue
            await _update_step(
                step["step_id"], status=STEP_SKIPPED,
                error_msg="上游步骤失败或被阻塞，已跳过", finished_at=_now(),
            )
            step["status"] = STEP_SKIPPED
            step["error_msg"] = "上游步骤失败或被阻塞，已跳过"
            changed = True
    return [s for s in steps if s["status"] == STEP_PENDING]


async def _run_parallel_step(
    task_id: int,
    step: dict,
    user: dict,
    goal: str,
    context: str,
    trace: str,
) -> None:
    """并行 worker：单步独立 running/结束写入，失败只标记本步，不终止任务。"""
    task_now = await get_task(task_id)
    if task_now["status"] in (TASK_CANCELLED, TASK_PAUSED, TASK_INTERRUPTED, TASK_BUDGET):
        return
    if not await _mark_step_running(task_id, step["step_id"]):
        return
    await _update_task(task_id, current_step=step["step_index"], status=TASK_RUNNING)
    _publish(task_id, {"type": "step_start", "step": step, "task": await get_task(task_id)})

    output: dict
    try:
        output = await _execute_step(step, user, goal, trace, context)
        output = _truncate_output(output)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("并行步骤 %s 执行异常：%s", step["step_index"], type(exc).__name__)
        output = {"ok": False, "summary": "工具执行异常", "error": type(exc).__name__}

    task_now = await get_task(task_id)
    if task_now["status"] in (TASK_CANCELLED, TASK_PAUSED):
        return

    ok = bool(output.get("ok"))
    if ok:
        written = await _finish_step_running(
            step["step_id"], status=STEP_DONE, output_json=_json(output),
            finished_at=_now(), error_msg=None,
        )
        if written:
            _log_step(task_now, step, output)
            _publish(task_id, {"type": "step_done", "step_index": step["step_index"],
                               "output": output, "task": await get_task(task_id)})
    else:
        written = await _finish_step_running(
            step["step_id"], status=STEP_FAILED, output_json=_json(output),
            error_msg=str(output.get("summary") or "执行失败")[:500], finished_at=_now(),
        )
        if written:
            _log_step(task_now, step, output)
            _publish(task_id, {"type": "step_error", "step_index": step["step_index"],
                               "error": output.get("summary"), "task": await get_task(task_id)})

    # LLM 反思（可选）：并行步骤各自判断，不改变执行结果
    if ok and bool(app_config.get_cached("WORKFLOW_LLM_REFLECT")):
        try:
            judge = await asyncio.to_thread(
                llm_chat,
                [
                    {"role": "system", "content": "判断步骤结果是否满足预期，只输出 JSON："
                     '{"satisfied": bool, "reason": str}'},
                    {"role": "user", "content": f"预期：{step.get('expected') or '完成该步骤'}\n结果："
                      f"{str(output.get('summary') or '')[:800]}"},
                ],
                json_mode=True,
                purpose="workflow_reflect",
            )
            _publish(task_id, {"type": "reflect", "step_index": step["step_index"], "raw": judge})
        except Exception:  # noqa: BLE001
            pass


async def _execute_parallel(task_id: int, user: dict, max_parallel: int) -> None:
    """N3 并行执行器：按 depends_on 构建 ready 队列，最多 max_parallel 个步骤同时运行。"""
    token = None
    task = await get_task(task_id)
    trace = task.get("trace_id") or io_log.get_trace_id()
    token = io_log.set_trace_id(trace)
    io_log.set_user_id(int(user.get("user_id") or 0))
    started = time.perf_counter()
    inflight: dict[int, asyncio.Task] = {}
    try:
        budget_seconds = int((task.get("budget") or {}).get("max_seconds") or settings.WORKFLOW_BUDGET_SECONDS)
        deadline = started + budget_seconds
        await _update_task(task_id, status=TASK_RUNNING)
        task["status"] = TASK_RUNNING
        _publish(task_id, {"type": "task_status", "status": TASK_RUNNING, "task": task})

        budget_hit = False
        while True:
            task = await get_task(task_id)
            if task["status"] == TASK_CANCELLED:
                return
            if time.perf_counter() > deadline:
                budget_hit = True

            # 回收已完成 worker；异常已在 worker 内收敛，这里只清理
            for sid, worker in list(inflight.items()):
                if worker.done():
                    inflight.pop(sid, None)
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("并行步骤 worker 异常：%s", type(exc).__name__)

            ready = _ready_steps(task["steps"], set(inflight))

            # 人工确认不与其他步骤并行：等 inflight 清空后再进入等待
            if not inflight:
                confirm_step = next((s for s in ready if s["requires_confirm"]), None)
                if confirm_step:
                    await _update_step(confirm_step["step_id"], status=STEP_AWAITING)
                    await _update_task(task_id, status=TASK_AWAITING,
                                       current_step=confirm_step["step_index"])
                    task_now = await get_task(task_id)
                    _publish(task_id, {"type": "confirm_required", "step": confirm_step, "task": task_now})
                    waited = 0.0
                    while waited < 600:
                        await asyncio.sleep(0.5)
                        waited += 0.5
                        task_now = await get_task(task_id)
                        if task_now["status"] == TASK_CANCELLED:
                            return
                        current = next(
                            (s for s in task_now["steps"] if s["step_id"] == confirm_step["step_id"]), None
                        )
                        if current and current["status"] != STEP_AWAITING:
                            break
                    else:
                        await _update_step(confirm_step["step_id"], status=STEP_PENDING)
                        await _finish(task_id, TASK_FAILED, "等待人工确认超时（10 分钟）")
                        return
                    task = await get_task(task_id)
                    if task["status"] == TASK_CANCELLED:
                        return
                    continue

            # 预算耗尽后不再投递新步骤，等待 running 收尾
            if not budget_hit:
                for step in ready:
                    if len(inflight) >= max_parallel:
                        break
                    if step["requires_confirm"]:
                        break
                    worker = asyncio.create_task(
                        _run_parallel_step(
                            task_id, step, user, task["goal"],
                            _context_of(task["steps"]), trace,
                        )
                    )
                    inflight[int(step["step_id"])] = worker

            if inflight:
                await asyncio.wait(list(inflight.values()), timeout=0.5,
                                   return_when=asyncio.FIRST_COMPLETED)
                continue

            # 无在跑步骤：预算耗尽优先按预算结束
            if budget_hit:
                await _finish(task_id, TASK_BUDGET, f"时间预算 {budget_seconds}s 已耗尽")
                return

            pending_left = await _skip_blocked_steps(task_id, task["steps"])
            failed = [s for s in task["steps"] if s["status"] == STEP_FAILED]
            if failed or pending_left:
                await _finish(
                    task_id, TASK_FAILED,
                    "存在失败或被依赖阻塞的步骤：" + "、".join(
                        f"第 {s['step_index']} 步" for s in (failed + pending_left)
                    )[:300],
                )
            else:
                await _finish(task_id, TASK_COMPLETED)
            return
    except asyncio.CancelledError:
        task_now = None
        try:
            task_now = await get_task(task_id)
        except Exception:  # noqa: BLE001
            pass
        if task_now and task_now["status"] == TASK_CANCELLED:
            _publish(task_id, {"type": "task_done", "status": TASK_CANCELLED, "task": task_now})
        elif task_now and task_now["status"] == TASK_PAUSED:
            _publish(task_id, {"type": "task_status", "status": TASK_PAUSED, "task": task_now})
        else:
            await _update_task(task_id, status=TASK_INTERRUPTED,
                               error_msg="执行被中断，可点「继续」从当前步骤恢复")
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("工作流任务 %s 并行执行异常", task_id)
        try:
            await _finish(task_id, TASK_FAILED, f"任务执行异常：{type(exc).__name__}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        for worker in inflight.values():
            if not worker.done():
                worker.cancel()
        RUNNING.pop(task_id, None)
        if token is not None:
            io_log.reset_trace_id(token)


async def _execute(task_id: int, user: dict) -> None:
    """执行入口：默认按 WORKFLOW_MAX_PARALLEL 并行；并发为 1 时走 M8 串行基线。"""
    if _parallel_limit() <= 1:
        await _execute_serial(task_id, user)
    else:
        await _execute_parallel(task_id, user, _parallel_limit())


async def _execute_serial(task_id: int, user: dict) -> None:
    """串行执行主循环（M8 基线）；所有异常都收敛到任务状态。"""
    token = None
    task = await get_task(task_id)
    trace = task.get("trace_id") or io_log.get_trace_id()
    token = io_log.set_trace_id(trace)
    io_log.set_user_id(int(user.get("user_id") or 0))
    started = time.perf_counter()
    try:
        budget_seconds = int((task.get("budget") or {}).get("max_seconds") or settings.WORKFLOW_BUDGET_SECONDS)
        deadline = started + budget_seconds
        await _update_task(task_id, status=TASK_RUNNING)
        task["status"] = TASK_RUNNING
        _publish(task_id, {"type": "task_status", "status": TASK_RUNNING, "task": task})

        while True:
            task = await get_task(task_id)
            if task["status"] == TASK_CANCELLED:
                return
            if time.perf_counter() > deadline:
                await _finish(task_id, TASK_BUDGET, f"时间预算 {budget_seconds}s 已耗尽")
                return
            step = _next_pending(task["steps"])
            if step is None:
                pending_left = [s for s in task["steps"] if s["status"] == STEP_PENDING]
                failed = [s for s in task["steps"] if s["status"] == STEP_FAILED]
                if failed or pending_left:
                    await _finish(
                        task_id, TASK_FAILED,
                        "存在失败或被依赖阻塞的步骤：" + "、".join(
                            f"第 {s['step_index']} 步" for s in (failed + pending_left)
                        )[:300],
                    )
                else:
                    await _finish(task_id, TASK_COMPLETED)
                return

            # 人工确认：停在此步，等待 confirm 接口推进
            if step["requires_confirm"]:
                await _update_step(step["step_id"], status=STEP_AWAITING)
                await _update_task(task_id, status=TASK_AWAITING, current_step=step["step_index"])
                task_now = await get_task(task_id)
                _publish(task_id, {"type": "confirm_required", "step": step, "task": task_now})
                # 轮询确认结果，避免 Event 在「clear 之前已 set」的竞态丢信号
                waited = 0.0
                while waited < 600:
                    await asyncio.sleep(0.5)
                    waited += 0.5
                    task_now = await get_task(task_id)
                    if task_now["status"] == TASK_CANCELLED:
                        return
                    current = next(
                        (s for s in task_now["steps"] if s["step_id"] == step["step_id"]), None
                    )
                    if current and current["status"] != STEP_AWAITING:
                        break
                else:
                    await _update_step(step["step_id"], status=STEP_PENDING)
                    await _finish(task_id, TASK_FAILED, "等待人工确认超时（10 分钟）")
                    return
                task = await get_task(task_id)
                if task["status"] == TASK_CANCELLED:
                    return
                continue

            await _update_step(step["step_id"], status=STEP_RUNNING, started_at=_now())
            await _update_task(task_id, current_step=step["step_index"], status=TASK_RUNNING)
            _publish(task_id, {"type": "step_start", "step": step, "task": await get_task(task_id)})

            output = await _execute_step(step, user, task["goal"], trace, _context_of(task["steps"]))
            output = _truncate_output(output)
            ok = bool(output.get("ok"))
            if ok:
                await _update_step(
                    step["step_id"], status=STEP_DONE, output_json=_json(output),
                    finished_at=_now(), error_msg=None,
                )
                _log_step(task, step, output)
                _publish(task_id, {"type": "step_done", "step_index": step["step_index"], "output": output,
                                   "task": await get_task(task_id)})
            else:
                await _update_step(
                    step["step_id"], status=STEP_FAILED, output_json=_json(output),
                    error_msg=str(output.get("summary") or "执行失败")[:500], finished_at=_now(),
                )
                _log_step(task, step, output)
                _publish(task_id, {"type": "step_error", "step_index": step["step_index"],
                                   "error": output.get("summary"), "task": await get_task(task_id)})
                await _finish(
                    task_id, TASK_FAILED,
                    f"第 {step['step_index']} 步「{step['title']}」执行失败：{output.get('summary') or ''}"[:500],
                )
                return
            # LLM 反思（可选）：只记录判断，不改变执行结果；规则优先避免额外调用
            if bool(app_config.get_cached("WORKFLOW_LLM_REFLECT")):
                try:
                    judge = await asyncio.to_thread(
                        llm_chat,
                        [
                            {"role": "system", "content": "判断步骤结果是否满足预期，只输出 JSON："
                             '{"satisfied": bool, "reason": str}'},
                            {"role": "user", "content": f"预期：{step.get('expected') or '完成该步骤'}\n结果："
                              f"{str(output.get('summary') or '')[:800]}"},
                        ],
                        json_mode=True,
                        purpose="workflow_reflect",
                    )
                    _publish(task_id, {"type": "reflect", "step_index": step["step_index"], "raw": judge})
                except Exception:  # noqa: BLE001
                    pass
    except asyncio.CancelledError:
        task_now = None
        try:
            task_now = await get_task(task_id)
        except Exception:  # noqa: BLE001
            pass
        if task_now and task_now["status"] == TASK_CANCELLED:
            _publish(task_id, {"type": "task_done", "status": TASK_CANCELLED, "task": task_now})
        elif task_now and task_now["status"] == TASK_PAUSED:
            _publish(task_id, {"type": "task_status", "status": TASK_PAUSED, "task": task_now})
        else:
            await _update_task(task_id, status=TASK_INTERRUPTED,
                               error_msg="执行被中断，可点「继续」从当前步骤恢复")
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("工作流任务 %s 执行异常", task_id)
        try:
            await _finish(task_id, TASK_FAILED, f"任务执行异常：{type(exc).__name__}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        RUNNING.pop(task_id, None)
        if token is not None:
            io_log.reset_trace_id(token)


async def start_task(task_id: int, user: dict) -> dict:
    """开始/继续执行；幂等：已在运行直接返回当前快照。"""
    task = await get_task(task_id, user)
    if task["status"] in TASK_TERMINAL:
        return task
    if task["status"] == TASK_AWAITING and any(s["status"] == STEP_AWAITING for s in task["steps"]):
        # 是步骤级确认，不是初始计划确认：交给 confirm 接口
        return task
    running = RUNNING.get(task_id)
    if running and not running.done():
        return task
    # 恢复：把上次中断遗留的 running 步骤重置为 pending（已完成步骤不重跑）
    for step in task["steps"]:
        if step["status"] == STEP_RUNNING:
            await _update_step(step["step_id"], status=STEP_PENDING, started_at=None)
    await _update_task(task_id, status=TASK_RUNNING, finished_at=None, error_msg=None)
    RUNNING[task_id] = asyncio.create_task(_execute(task_id, user))
    return await get_task(task_id)


async def confirm_task(task_id: int, step_id: int, action: str, user: dict) -> dict:
    task = await get_task(task_id, user)
    allowed = {"continue", "skip", "cancel"}
    if action not in allowed:
        raise BizError(ERR_PARAM, "action 只支持 continue / skip / cancel")
    step = next((s for s in task["steps"] if s["step_id"] == int(step_id)), None)
    if step is None:
        raise BizError(40404, "步骤不存在", http_status=404)
    if action == "cancel":
        return await cancel_task(task_id, user)
    if step["status"] == STEP_DONE:
        return task
    if action == "skip":
        await _update_step(step["step_id"], status=STEP_SKIPPED,
                           error_msg="用户选择跳过",
                           output_json=_json({"ok": True, "summary": "用户选择跳过该步骤"}),
                           finished_at=_now())
    else:
        # 人工确认已消费：清掉 requires_confirm，避免执行器再次进入 awaiting_confirm
        await _update_step(step["step_id"], status=STEP_PENDING, requires_confirm=False)
    await _update_task(task_id, status=TASK_RUNNING)
    event = _CONFIRM_EVENTS.setdefault(task_id, asyncio.Event())
    event.set()
    if not RUNNING.get(task_id) or RUNNING[task_id].done():
        RUNNING[task_id] = asyncio.create_task(_execute(task_id, user))
    return await get_task(task_id)


async def cancel_task(task_id: int, user: dict) -> dict:
    task = await get_task(task_id, user)
    if task["status"] in TASK_TERMINAL:
        return task
    await _update_task(task_id, status=TASK_CANCELLED, finished_at=_now())
    await _update_step_running_as_cancelled(task_id)
    running = RUNNING.get(task_id)
    if running and not running.done():
        running.cancel()
    event = _CONFIRM_EVENTS.get(task_id)
    if event:
        event.set()
    task = await get_task(task_id)
    # 取消保留已完成步骤的结果：立即生成部分完成报告，用户回到任务中心仍可查看
    try:
        report = _deterministic_report(task)
        await _update_task(task_id, result_summary=report)
        task["result_summary"] = report
    except Exception:  # noqa: BLE001
        pass
    _publish(task_id, {"type": "task_done", "status": TASK_CANCELLED, "task": task})
    return task


async def _update_step_running_as_cancelled(task_id: int) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE workflow_step SET status = 'skipped',
                    error_msg = '任务已取消', finished_at = now()
                WHERE task_id = %(tid)s AND status IN ('pending','running','awaiting_confirm')
                """,
                {"tid": int(task_id)},
            )


async def pause_task(task_id: int, user: dict) -> dict:
    """暂停：取消后台协程并把当前 running 步骤退回 pending，点「继续」可恢复。"""
    task = await get_task(task_id, user)
    if task["status"] in TASK_TERMINAL or task["status"] == TASK_PAUSED:
        return task
    await _update_task(task_id, status=TASK_PAUSED, finished_at=_now(),
                       error_msg="用户暂停，可点「继续」从当前步骤恢复")
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE workflow_step SET status = 'pending', started_at = NULL
                WHERE task_id = %(tid)s AND status = 'running'
                """,
                {"tid": int(task_id)},
            )
    running = RUNNING.get(task_id)
    if running and not running.done():
        running.cancel()
    task = await get_task(task_id)
    _publish(task_id, {"type": "task_status", "status": TASK_PAUSED, "task": task})
    return task


async def resume_task(task_id: int, user: dict) -> dict:
    task = await get_task(task_id, user)
    if task["status"] not in (TASK_INTERRUPTED, TASK_PAUSED, TASK_FAILED, TASK_AWAITING):
        if task["status"] == TASK_RUNNING:
            return task
        raise BizError(ERR_PARAM, f"当前状态（{task['status']}）不允许继续")
    for step in task["steps"]:
        if step["status"] == STEP_RUNNING:
            await _update_step(step["step_id"], status=STEP_PENDING, started_at=None)
        if step["status"] == STEP_AWAITING:
            await _update_step(step["step_id"], status=STEP_PENDING)
    await _update_task(task_id, status=TASK_RUNNING, finished_at=None, error_msg=None)
    RUNNING[task_id] = asyncio.create_task(_execute(task_id, user))
    return await get_task(task_id)


def running_count() -> int:
    return len([t for t in RUNNING.values() if not t.done()])
