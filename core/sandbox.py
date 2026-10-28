"""Small AST-checked calculation sandbox for trusted, data-only analysis."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
import sys
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


class SandboxSecurityError(ValueError):
    """Raised when submitted code is outside the calculation sandbox policy."""


_FORBIDDEN_NODES = {
    ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Delete, ast.Try,
    ast.Raise, ast.With, ast.AsyncWith, ast.AsyncFor, ast.AsyncFunctionDef,
    ast.ClassDef,
}
_SAFE_BUILTIN_NAMES = {
    "sum", "min", "max", "len", "round", "abs", "sorted", "float", "int",
    "str", "bool", "list", "dict", "set", "tuple", "enumerate", "zip",
    "any", "all", "range",
}
_SAFE_METHODS = {"get", "keys", "values", "items", "count", "index"}
_SAFE_NODE_TYPES = {
    ast.Module, ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.UAdd, ast.USub, ast.Not, ast.Compare, ast.Eq, ast.NotEq, ast.Lt,
    ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.BoolOp, ast.And, ast.Or, ast.If, ast.IfExp, ast.For, ast.Break,
    ast.Continue, ast.Pass, ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.Subscript, ast.Slice, ast.Attribute, ast.Call, ast.keyword,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.comprehension, ast.JoinedStr, ast.FormattedValue,
}
_MAX_AST_NODES = 1200
_MAX_INPUT_BYTES = 1_000_000
_MAX_STEPS = 500_000
_MAX_RANGE_LENGTH = 100_000
_MAX_SEQUENCE_LENGTH = 1_000_000


def _bounded_range(*args: int) -> range:
    result = range(*args)
    if len(result) > _MAX_RANGE_LENGTH:
        raise SandboxSecurityError(f"range 长度超过上限 {_MAX_RANGE_LENGTH}")
    return result


SAFE_BUILTINS = {
    "sum": sum, "min": min, "max": max, "len": len, "round": round,
    "abs": abs, "sorted": sorted, "float": float, "int": int, "str": str,
    "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "enumerate": enumerate, "zip": zip, "any": any, "all": all,
    "range": _bounded_range,
}


def _safe_binary(operator_name: str, left: Any, right: Any) -> Any:
    operations = {
        "Add": operator.add, "Sub": operator.sub, "Mult": operator.mul,
        "Div": operator.truediv, "FloorDiv": operator.floordiv,
        "Mod": operator.mod,
    }
    if operator_name not in operations:
        raise SandboxSecurityError(f"不允许的运算符：{operator_name}")
    if operator_name == "Mult":
        if isinstance(left, (str, list, tuple)) and isinstance(right, int):
            if len(left) * max(0, right) > _MAX_SEQUENCE_LENGTH:
                raise SandboxSecurityError("乘法结果超过序列长度上限")
        if isinstance(right, (str, list, tuple)) and isinstance(left, int):
            if len(right) * max(0, left) > _MAX_SEQUENCE_LENGTH:
                raise SandboxSecurityError("乘法结果超过序列长度上限")
        if isinstance(left, int) and isinstance(right, int):
            if left.bit_length() + right.bit_length() > 1_000_000:
                raise SandboxSecurityError("乘法结果超过整数位数上限")
    if operator_name == "Add" and isinstance(left, (str, list, tuple)) and isinstance(right, type(left)):
        if len(left) + len(right) > _MAX_SEQUENCE_LENGTH:
            raise SandboxSecurityError("拼接结果超过序列长度上限")
    result = operations[operator_name](left, right)
    if isinstance(result, int) and result.bit_length() > 1_000_000:
        raise SandboxSecurityError("整数结果超过位数上限")
    if isinstance(result, (str, list, tuple, dict, set)) and len(result) > _MAX_SEQUENCE_LENGTH:
        raise SandboxSecurityError("计算结果超过大小上限")
    return result


class _BinaryOperationGuard(ast.NodeTransformer):
    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        node = self.generic_visit(node)
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__safe_binary", ctx=ast.Load()),
                args=[ast.Constant(type(node.op).__name__), node.left, node.right],
                keywords=[],
            ),
            node,
        )


def validate_ast(code: str) -> bool:
    """Parse and enforce a strict allowlist; return True when accepted."""
    if not isinstance(code, str) or not code.strip():
        raise SandboxSecurityError("待执行代码必须是非空字符串")
    if len(code.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise SandboxSecurityError("代码长度超过安全上限")
    try:
        tree = ast.parse(code, mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise SandboxSecurityError(f"Python 语法解析失败：{exc}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > _MAX_AST_NODES:
        raise SandboxSecurityError(f"语法树节点数超过安全上限 {_MAX_AST_NODES}")
    for node in nodes:
        if type(node) in _FORBIDDEN_NODES:
            raise SandboxSecurityError(f"禁止使用语法节点：{type(node).__name__}")
        if type(node) not in _SAFE_NODE_TYPES:
            raise SandboxSecurityError(f"不在白名单中的语法节点：{type(node).__name__}")
        if isinstance(node, ast.Name) and (node.id.startswith("__") or node.id.endswith("__")):
            raise SandboxSecurityError(f"禁止访问双下划线名称：{node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr.endswith("__"):
                raise SandboxSecurityError(f"禁止访问双下划线属性：{node.attr}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _SAFE_BUILTIN_NAMES:
                    raise SandboxSecurityError(f"禁止调用函数：{node.func.id}")
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr not in _SAFE_METHODS:
                    raise SandboxSecurityError(f"禁止调用对象方法：{node.func.attr}")
            else:
                raise SandboxSecurityError("只允许调用白名单内置函数或只读容器方法")
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value.bit_length() > 128:
            raise SandboxSecurityError("整数常量超过安全上限")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            raise SandboxSecurityError("不允许指数运算")
    if not any(isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and
               any(isinstance(target, ast.Name) and target.id == "result"
                   for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))
               for node in nodes):
        raise SandboxSecurityError("代码必须将最终结果赋值给变量 result")
    return True


def run_in_sandbox(code: str, context_data: dict, timeout_sec: float = 2.0) -> dict:
    """Execute accepted calculations with JSON-only copied input and time limits."""
    try:
        validate_ast(code)
        if not isinstance(context_data, dict):
            raise SandboxSecurityError("context_data 必须是字典")
        encoded_context = json.dumps(context_data, ensure_ascii=False, allow_nan=False)
        if len(encoded_context.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise SandboxSecurityError("输入数据超过安全上限")
        safe_locals = json.loads(encoded_context)
        tree = ast.parse(code, mode="exec")
        transformed = _BinaryOperationGuard().visit(tree)
        ast.fix_missing_locations(transformed)
        compiled = compile(transformed, "<banking-sandbox>", "exec")
        safe_globals = {"__builtins__": SAFE_BUILTINS, "__safe_binary": _safe_binary}
        timeout = max(0.01, min(float(timeout_sec), 10.0))
        deadline = time.monotonic() + timeout
        outcome: dict[str, Any] = {}

        def worker() -> None:
            steps = 0

            def trace(frame, event, arg):
                nonlocal steps
                if frame.f_code.co_filename == "<banking-sandbox>":
                    steps += 1
                    if steps > _MAX_STEPS or time.monotonic() > deadline:
                        raise TimeoutError("沙箱执行超时或超过指令步数上限")
                    frame.f_trace_opcodes = True
                return trace

            sys.settrace(trace)
            try:
                exec(compiled, safe_globals, safe_locals)
                if "result" not in safe_locals:
                    raise SandboxSecurityError("执行后未生成 result")
                outcome["result"] = json.loads(json.dumps(safe_locals["result"], ensure_ascii=False, allow_nan=False))
            except BaseException as exc:
                outcome["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                sys.settrace(None)

        thread = threading.Thread(target=worker, name="banking-sandbox", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            return {"success": False, "error": "TimeoutError: 沙箱执行超时", "ast_verified": False}
        if "error" in outcome:
            return {"success": False, "error": outcome["error"], "ast_verified": False}
        return {
            "success": True, "result": outcome["result"],
            "ast_verified": True, "executed_code": code,
        }
    except BaseException as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}", "ast_verified": False}


_DATE_PATTERN = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b")
_NUMBER_PATTERN = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\w.])")


def _decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("%", "")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError, TypeError):
        return None


def verify_numerical_grounding(response_text: str, ground_truth_numbers: list) -> dict:
    """Compare numbers mentioned in text against tool-provided numeric facts."""
    if not isinstance(response_text, str):
        response_text = str(response_text)
    text_without_dates = _DATE_PATTERN.sub(" ", response_text)
    matched_numbers = []
    unverified_numbers = []
    grounded_numbers = []
    truth = [_decimal_value(value) for value in ground_truth_numbers]
    truth = [value for value in truth if value is not None]
    for match in _NUMBER_PATTERN.finditer(text_without_dates):
        raw = match.group(0)
        value = _decimal_value(raw)
        if value is None:
            continue
        display: int | float = float(raw.replace(",", "").replace("%", ""))
        if display.is_integer():
            display = int(display)
        matched_numbers.append(display)
        if any(abs(value - known) <= Decimal("0.005") for known in truth):
            grounded_numbers.append(display)
        else:
            unverified_numbers.append(display)
    return {
        "passed": not unverified_numbers,
        "unverified_numbers": unverified_numbers,
        "grounded_numbers": grounded_numbers,
    }
