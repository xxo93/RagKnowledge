"""工作流 Agent 单元测试（v3 / WP4）。

覆盖 planner 强校验（步数 / 工具白名单 / 参数 / 环 / 危险动作 / 权限裁剪）、
拓扑选择与输出截断；端到端工具执行由验收脚本覆盖。
"""
import json

import pytest

from backend.errors import BizError
from backend.services import workflow


def _raw(steps):
    return {"goal": "测试任务", "steps": steps}


def _step(sid, tool="knowledge_search", args=None, deps=None, **extra):
    return {
        "id": sid,
        "title": extra.get("title", f"步骤 {sid}"),
        "tool": tool,
        "args": args or {"query": "退款流程"},
        "depends_on": deps or [],
        "requires_confirm": extra.get("requires_confirm", False),
        "expected": extra.get("expected", "有结果"),
    }


def test_plan_rejects_too_many_steps():
    steps = [_step(f"s{i}") for i in range(1, 8)]
    with pytest.raises(BizError) as exc:
        workflow._validate_plan(_raw(steps), {"role": "admin"}, 6)
    assert "拆分为两个" in exc.value.msg


def test_plan_rejects_unknown_tool_and_missing_args():
    with pytest.raises(BizError):
        workflow._validate_plan(_raw([_step("s1", tool="shell")]), {"role": "admin"}, 6)
    with pytest.raises(BizError):
        workflow._validate_plan(
            _raw([_step("s1", args={"query": ""})]), {"role": "admin"}, 6
        )
    with pytest.raises(BizError):
        workflow._validate_plan(
            _raw([_step("s1", tool="calculator", args={"expression": "__import__('os')"})]),
            {"role": "admin"}, 6,
        )


def test_plan_rejects_dependency_forward_and_cycle():
    with pytest.raises(BizError):
        workflow._validate_plan(_raw([_step("s1", deps=["s2"]), _step("s2")]), {"role": "admin"}, 6)


def test_plan_rejects_dangerous_sql_and_shell():
    for query in ("select * from users", "rm -rf /tmp/x", "DROP TABLE chat_record"):
        with pytest.raises(BizError):
            workflow._validate_plan(_raw([_step("s1", args={"query": query})]), {"role": "admin"}, 6)


def test_member_salary_query_marked_skipped_but_plan_valid():
    plan, steps = workflow._validate_plan(
        _raw([_step("s1", tool="biz_query", args={"query": "查询全员薪资表"})]),
        {"role": "member"}, 6,
    )
    assert plan["goal"] == "测试任务"
    assert steps[0]["skipped_reason"] and "无权限" in steps[0]["skipped_reason"]


def test_admin_salary_query_not_skipped():
    _, steps = workflow._validate_plan(
        _raw([_step("s1", tool="biz_query", args={"query": "查询全员薪资表"})]),
        {"role": "admin"}, 6,
    )
    assert steps[0]["skipped_reason"] is None


def test_next_pending_respects_topology_and_skips_failed_dependents():
    steps = [
        {"step_index": 1, "status": "done", "depends_on": []},
        {"step_index": 2, "status": "pending", "depends_on": [1]},
        {"step_index": 3, "status": "pending", "depends_on": [2]},
    ]
    assert workflow._next_pending(steps)["step_index"] == 2
    steps[1]["status"] = "failed"
    assert workflow._next_pending(steps) is None


def test_ready_steps_returns_all_independent_pending():
    steps = [
        {"step_id": 1, "step_index": 1, "status": "done", "depends_on": []},
        {"step_id": 2, "step_index": 2, "status": "pending", "depends_on": []},
        {"step_id": 3, "step_index": 3, "status": "pending", "depends_on": []},
        {"step_id": 4, "step_index": 4, "status": "pending", "depends_on": [1]},
        {"step_id": 5, "step_index": 5, "status": "pending", "depends_on": [2]},
    ]
    ready = workflow._ready_steps(steps)
    assert [s["step_index"] for s in ready] == [2, 3, 4]
    # 已在执行的步骤不重复投递
    assert [s["step_index"] for s in workflow._ready_steps(steps, {2})] == [3, 4]


def test_ready_steps_blocks_failed_dependency_branch():
    steps = [
        {"step_id": 1, "step_index": 1, "status": "failed", "depends_on": []},
        {"step_id": 2, "step_index": 2, "status": "pending", "depends_on": [1]},
        {"step_id": 3, "step_index": 3, "status": "pending", "depends_on": []},
    ]
    assert [s["step_index"] for s in workflow._ready_steps(steps)] == [3]


def test_parallel_limit_reads_config_and_clamps(monkeypatch):
    monkeypatch.setattr(workflow.app_config, "get_cached", lambda key: {"WORKFLOW_PARALLEL": True,
                                                                        "WORKFLOW_MAX_PARALLEL": 9}.get(key))
    assert workflow._parallel_limit() == 4
    monkeypatch.setattr(workflow.app_config, "get_cached", lambda key: {"WORKFLOW_PARALLEL": True,
                                                                        "WORKFLOW_MAX_PARALLEL": 0}.get(key))
    assert workflow._parallel_limit() == 1
    monkeypatch.setattr(workflow.app_config, "get_cached", lambda key: False if key == "WORKFLOW_PARALLEL" else 2)
    assert workflow._parallel_limit() == 1


def test_truncate_output_keeps_summary_and_marks_truncated():
    huge = {"ok": True, "summary": "x" * 20000, "table": {"rows": [{"a": "y" * 1000}]}}
    out = workflow._truncate_output(huge, limit=1024)
    assert out["truncated"] is True
    assert out["original_chars"] > 1024
    assert len(json.dumps(out, ensure_ascii=False)) <= 2048
