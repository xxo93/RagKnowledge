"""计算器工具安全与正确性单测（验收标准第 4 条：1234*56 = 69104）。

运行（backend/ 目录下）：
    python -m pytest tests/test_calc_tool.py -v
本用例无外部依赖、不连数据库。
"""
from agent.tools import calc_tool


def test_basic_multiply_matches_acceptance():
    res = calc_tool.calculate("1234*56")
    assert res["ok"] is True
    assert res["result"] == 69104


def test_parentheses_and_precedence():
    assert calc_tool.calculate("(1+2)*3")["result"] == 9
    assert calc_tool.calculate("10 - 4 / 2")["result"] == 8


def test_division_zero_rejected():
    res = calc_tool.calculate("1/0")
    assert res["ok"] is False
    assert "0" in res["error"]


def test_function_call_rejected():
    # 禁止函数调用（如 eval 内置风险）
    assert calc_tool.calculate("__import__('os')")["ok"] is False
    assert calc_tool.calculate("abs(-5)")["ok"] is False


def test_variable_or_name_rejected():
    assert calc_tool.calculate("x+1")["ok"] is False


def test_non_arithmetic_chars_rejected():
    assert calc_tool.calculate("1; drop table x")["ok"] is False
    assert calc_tool.calculate("2**8")["ok"] is False      # 幂运算不在白名单
    assert calc_tool.calculate("")["ok"] is False
