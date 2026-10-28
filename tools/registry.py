"""Central registry and deterministic risk precheck for implemented scenario tools."""

from __future__ import annotations

import inspect
from typing import Any

from core import policy
from . import bill_tools, card_tools, cross_scene_tools, subscription_tools, transfer_tools, wealth_tools


_FUNCTIONS = {
    "resolve_contact": transfer_tools.resolve_contact,
    "execute_transfer": transfer_tools.execute_transfer,
    "schedule_transfer": transfer_tools.schedule_transfer,
    "split_aa_bill": transfer_tools.split_aa_bill,
    "analyze_bills_in_sandbox": bill_tools.analyze_bills_in_sandbox,
    "detect_abnormal_transactions": bill_tools.detect_abnormal_transactions,
    "generate_bill_report": bill_tools.generate_bill_report,
    "query_and_compare_wealth": wealth_tools.query_and_compare_wealth,
    "evaluate_user_risk_profile": wealth_tools.evaluate_user_risk_profile,
    "subscribe_wealth_product": wealth_tools.subscribe_wealth_product,
    "redeem_wealth_product": wealth_tools.redeem_wealth_product,
    "query_cards_status": card_tools.query_cards_status,
    "apply_virtual_card": card_tools.apply_virtual_card,
    "manage_card_limits_and_lock": card_tools.manage_card_limits_and_lock,
    "report_card_loss": card_tools.report_card_loss,
    "scan_subscriptions": subscription_tools.scan_subscriptions,
    "cancel_subscription": subscription_tools.cancel_subscription,
    "lock_demand_funds": cross_scene_tools.lock_demand_funds,
    "create_lifestyle_order": cross_scene_tools.create_lifestyle_order,
}
_COMPENSATORS = {
    "execute_transfer": transfer_tools.compensate_execute_transfer,
    "schedule_transfer": transfer_tools.compensate_schedule_transfer,
    "split_aa_bill": transfer_tools.compensate_split_aa_bill,
    "subscribe_wealth_product": wealth_tools.compensate_subscribe_wealth,
    "redeem_wealth_product": wealth_tools.compensate_redeem_wealth,
    "apply_virtual_card": card_tools.compensate_apply_virtual_card,
    "manage_card_limits_and_lock": card_tools.compensate_manage_card_limits_and_lock,
    "report_card_loss": card_tools.compensate_report_card_loss,
    "cancel_subscription": subscription_tools.compensate_cancel_subscription,
    "lock_demand_funds": cross_scene_tools.compensate_lock_demand_funds,
    "create_lifestyle_order": cross_scene_tools.compensate_create_lifestyle_order,
}


def _schema_for(name: str, func) -> dict:
    properties = {}
    required = []
    for parameter in inspect.signature(func).parameters.values():
        if parameter.name in {"user_id", "db_path"}:
            continue
        annotation = parameter.annotation
        kind = "number" if annotation in {float, int} or parameter.name in {"amount", "total_amount", "limit_amount", "new_daily_limit"} else "boolean" if annotation is bool else "array" if annotation is list else "string"
        item = {"type": kind}
        if kind == "array":
            item["items"] = {"type": "string"}
        properties[parameter.name] = item
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
    return {"type": "function", "function": {"name": name, "description": f"Easier-banking 场景工具：{name}",
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


TOOL_REGISTRY = {
    name: {"func": func, "compensator": _COMPENSATORS.get(name), "schema": _schema_for(name, func)}
    for name, func in _FUNCTIONS.items()
}
TOOL_SCHEMAS = [entry["schema"] for entry in TOOL_REGISTRY.values()]


def get_openai_tool_schemas() -> list[dict]:
    return [dict(schema) for schema in TOOL_SCHEMAS]


def evaluate_tool_risk(tool_name: str, arguments: dict | None = None, user_id: str = "U1001") -> dict:
    args = dict(arguments or {})
    level, reason, blocked = "GREEN", "只读查询或低风险操作。", False
    classified = policy.classify_tool(user_id, tool_name, args)
    if classified:
        policy_level = str(classified.get("level", "L3"))
        level = "RED" if policy_level == "L3" else "YELLOW"
        reason = "；".join(classified.get("reasons") or ["该操作需要用户确认。"])
    payee = str(args.get("payee_id_or_account", "")).lower()
    remark = str(args.get("remark", "")).lower()
    if any(term in payee + remark for term in ("安全账户", "公检法", "验资", "解冻费", "高息返利")):
        level, reason, blocked = "RED", "收款人或用途命中反诈高危规则。", True
    if tool_name not in TOOL_REGISTRY:
        blocked, level, reason = True, "RED", "工具未注册，已按默认拒绝策略拦截。"
    return {"tool_name": tool_name, "risk_level": level, "require_confirmation": level != "GREEN",
            "require_mfa": level == "RED", "risk_reason": reason, "blocked": blocked,
            "block_reason": reason if blocked else ""}


def execute_tool(tool_name: str, arguments: dict | None = None, user_id: str = "U1001", *, mfa_verified: bool = False) -> dict:
    if tool_name not in _FUNCTIONS:
        return {"status": "blocked", "tool_name": tool_name, "summary": "工具未注册。", "data": {}, "rollback_token": None}
    args = dict(arguments or {})
    args.pop("db_path", None)
    signature = inspect.signature(_FUNCTIONS[tool_name])
    if "user_id" in signature.parameters:
        args["user_id"] = user_id
    if "mfa_verified" in signature.parameters:
        args["mfa_verified"] = bool(mfa_verified)
    try:
        return _FUNCTIONS[tool_name](**args)
    except Exception as exc:
        return {"status": "error", "tool_name": tool_name, "summary": f"工具执行失败：{exc}", "data": {}, "rollback_token": None}


def compensate_tool_action(rollback_token: dict) -> dict:
    if not isinstance(rollback_token, dict):
        return {"status": "error", "summary": "回滚凭据格式无效。"}
    name = rollback_token.get("tool_name")
    compensator = _COMPENSATORS.get(name)
    if compensator is None:
        return {"status": "error", "summary": f"工具 {name} 没有注册补偿函数。"}
    return compensator(rollback_token)


__all__ = ["TOOL_REGISTRY", "TOOL_SCHEMAS", "get_openai_tool_schemas", "evaluate_tool_risk", "execute_tool", "compensate_tool_action"]
