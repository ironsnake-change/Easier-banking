"""Central registry and deterministic risk precheck for implemented scenario tools."""

from __future__ import annotations

import inspect
from typing import Any

from core import database
from core.config import DAILY_SMALL_TRANSFER_LIMIT
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
    amount = 0.0
    if tool_name in {"execute_transfer", "schedule_transfer"}:
        amount = float(args.get("amount", 0) or 0)
        rows = database.fetch_all("SELECT daily_transfer_used FROM users WHERE user_id=?", (user_id,))
        used = float(rows[0]["daily_transfer_used"]) if rows else 0.0
        daily_after = used + amount
        payee = str(args.get("payee_id_or_account", ""))
        contacts = database.fetch_all("SELECT contact_id,is_risky,name,alias FROM contacts WHERE user_id=?", (user_id,))
        matched = [c for c in contacts if payee == c["contact_id"] or payee == c["name"] or payee in c["alias"].split(",")]
        if any(int(c["is_risky"]) for c in matched) or any(term in str(args.get("remark", "")) for term in ("安全账户", "公检法", "验资", "解冻费", "高息返利")):
            level, reason, blocked = "RED", "收款人或用途命中反诈高危规则。", True
        elif len(matched) > 1:
            level, reason = "YELLOW", "联系人重名，需要先消歧确认。"
        elif daily_after > DAILY_SMALL_TRANSFER_LIMIT:
            level, reason = "RED", f"今日累计将达到 {daily_after:.2f} 元，超过 1000 元阈值，需要短信与人脸验证。"
        elif amount > 0:
            level, reason = "YELLOW", "转账需要用户确认，且今日累计不超过 1000 元。"
    elif tool_name == "redeem_wealth_product":
        amount = float(args.get("amount", 0) or 0)
        level, reason = ("RED", "赎回金额超过 5000 元，需强验证。") if amount > 5000 else ("YELLOW", "理财赎回需二次确认。")
    elif tool_name == "subscribe_wealth_product":
        level, reason = "RED", "理财申购需适当性复核与强验证。"
    elif tool_name == "report_card_loss":
        level, reason = "RED", "银行卡挂失属于敏感操作，需强验证。"
    elif tool_name == "manage_card_limits_and_lock":
        action = args.get("action")
        level = "RED" if action == "unlock" or (action == "overseas_switch" and not args.get("overseas_switch")) else "YELLOW"
        reason = "卡片保护设置变更需用户确认。"
    elif tool_name in {"cancel_subscription", "lock_demand_funds", "create_lifestyle_order"}:
        level, reason = "YELLOW", "该操作会改变代扣、账户资金或创建订单，需要用户确认。"
    elif tool_name in {"apply_virtual_card", "schedule_transfer", "split_aa_bill"}:
        level, reason = "YELLOW", "该操作需要用户二次确认。"
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
