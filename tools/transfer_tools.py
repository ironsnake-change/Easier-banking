"""Transfer, contact resolution, scheduled transfer, and AA-split tools."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from core.config import DAILY_SMALL_TRANSFER_LIMIT
from ._common import (
    DEFAULT_USER_ID, SCAM_KEYWORDS, account_available, db_session, get_account,
    get_user, json_text, make_result, make_rollback_token, now_text, resolve_payee_rows,
)


def resolve_contact(query: str, user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "resolve_contact"
    try:
        query = (query or "").strip()
        if not query:
            return make_result(tool, "智能转账", "请告诉我收款人姓名、称呼、手机号或卡号尾号。", status="clarification_needed", data={"matches": []})
        with db_session(db_path) as connection:
            matches = resolve_payee_rows(connection, query, user_id)
            if not matches:
                return make_result(tool, "智能转账", f"没有找到“{query}”对应的联系人，请核对信息。", status="clarification_needed", data={"matches": [], "is_high_risk_payee": False})
            if len(matches) > 1:
                public = []
                for row in matches:
                    remarks = [dict(r) for r in connection.execute(
                        "SELECT remark FROM transactions WHERE user_id=? AND merchant=? AND remark!='' ORDER BY occurred_at DESC LIMIT 5",
                        (user_id, row["name"]),
                    ).fetchall()]
                    public.append({
                        "contact_id": row["contact_id"], "name": row["name"],
                        "bank_name": row["bank_name"], "bank_card": row["bank_card"],
                        "relation": row["relation"] or row["alias"], "phone": row["phone"],
                        "history_remarks": [r["remark"] for r in remarks], "is_risky": row["is_risky"],
                    })
                return make_result(tool, "智能转账", f"找到 {len(matches)} 位同名联系人，请确认是哪一位。", status="clarification_needed", data={"matches": public, "count": len(matches)}, ground_truth_numbers=[len(matches)])
        match = matches[0]
        is_high_risk = bool(match["is_risky"])
        level = "RED" if is_high_risk else "GREEN"
        return make_result(
            tool, "智能转账",
            f"已匹配联系人 {match['name']}（尾号 {match['bank_card'][-4:]}）。" if not is_high_risk else f"该收款账户 {match['name']} 命中高危风险标记，请停止转账。",
            status="blocked" if is_high_risk else "success",
            risk_level=level,
            risk_reason="该账户带有涉诈/高危陌生人标记，触发反诈预警。" if is_high_risk else "收款人唯一匹配且未命中风险名单。",
            data={"contact": match, "is_high_risk_payee": is_high_risk},
        )
    except Exception as exc:
        return make_result(tool, "智能转账", f"联系人解析失败：{exc}", status="error", risk_level="RED", risk_reason="联系人查询异常。")


def _transfer_risk_blocked(connection, payee: str, remark: str, user_id: str) -> tuple[bool, str, dict | None, int]:
    """Return (blocked, block_reason, contact_row_or_None, stranger_flag)."""
    if remark and any(keyword in remark for keyword in SCAM_KEYWORDS):
        return True, "备注包含疑似受骗特征词，已触发反诈劝阻。", None, 0
    rows = resolve_payee_rows(connection, payee, user_id)
    if len(rows) > 1:
        return False, "", None, 0
    if not rows:
        return False, "", None, 1
    contact = rows[0]
    if contact["is_risky"]:
        return True, "收款人为电信诈骗高危黑名单账户，转账已拦截。", contact, 0
    return False, "", contact, 0


def execute_transfer(payee_id_or_account: str, amount: float, remark: str = "",
                     user_id: str = DEFAULT_USER_ID, db_path=None, mfa_verified: bool = False) -> dict:
    tool = "execute_transfer"
    try:
        value = round(float(amount), 2)
        if value <= 0:
            return make_result(tool, "智能转账", "转账金额必须大于零。", status="error", risk_level="RED", risk_reason="金额无效。")
        with db_session(db_path) as connection:
            user = get_user(connection, user_id)
            account = get_account(connection, user_id)
            if not user or not account:
                return make_result(tool, "智能转账", "未找到用户或账户。", status="error", risk_level="RED", risk_reason="账户数据缺失。")
            if user["session_locked"]:
                return make_result(tool, "智能转账", "会话已安全锁定，请先解除锁定。", status="blocked", risk_level="RED", risk_reason="会话熔断状态。")
            blocked, block_reason, contact, stranger = _transfer_risk_blocked(connection, payee_id_or_account, remark, user_id)
            if blocked:
                return make_result(tool, "智能转账", block_reason, status="blocked", risk_level="RED", risk_reason=block_reason, data={"payee": payee_id_or_account})
            if len(resolve_payee_rows(connection, payee_id_or_account, user_id)) > 1:
                return make_result(tool, "智能转账", "收款人存在重名，请先解析并确认具体联系人。", status="clarification_needed", data={})
            if value > account_available(account):
                return make_result(tool, "智能转账", "可用余额不足。", status="blocked", risk_level="RED", risk_reason="余额不足。", data={"available_balance": account_available(account)}, ground_truth_numbers=[value, account_available(account)])
            daily_after = round(float(user["daily_transfer_used"]) + value, 2)
            if daily_after > DAILY_SMALL_TRANSFER_LIMIT and not mfa_verified:
                return make_result(tool, "智能转账", "今日累计转账将超过 1000 元红线，需要红级强验证后才能办理。", status="blocked", risk_level="RED", risk_reason=f"日累计将达到 {daily_after:.2f} 元，需短信+人脸多因子验证。", data={"daily_transfer_used": user["daily_transfer_used"], "projected": daily_after}, ground_truth_numbers=[value, daily_after, DAILY_SMALL_TRANSFER_LIMIT])
            if stranger:
                return make_result(tool, "智能转账", "收款人不是常用联系人，首次转账需红级强验证。", status="blocked", risk_level="RED", risk_reason="首次转账陌生账户需多因子验证。")
            before = {"account": account, "user": user, "transaction_id": None}
            balance_after = round(float(account["balance"]) - value, 2)
            transfer_id = "TRF_" + uuid.uuid4().hex[:12].upper()
            payee_name = contact["name"] if contact else payee_id_or_account
            connection.execute("UPDATE accounts SET balance=? WHERE account_id=?", (balance_after, account["account_id"]))
            connection.execute("UPDATE users SET daily_transfer_used=? WHERE user_id=?", (daily_after, user_id))
            connection.execute(
                "INSERT INTO transactions (transaction_id,user_id,account_id,occurred_at,direction,category,merchant,amount,balance_after,channel,is_abnormal,abnormal_reason,status,remark) VALUES (?,?,?,?,?,?,?,?,?,?,0,'','POSTED',?)",
                (transfer_id, user_id, account["account_id"], now_text(), "OUT", "转账", payee_name, value, balance_after, "AI智能转账", remark),
            )
            before["transaction_id"] = transfer_id
        return make_result(tool, "智能转账", f"已向 {payee_name} 转账 {value:.2f} 元，账户余额 {balance_after:.2f} 元。", risk_level="YELLOW", risk_reason="今日累计未超红线且为白名单联系人，需黄色确认后办理。", elder_voice_text=f"转账办好了，给{payee_name}转了{value:.2f}元，账户还剩{balance_after:.2f}元。", data={"transfer_id": transfer_id, "recipient": payee_name, "amount": value, "balance_after": balance_after, "daily_transfer_used": daily_after, "remark": remark}, ground_truth_numbers=[value, balance_after, daily_after], rollback_token=make_rollback_token(tool, before))
    except (TypeError, ValueError) as exc:
        return make_result(tool, "智能转账", f"转账参数无效：{exc}", status="error", risk_level="RED", risk_reason="金额参数无效。")
    except sqlite3.Error as exc:
        return make_result(tool, "智能转账", f"转账失败：{exc}", status="error", risk_level="RED", risk_reason="数据库交易失败。")


def compensate_execute_transfer(rollback_token: dict, db_path=None) -> dict:
    tool = "execute_transfer"
    before = rollback_token.get("before_snapshot", {})
    try:
        with db_session(db_path) as connection:
            if before.get("account"):
                account = before["account"]
                connection.execute("UPDATE accounts SET balance=? WHERE account_id=?", (account["balance"], account["account_id"]))
            if before.get("user"):
                user = before["user"]
                connection.execute("UPDATE users SET daily_transfer_used=? WHERE user_id=?", (user["daily_transfer_used"], user["user_id"]))
            transfer_id = before.get("transaction_id")
            if transfer_id:
                connection.execute("UPDATE transactions SET status='ROLLED_BACK' WHERE transaction_id=?", (transfer_id,))
        return make_result(tool, "Saga补偿", "已恢复余额与今日累计转账额，并将该笔流水标记为已冲正。", risk_level="YELLOW", risk_reason="按 before_snapshot 精确逆向恢复。", data={"transaction_id": before.get("transaction_id"), "status": "ROLLED_BACK"})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"转账回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")


def schedule_transfer(payee_id_or_account: str, amount: float, schedule_cron_or_date: str,
                      remark: str = "", user_id: str = DEFAULT_USER_ID, db_path=None, mfa_verified: bool = False) -> dict:
    tool = "schedule_transfer"
    try:
        value = round(float(amount), 2)
        if value <= 0:
            return make_result(tool, "智能转账", "预约转账金额必须大于零。", status="error", risk_level="RED", risk_reason="金额无效。")
        with db_session(db_path) as connection:
            user = get_user(connection, user_id)
            blocked, block_reason, contact, stranger = _transfer_risk_blocked(connection, payee_id_or_account, remark, user_id)
            if blocked:
                return make_result(tool, "智能转账", block_reason, status="blocked", risk_level="RED", risk_reason=block_reason)
            if len(resolve_payee_rows(connection, payee_id_or_account, user_id)) > 1:
                return make_result(tool, "智能转账", "收款人存在重名，请先解析并确认。", status="clarification_needed", data={})
            daily_after = round(float(user["daily_transfer_used"]) + value, 2) if user else value
            if (daily_after > DAILY_SMALL_TRANSFER_LIMIT and not mfa_verified) or stranger:
                return make_result(tool, "智能转账", "预约转账命中红级条件（超累计红线或陌生收款人），需强验证。", status="blocked", risk_level="RED", risk_reason="预约单笔及累计超1000元或陌生收款人需红级验证。", ground_truth_numbers=[value, daily_after])
            payee_name = contact["name"] if contact else payee_id_or_account
            task_id = "TASK_" + uuid.uuid4().hex[:12].upper()
            metadata = {"payee": payee_id_or_account, "payee_name": payee_name, "schedule": schedule_cron_or_date, "remark": remark}
            connection.execute(
                "INSERT INTO scheduled_tasks (task_id,user_id,task_type,description,amount,target_date,status,metadata_json,created_at) VALUES (?,?,?,?,?,?, 'ACTIVE',?,?)",
                (task_id, user_id, "SCHEDULED_TRANSFER", f"预约转账给{payee_name}", value, schedule_cron_or_date, json_text(metadata), now_text()),
            )
        return make_result(tool, "智能转账", f"已创建预约转账：给 {payee_name} {value:.2f} 元，计划日期 {schedule_cron_or_date}。", risk_level="YELLOW", risk_reason="预约转账金额在黄色区间，需确认后生效。", data={"task_id": task_id, "recipient": payee_name, "amount": value, "schedule": schedule_cron_or_date}, ground_truth_numbers=[value], rollback_token=make_rollback_token(tool, {"task_id": task_id}))
    except (TypeError, ValueError) as exc:
        return make_result(tool, "智能转账", f"预约参数无效：{exc}", status="error", risk_level="RED", risk_reason="参数无效。")
    except sqlite3.Error as exc:
        return make_result(tool, "智能转账", f"预约创建失败：{exc}", status="error", risk_level="RED", risk_reason="数据库写入失败。")


def compensate_schedule_transfer(rollback_token: dict, db_path=None) -> dict:
    tool = "schedule_transfer"
    task_id = rollback_token.get("before_snapshot", {}).get("task_id")
    try:
        with db_session(db_path) as connection:
            connection.execute("UPDATE scheduled_tasks SET status='CANCELLED' WHERE task_id=?", (task_id,))
        return make_result(tool, "Saga补偿", f"预约转账 {task_id} 已置为 CANCELLED。", risk_level="YELLOW", risk_reason="撤销新建预约计划。", data={"task_id": task_id, "status": "CANCELLED"})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"预约回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")


def split_aa_bill(total_amount: float, participants: list, title: str = "AA聚餐分账",
                  user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "split_aa_bill"
    try:
        total = round(float(total_amount), 2)
        if total <= 0:
            return make_result(tool, "智能转账", "AA 分账总额必须大于零。", status="error", risk_level="RED", risk_reason="金额无效。")
        if not isinstance(participants, list) or not participants:
            return make_result(tool, "智能转账", "请提供参与分账的人员列表。", status="error", risk_level="RED", risk_reason="参与人列表为空。")
        normalized = []
        for participant in participants:
            if isinstance(participant, str):
                normalized.append({"name": participant, "account": ""})
            elif isinstance(participant, dict):
                normalized.append({"name": str(participant.get("name", "未命名")), "account": str(participant.get("account", ""))})
            elif isinstance(participant, (list, tuple)) and len(participant) >= 1:
                normalized.append({"name": str(participant[0]), "account": str(participant[1]) if len(participant) > 1 else ""})
            else:
                return make_result(tool, "智能转账", "参与人格式无效。", status="error", risk_level="RED", risk_reason="参与人必须是姓名、字典或二元组。")
        count = len(normalized)
        base = round(total / count, 2)
        shares = [base] * count
        shares[-1] = round(total - base * (count - 1), 2)
        batch_id = "AA_" + uuid.uuid4().hex[:12].upper()
        created_at = now_text()
        with db_session(db_path) as connection:
            connection.execute(
                "INSERT INTO aa_batches (batch_id,user_id,title,total_amount,participant_count,per_share,status,created_at) VALUES (?,?,?,?,?,?, 'ACTIVE',?)",
                (batch_id, user_id, title, total, count, base, created_at),
            )
            for index, (person, share) in enumerate(zip(normalized, shares), start=1):
                detail_id = f"{batch_id}_{index:03d}"
                connection.execute(
                    "INSERT INTO aa_details (detail_id,batch_id,participant_name,participant_account,share_amount,status,created_at) VALUES (?,?,?,?,?, 'PENDING',?)",
                    (detail_id, batch_id, person["name"], person["account"], share, created_at),
                )
        return make_result(tool, "智能转账", f"AA 分账“{title}”已创建，共 {count} 人，每人 {base:.2f} 元，尾差已归入最后一人。", risk_level="YELLOW", risk_reason="AA 分账涉及多人收款，需黄色确认。", data={"batch_id": batch_id, "title": title, "total_amount": total, "participant_count": count, "per_share": base, "details": [{"name": p["name"], "share_amount": s} for p, s in zip(normalized, shares)]}, ground_truth_numbers=[total, base, *shares], rollback_token=make_rollback_token(tool, {"batch_id": batch_id}))
    except (TypeError, ValueError) as exc:
        return make_result(tool, "智能转账", f"AA 分账参数无效：{exc}", status="error", risk_level="RED", risk_reason="参数无效。")
    except sqlite3.Error as exc:
        return make_result(tool, "智能转账", f"AA 分账写入失败：{exc}", status="error", risk_level="RED", risk_reason="数据库写入失败。")


def compensate_split_aa_bill(rollback_token: dict, db_path=None) -> dict:
    tool = "split_aa_bill"
    batch_id = rollback_token.get("before_snapshot", {}).get("batch_id")
    try:
        with db_session(db_path) as connection:
            connection.execute("DELETE FROM aa_details WHERE batch_id=?", (batch_id,))
            connection.execute("DELETE FROM aa_batches WHERE batch_id=?", (batch_id,))
        return make_result(tool, "Saga补偿", f"AA 分账批次 {batch_id} 已撤销。", risk_level="YELLOW", risk_reason="删除批次及明细记录。", data={"batch_id": batch_id})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"AA 分账回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")
