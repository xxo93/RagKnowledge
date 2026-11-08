"""计算器工具（F-05 / 架构 §4.5 安全设计）。

安全措施（多层防护，面试可讲）：
1. 正则白名单：仅允许数字、空白、+ - * / ( ) .；长度 ≤ 200；
2. AST 白名单：仅 Expression / BinOp / UnaryOp / Constant，运算符仅 Add Sub Mult Div；
   拒绝 Name（变量/内置函数）、Call（函数调用）、Attribute（属性访问）、幂运算、取模等；
3. 不使用 eval，采用 AST 递归求值，从根上杜绝任意代码执行；
4. 除零捕获、结果有限性（isfinite）校验。

工具出参契约（§4.4）：{"ok": bool, "expression": str, "result": float, "error": str|None}
"""
from __future__ import annotations

import ast
import math
import re
from typing import Any

# 允许的字符：数字、小数点、空白、四则运算符、括号
_ALLOWED_RE = re.compile(r"^[\d\s+\-*/().]+$")
_MAX_LEN = 200

# 允许的 AST 运算符映射到 Python 求值函数
_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}
_UNARY = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


def _eval_node(node: ast.AST) -> Any:
    """递归求值 AST 节点；遇到未授权节点直接抛 ValueError。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("仅支持数字常量")

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _OPS:
            raise ValueError(f"不支持的运算符：{op_type.__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return _OPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY:
            raise ValueError(f"不支持的一元运算符：{op_type.__name__}")
        return _UNARY[op_type](_eval_node(node.operand))

    # 显式拒绝 Name / Call / Attribute / Subscript / Pow / Mod 等
    raise ValueError("表达式包含不允许的语法元素")


def calculate(expression: str) -> dict:
    """对外工具入口。失败返回 ok=False + error，不抛异常给 Agent 图（§4.4 失败语义）。"""
    expr = (expression or "").strip()
    if not expr:
        return {"ok": False, "expression": expr, "result": None, "error": "表达式为空"}
    if len(expr) > _MAX_LEN:
        return {"ok": False, "expression": expr, "result": None, "error": "表达式过长"}
    if not _ALLOWED_RE.match(expr):
        return {
            "ok": False,
            "expression": expr,
            "result": None,
            "error": "仅允许数字与 + - * / ( ) . 运算，禁止函数、变量与其他符号",
        }

    try:
        tree = ast.parse(expr, mode="eval")
        result = _eval_node(tree)
    except ZeroDivisionError:
        return {"ok": False, "expression": expr, "result": None, "error": "除数不能为 0"}
    except (ValueError, SyntaxError, TypeError) as exc:
        return {"ok": False, "expression": expr, "result": None, "error": f"表达式非法：{exc}"}

    if not math.isfinite(result):
        return {"ok": False, "expression": expr, "result": None, "error": "结果不是有限数值"}

    # 整数结果规整，避免 69104.0 这类展示
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return {"ok": True, "expression": expr, "result": result, "error": None}
