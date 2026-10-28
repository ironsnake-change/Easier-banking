"""Wealth product query, suitability, subscription, and redemption tools."""

from __future__ import annotations

import sqlite3
import uuid

from ._common import (
    DEFAULT_USER_ID, account_available, db_session, get_account, get_user,
    make_result, make_rollback_token, now_text, row_dict,
)

_RISK_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}


def _user_risk_rank(user: dict) -> int:
    level = (user.get("risk_level") or "").strip()
    if level.startswith("C1"):
        return 1
    if level.startswith("C2"):
        return 2
    if level.startswith("C3"):
        return 3
    if level.startswith("C4"):
        return 4
    if level.startswith("C5"):
        return 5
    return 2


def query_and_compare_wealth(risk_filter: str = None, compare_product_ids: list = None,
                             user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "query_and_compare_wealth"
    try:
        with db_session(db_path) as connection:
            products = [dict(row) for row in connection.execute("SELECT * FROM wealth_products ORDER BY product_id").fetchall()]
            holdings = [dict(row) for row in connection.execute("SELECT * FROM wealth_holdings WHERE user_id=? ORDER BY product_id", (user_id,)).fetchall()]
        if risk_filter:
            products = [p for p in products if p["risk_grade"] == risk_filter]
        if compare_product_ids:
            products = [p for p in products if p["product_id"] in compare_product_ids]
        held_map = {h["product_id"]: h for h in holdings}
        comparison = []
        for p in products:
            comparison.append({
                "product_id": p["product_id"], "name": p["name"], "risk_grade": p["risk_grade"],
                "expected_yield": p["expected_yield"], "min_amount": p["min_amount"], "term_days": p["term_days"],
                "held_amount": held_map.get(p["product_id"], {}).get("amount", 0.0),
                "accrued_profit": held_map.get(p["product_id"], {}).get("accrued_profit", 0.0),
            })
        return make_result(tool, "理财操作", f"共 {len(products)} 款产品可对比。", data={"products": comparison, "holdings": holdings, "count": len(products)}, ground_truth_numbers=[len(products), *[p["min_amount"] for p in products], *[p["term_days"] for p in products]])
    except Exception as exc:
        return make_result(tool, "理财操作", f"产品查询失败：{exc}", status="error", risk_level="RED", risk_reason="产品库读取异常。")


def evaluate_user_risk_profile(target_product_id: str = None,
                               user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "evaluate_user_risk_profile"
    try:
        with db_session(db_path) as connection:
            user = get_user(connection, user_id)
            product = row_dict(connection.execute("SELECT * FROM wealth_products WHERE product_id=?", (target_product_id,)).fetchone()) if target_product_id else None
        if not user:
            return make_result(tool, "理财操作", "用户不存在。", status="error", risk_level="RED", risk_reason="用户信息缺失。")
        rank = _user_risk_rank(user)
        profile = {"user_id": user["user_id"], "risk_level": user["risk_level"], "risk_rank": rank}
        if product:
            product_rank = _RISK_ORDER.get(product["risk_grade"], 99)
            matched = product_rank <= rank
            profile.update({"target_product_id": product["product_id"], "product_risk_grade": product["risk_grade"], "suitability_matched": matched})
            summary = f"产品 {product['name']} 风险等级 {product['risk_grade']}，" + ("与您的承受能力匹配。" if matched else "超出您的承受能力，存在适当性风险。")
            level = "GREEN" if matched else "RED"
            reason = "适当性匹配。" if matched else "用户风险等级低于产品风险等级，触发合规拦截警示。"
        else:
            summary = f"您的风险承受等级为 {user['risk_level']}。"
            level = "GREEN"
            reason = "用户风险画像查询。"
        return make_result(tool, "理财操作", summary, risk_level=level, risk_reason=reason, data=profile)
    except Exception as exc:
        return make_result(tool, "理财操作", f"风险评估失败：{exc}", status="error", risk_level="RED", risk_reason="适当性评估异常。")


def subscribe_wealth_product(product_id: str, amount: float, user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "subscribe_wealth_product"
    try:
        value = round(float(amount), 2)
        with db_session(db_path) as connection:
            user = get_user(connection, user_id)
            product = row_dict(connection.execute("SELECT * FROM wealth_products WHERE product_id=?", (product_id,)).fetchone())
            account = get_account(connection, user_id)
            if not user or not product or not account:
                return make_result(tool, "理财操作", "用户、产品或账户不存在。", status="error", risk_level="RED", risk_reason="申购数据缺失。")
            if value < float(product["min_amount"]):
                return make_result(tool, "理财操作", f"申购金额低于起购金额 {product['min_amount']:.2f} 元。", status="blocked", risk_level="RED", risk_reason="未达起购门槛。", ground_truth_numbers=[value, product["min_amount"]])
            if value > account_available(account):
                return make_result(tool, "理财操作", "账户可用余额不足。", status="blocked", risk_level="RED", risk_reason="可用余额不足。", ground_truth_numbers=[value, account_available(account)])
            if _RISK_ORDER.get(product["risk_grade"], 99) > _user_risk_rank(user):
                return make_result(tool, "理财操作", f"产品 {product['risk_grade']} 超出您的风险承受等级，已做适当性拦截。", status="blocked", risk_level="RED", risk_reason="适当性匹配校验未通过。", data={"suitability_matched": False, "user_risk_level": user["risk_level"], "product_risk_grade": product["risk_grade"]})
            holding = row_dict(connection.execute("SELECT * FROM wealth_holdings WHERE user_id=? AND product_id=? ORDER BY holding_id LIMIT 1", (user_id, product_id)).fetchone())
            before = {"account": account, "holding": holding}
            if holding:
                connection.execute("UPDATE wealth_holdings SET amount=amount+? WHERE holding_id=?", (value, holding["holding_id"]))
                holding_id = holding["holding_id"]
            else:
                holding_id = "HOLD_" + uuid.uuid4().hex[:12].upper()
                connection.execute("INSERT INTO wealth_holdings VALUES (?, ?, ?, ?, 0, ?)", (holding_id, user_id, product_id, value, now_text()))
                before["_delete_holding_id"] = holding_id
            balance_after = round(float(account["balance"]) - value, 2)
            connection.execute("UPDATE accounts SET balance=? WHERE account_id=?", (balance_after, account["account_id"]))
        return make_result(tool, "理财操作", f"已申购 {product['name']} {value:.2f} 元。", risk_level="RED", risk_reason="理财申购属红级操作，已通过适当性与余额校验。", data={"holding_id": holding_id, "product_id": product_id, "amount": value, "balance_after": balance_after}, ground_truth_numbers=[value, balance_after], rollback_token=make_rollback_token(tool, before))
    except (TypeError, ValueError) as exc:
        return make_result(tool, "理财操作", f"申购参数无效：{exc}", status="error", risk_level="RED", risk_reason="金额参数无效。")
    except sqlite3.Error as exc:
        return make_result(tool, "理财操作", f"申购失败：{exc}", status="error", risk_level="RED", risk_reason="数据库交易失败。")


def compensate_subscribe_wealth(rollback_token: dict, db_path=None) -> dict:
    tool = "subscribe_wealth_product"
    before = rollback_token.get("before_snapshot", {})
    try:
        with db_session(db_path) as connection:
            if before.get("account"):
                account = before["account"]
                connection.execute("UPDATE accounts SET balance=? WHERE account_id=?", (account["balance"], account["account_id"]))
            holding = before.get("holding")
            if holding:
                connection.execute("UPDATE wealth_holdings SET amount=? WHERE holding_id=?", (holding["amount"], holding["holding_id"]))
            elif before.get("_delete_holding_id"):
                connection.execute("DELETE FROM wealth_holdings WHERE holding_id=?", (before["_delete_holding_id"],))
        return make_result(tool, "Saga补偿", "已恢复账户余额并撤销新增/变动的理财持仓。", risk_level="YELLOW", risk_reason="恢复申购前快照。", data={"holding_id": (before.get("holding") or {}).get("holding_id") or before.get("_delete_holding_id")})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"申购回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")


def redeem_wealth_product(holding_id_or_product_id: str, amount: float = None,
                          user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "redeem_wealth_product"
    try:
        with db_session(db_path) as connection:
            holding = row_dict(connection.execute(
                "SELECT * FROM wealth_holdings WHERE user_id=? AND (holding_id=? OR product_id=?) ORDER BY holding_id LIMIT 1",
                (user_id, holding_id_or_product_id, holding_id_or_product_id),
            ).fetchone())
            account = get_account(connection, user_id)
            if not holding or not account:
                return make_result(tool, "理财操作", "未找到对应持仓或账户。", status="error", risk_level="RED", risk_reason="赎回数据缺失。")
            value = round(float(holding["amount"] if amount is None else amount), 2)
            if value <= 0 or value > float(holding["amount"]):
                return make_result(tool, "理财操作", "赎回金额超出当前持仓。", status="blocked", risk_level="RED", risk_reason="赎回金额无效。", ground_truth_numbers=[value, holding["amount"]])
            before = {"holding": holding, "account": account}
            remaining = round(float(holding["amount"]) - value, 2)
            connection.execute("UPDATE wealth_holdings SET amount=? WHERE holding_id=?", (remaining, holding["holding_id"]))
            balance_after = round(float(account["balance"]) + value, 2)
            connection.execute("UPDATE accounts SET balance=? WHERE account_id=?", (balance_after, account["account_id"]))
        level = "RED" if value > 5000 else "YELLOW"
        return make_result(tool, "理财操作", f"已赎回 {value:.2f} 元，资金退回活期账户。", risk_level=level, risk_reason="赎回额超5000元需红级验证。" if value > 5000 else "小额赎回需黄色确认。", data={"holding_id": holding["holding_id"], "redeemed": value, "remaining_holding": remaining, "balance_after": balance_after}, ground_truth_numbers=[value, remaining, balance_after], rollback_token=make_rollback_token(tool, before))
    except (TypeError, ValueError) as exc:
        return make_result(tool, "理财操作", f"赎回参数无效：{exc}", status="error", risk_level="RED", risk_reason="金额参数无效。")
    except sqlite3.Error as exc:
        return make_result(tool, "理财操作", f"赎回失败：{exc}", status="error", risk_level="RED", risk_reason="数据库交易失败。")


def compensate_redeem_wealth(rollback_token: dict, db_path=None) -> dict:
    tool = "redeem_wealth_product"
    before = rollback_token.get("before_snapshot", {})
    try:
        with db_session(db_path) as connection:
            holding = before.get("holding")
            account = before.get("account")
            if holding:
                connection.execute("UPDATE wealth_holdings SET amount=? WHERE holding_id=?", (holding["amount"], holding["holding_id"]))
            if account:
                connection.execute("UPDATE accounts SET balance=? WHERE account_id=?", (account["balance"], account["account_id"]))
        return make_result(tool, "Saga补偿", "已恢复原持仓金额并扣回已退还的活期余额。", risk_level="YELLOW", risk_reason="恢复赎回前快照。", data={"holding_id": (holding or {}).get("holding_id")})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"赎回回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")
