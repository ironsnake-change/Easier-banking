"""Subscription inspection and cancellation tools."""

from __future__ import annotations

from ._common import DEFAULT_USER_ID, db_session, make_result, make_rollback_token


def scan_subscriptions(user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    with db_session(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM subscriptions WHERE user_id=? ORDER BY sub_id", (user_id,)
        ).fetchall()]
    active = [row for row in rows if row["status"] == "ACTIVE"]
    monthly = round(sum(float(row["amount"]) for row in active), 2)
    annual = round(monthly * 12, 2)
    flagged = []
    for row in active:
        merchant = row["merchant"]
        reason = ""
        if any(word in merchant for word in ("视频", "云盘", "会员")) and float(row["amount"]) <= 30:
            reason = "低价会员建议确认是否仍在使用"
        if "试用" in merchant or "续费" in merchant:
            reason = "请留意试用或自动续费条款"
        if reason:
            flagged.append({"sub_id": row["sub_id"], "merchant": merchant, "reason": reason})
    return make_result(
        "scan_subscriptions", "订阅代扣管理",
        f"目前有 {len(active)} 项有效代扣，每月约 {monthly:.2f} 元，预计一年 {annual:.2f} 元。",
        data={"subscriptions": rows, "active_count": len(active), "monthly_total": monthly,
              "annual_estimate": annual, "risk_flags": flagged},
        ground_truth_numbers=[len(active), monthly, annual, *[r["amount"] for r in active]],
    )


def cancel_subscription(subscription_id_or_name: str, reason: str = "用户主动退订",
                        user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    with db_session(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='ACTIVE' ORDER BY sub_id", (user_id,)
        ).fetchall()]
        query = subscription_id_or_name.strip()
        matches = [row for row in rows if query == row["sub_id"] or query in row["merchant"]]
        if len(matches) != 1:
            return make_result("cancel_subscription", "订阅代扣管理",
                "没有找到唯一的有效代扣项目，请提供订阅编号或更具体的服务名称。",
                status="clarification_needed", risk_level="YELLOW", risk_reason="取消代扣需用户确认。",
                data={"matches": [{"sub_id": r["sub_id"], "merchant": r["merchant"]} for r in matches]})
        before = matches[0]
        connection.execute("UPDATE subscriptions SET status='CANCELLED' WHERE sub_id=? AND user_id=?",
                           (before["sub_id"], user_id))
        token = make_rollback_token("cancel_subscription", {"subscription": before})
    return make_result("cancel_subscription", "订阅代扣管理",
        f"已取消“{before['merchant']}”自动代扣。原因：{reason}。",
        risk_level="YELLOW", risk_reason="取消订阅会改变代扣授权，需二次确认。",
        data={"subscription_id": before["sub_id"], "merchant": before["merchant"], "status": "CANCELLED", "reason": reason},
        ground_truth_numbers=[before["amount"]], rollback_token=token)


def compensate_cancel_subscription(rollback_token: dict, db_path=None) -> dict:
    before = (rollback_token or {}).get("before_snapshot", {}).get("subscription")
    if not before:
        return make_result("compensate_cancel_subscription", "订阅代扣管理", "回滚凭据缺少订阅快照。", status="error")
    with db_session(db_path) as connection:
        connection.execute("UPDATE subscriptions SET status=? WHERE sub_id=? AND user_id=?",
                           (before["status"], before["sub_id"], before["user_id"]))
    return make_result("compensate_cancel_subscription", "订阅代扣管理", "已恢复原订阅状态。",
                       data={"subscription_id": before["sub_id"], "status": before["status"]})
