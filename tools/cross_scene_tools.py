"""Cross-scene fund reservation and lifestyle order tools with Saga undo."""

from __future__ import annotations

import uuid

from ._common import (DEFAULT_USER_ID, account_available, db_session, json_text,
                      make_result, make_rollback_token, now_text, days_later_text)


def lock_demand_funds(amount: float, purpose: str = "专项资金预留锁定", lock_days: int = 7,
                      user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    amount = round(float(amount), 2)
    if amount <= 0 or lock_days < 1:
        return make_result("lock_demand_funds", "跨场景联动", "金额和锁定天数必须为正数。", status="error", risk_level="YELLOW")
    with db_session(db_path) as connection:
        account = connection.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
        if not account:
            return make_result("lock_demand_funds", "跨场景联动", "未找到可用账户。", status="error", risk_level="YELLOW")
        account = dict(account)
        available = account_available(account)
        if available < amount:
            return make_result("lock_demand_funds", "跨场景联动", "活期可用余额不足，暂时无法预留。", status="blocked",
                               risk_level="YELLOW", data={"available_balance": available, "requested": amount},
                               ground_truth_numbers=[available, amount])
        task_id = "TASK_" + uuid.uuid4().hex[:12].upper()
        before = {"account": account, "task_id": task_id}
        connection.execute("UPDATE accounts SET locked_balance=locked_balance+? WHERE account_id=?", (amount, account["account_id"]))
        connection.execute("INSERT INTO scheduled_tasks (task_id,user_id,task_type,description,amount,target_date,status,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, user_id, "FUND_LOCK", purpose, amount, days_later_text(lock_days), "ACTIVE",
             json_text({"purpose": purpose, "lock_days": lock_days}), now_text()))
        token = make_rollback_token("lock_demand_funds", before)
    return make_result("lock_demand_funds", "跨场景联动", f"已为您预留 {amount:.2f} 元专项资金。",
        risk_level="YELLOW", risk_reason="专项资金锁定会暂时减少可用余额，需要确认。",
        data={"task_id": task_id, "amount": amount, "purpose": purpose, "available_before": available,
              "available_after": round(available-amount, 2), "target_date": days_later_text(lock_days)},
        ground_truth_numbers=[amount, available, round(available-amount, 2)], rollback_token=token)


def compensate_lock_demand_funds(rollback_token: dict, db_path=None) -> dict:
    snap = (rollback_token or {}).get("before_snapshot", {})
    account, task_id = snap.get("account"), snap.get("task_id")
    if not account or not task_id:
        return make_result("compensate_lock_demand_funds", "跨场景联动", "回滚凭据缺少账户或任务快照。", status="error")
    with db_session(db_path) as connection:
        connection.execute("UPDATE accounts SET balance=?,locked_balance=? WHERE account_id=?",
                           (account["balance"], account["locked_balance"], account["account_id"]))
        connection.execute("UPDATE scheduled_tasks SET status='ROLLED_BACK' WHERE task_id=?", (task_id,))
    return make_result("compensate_lock_demand_funds", "跨场景联动", "已解除专项资金锁定并恢复原账户余额。",
                       ground_truth_numbers=[account["balance"], account["locked_balance"]])


def create_lifestyle_order(service_type: str, title: str, amount: float, use_locked_funds: bool = True,
                           remark: str = "", user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    amount = round(float(amount), 2)
    if amount <= 0:
        return make_result("create_lifestyle_order", "跨场景联动", "订单金额必须大于零。", status="error", risk_level="YELLOW")
    with db_session(db_path) as connection:
        account_row = connection.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
        if not account_row:
            return make_result("create_lifestyle_order", "跨场景联动", "未找到可用账户。", status="error", risk_level="YELLOW")
        account = dict(account_row)
        locked_before = float(account["locked_balance"])
        available_before = account_available(account)
        use_locked = bool(use_locked_funds and locked_before >= amount)
        if use_locked:
            connection.execute("UPDATE accounts SET balance=balance-?,locked_balance=locked_balance-? WHERE account_id=?",
                               (amount, amount, account["account_id"]))
        elif available_before >= amount:
            connection.execute("UPDATE accounts SET balance=balance-? WHERE account_id=?", (amount, account["account_id"]))
        else:
            return make_result("create_lifestyle_order", "跨场景联动", "可用余额或足额专项锁定资金不足。", status="blocked",
                risk_level="YELLOW", data={"available_balance": available_before, "locked_balance": locked_before, "requested": amount},
                ground_truth_numbers=[available_before, locked_before, amount])
        task_id = "TASK_" + uuid.uuid4().hex[:12].upper()
        transaction_id = "TX_" + uuid.uuid4().hex[:16].upper()
        before = {"account": account, "task_id": task_id, "transaction_id": transaction_id}
        metadata = {"service_type": service_type, "title": title, "remark": remark, "fund_source": "LOCKED" if use_locked else "AVAILABLE"}
        connection.execute("INSERT INTO scheduled_tasks (task_id,user_id,task_type,description,amount,target_date,status,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, user_id, "LIFESTYLE_ORDER", title, amount, now_text()[:10], "ACTIVE", json_text(metadata), now_text()))
        connection.execute("INSERT INTO transactions (transaction_id,user_id,account_id,occurred_at,direction,category,merchant,amount,balance_after,channel,is_abnormal,abnormal_reason,status,remark) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (transaction_id,user_id,account["account_id"],now_text(),"OUT","生活服务",title,amount,
             round(float(account["balance"])-amount,2),"生活服务订单",0,"","POSTED",remark))
        token = make_rollback_token("create_lifestyle_order", before)
    return make_result("create_lifestyle_order", "跨场景联动", f"已创建“{title}”生活服务订单，金额 {amount:.2f} 元。",
        risk_level="YELLOW", risk_reason="创建生活服务订单会产生资金支出，需要确认。",
        data={"order_id": task_id, "transaction_id": transaction_id, "service_type": service_type,
              "title": title, "amount": amount, "fund_source": "LOCKED" if use_locked else "AVAILABLE",
              "available_before": available_before, "locked_before": locked_before},
        ground_truth_numbers=[amount, available_before, locked_before], rollback_token=token)


def compensate_create_lifestyle_order(rollback_token: dict, db_path=None) -> dict:
    snap = (rollback_token or {}).get("before_snapshot", {})
    account, task_id, transaction_id = snap.get("account"), snap.get("task_id"), snap.get("transaction_id")
    if not account or not task_id or not transaction_id:
        return make_result("compensate_create_lifestyle_order", "跨场景联动", "回滚凭据缺少订单或账户快照。", status="error")
    with db_session(db_path) as connection:
        connection.execute("UPDATE accounts SET balance=?,locked_balance=? WHERE account_id=?",
                           (account["balance"], account["locked_balance"], account["account_id"]))
        connection.execute("UPDATE scheduled_tasks SET status='ROLLED_BACK' WHERE task_id=?", (task_id,))
        connection.execute("UPDATE transactions SET status='REVERSED',remark=remark || ' [Saga回滚]' WHERE transaction_id=?", (transaction_id,))
    return make_result("compensate_create_lifestyle_order", "跨场景联动", "生活服务订单已取消，资金已按操作前快照恢复。",
                       ground_truth_numbers=[account["balance"], account["locked_balance"]])
