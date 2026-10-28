"""Card status, virtual card, limit/lock management, and loss-report tools."""

from __future__ import annotations

import sqlite3
import uuid

from ._common import DEFAULT_USER_ID, days_later_text, db_session, make_result, make_rollback_token, now_text, row_dict


def query_cards_status(user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "query_cards_status"
    try:
        with db_session(db_path) as connection:
            cards = [dict(row) for row in connection.execute("SELECT * FROM cards WHERE user_id=? ORDER BY card_id", (user_id,)).fetchall()]
        return make_result(tool, "卡片管理", f"查询到 {len(cards)} 张银行卡。", data={"cards": cards, "count": len(cards)}, ground_truth_numbers=[len(cards), *[c["daily_limit"] for c in cards]])
    except Exception as exc:
        return make_result(tool, "卡片管理", f"卡片查询失败：{exc}", status="error", risk_level="RED", risk_reason="卡片读取异常。")


def apply_virtual_card(source_card_id: str, limit_amount: float = 500.0, usage_label: str = "网购专属小额卡",
                       valid_days: int = 30, user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "apply_virtual_card"
    try:
        limit = round(float(limit_amount), 2)
        valid_days = int(valid_days)
        if limit <= 0 or valid_days <= 0:
            return make_result(tool, "卡片管理", "虚拟卡限额或有效期必须为正数。", status="error", risk_level="RED", risk_reason="参数无效。")
        with db_session(db_path) as connection:
            source = row_dict(connection.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (source_card_id, user_id)).fetchone())
            if not source:
                return make_result(tool, "卡片管理", "未找到主卡。", status="error", risk_level="RED", risk_reason="源卡不存在。")
            if source["status"] != "ACTIVE":
                return make_result(tool, "卡片管理", "主卡当前不可用，无法申请虚拟卡。", status="blocked", risk_level="RED", risk_reason="源卡状态异常。")
            new_id = "CARD_V_" + uuid.uuid4().hex[:12].upper()
            virtual_no = "6259" + "".join([str((uuid.uuid4().int % 10)) for _ in range(8)]) + "****" + str(uuid.uuid4().int % 10000).zfill(4)
            connection.execute(
                "INSERT INTO cards (card_id,user_id,card_no,card_type,status,daily_limit,night_lock,overseas_lock,virtual_parent_card_id,usage_label,valid_until,credit_limit,reissue_work_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'')",
                (new_id, user_id, virtual_no, "虚拟卡", "ACTIVE", limit, 0, 1, source_card_id, usage_label, days_later_text(valid_days)),
            )
        return make_result(tool, "卡片管理", f"已申请虚拟安全副卡，限额 {limit:.2f} 元，有效期 {valid_days} 天。", risk_level="YELLOW", risk_reason="虚拟卡用于隔离高风险网购或订阅，需黄色确认。", data={"card_id": new_id, "source_card_id": source_card_id, "limit_amount": limit, "usage_label": usage_label, "valid_days": valid_days, "card_no": virtual_no}, ground_truth_numbers=[limit, valid_days], rollback_token=make_rollback_token(tool, {"_delete_card_id": new_id}))
    except (TypeError, ValueError) as exc:
        return make_result(tool, "卡片管理", f"虚拟卡参数无效：{exc}", status="error", risk_level="RED", risk_reason="参数无效。")
    except sqlite3.Error as exc:
        return make_result(tool, "卡片管理", f"虚拟卡申请失败：{exc}", status="error", risk_level="RED", risk_reason="数据库写入失败。")


def compensate_apply_virtual_card(rollback_token: dict, db_path=None) -> dict:
    tool = "apply_virtual_card"
    card_id = rollback_token.get("before_snapshot", {}).get("_delete_card_id")
    try:
        with db_session(db_path) as connection:
            connection.execute("DELETE FROM cards WHERE card_id=?", (card_id,))
        return make_result(tool, "Saga补偿", f"虚拟卡 {card_id} 已注销移除。", risk_level="YELLOW", risk_reason="删除新生成的虚拟卡记录。", data={"card_id": card_id})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"虚拟卡回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")


def manage_card_limits_and_lock(card_id: str, action: str, new_daily_limit: float = None,
                                overseas_switch: bool = None, user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "manage_card_limits_and_lock"
    if action not in {"lock", "unlock", "overseas_switch", "adjust_limit"}:
        return make_result(tool, "卡片管理", "action 仅支持 lock、unlock、overseas_switch、adjust_limit。", status="error", risk_level="RED", risk_reason="不支持的操作。")
    try:
        with db_session(db_path) as connection:
            card = row_dict(connection.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (card_id, user_id)).fetchone())
            if not card:
                return make_result(tool, "卡片管理", "没有找到这张银行卡。", status="error", risk_level="RED", risk_reason="卡片不存在。")
            before = {"card": card}
            if action == "lock":
                connection.execute("UPDATE cards SET status='FROZEN' WHERE card_id=?", (card_id,))
                summary, level = f"{card['card_no']} 已临时锁定。", "YELLOW"
            elif action == "unlock":
                connection.execute("UPDATE cards SET status='ACTIVE' WHERE card_id=?", (card_id,))
                summary, level = f"{card['card_no']} 已解锁。", "RED"
            elif action == "overseas_switch":
                switch = int(bool(overseas_switch))
                connection.execute("UPDATE cards SET overseas_lock=? WHERE card_id=?", (switch, card_id))
                summary = f"境外支付已{'关闭' if switch else '开启'}。"
                level = "YELLOW" if switch else "RED"
            else:
                limit = round(float(new_daily_limit), 2)
                if limit <= 0:
                    return make_result(tool, "卡片管理", "新限额必须大于零。", status="error", risk_level="RED", risk_reason="限额无效。")
                connection.execute("UPDATE cards SET daily_limit=? WHERE card_id=?", (limit, card_id))
                summary = f"日累计限额已调整为 {limit:.2f} 元。"
                level = "YELLOW" if limit <= float(card["daily_limit"]) else "RED"
        return make_result(tool, "卡片管理", summary, risk_level=level, risk_reason="保护性操作为黄级，开放/上调为红级。", data={"card_id": card_id, "action": action, "previous": {k: card[k] for k in ("status", "daily_limit", "overseas_lock")}}, rollback_token=make_rollback_token(tool, before))
    except (TypeError, ValueError) as exc:
        return make_result(tool, "卡片管理", f"参数无效：{exc}", status="error", risk_level="RED", risk_reason="参数无效。")
    except sqlite3.Error as exc:
        return make_result(tool, "卡片管理", f"卡片操作失败：{exc}", status="error", risk_level="RED", risk_reason="数据库更新失败。")


def compensate_manage_card_limits_and_lock(rollback_token: dict, db_path=None) -> dict:
    tool = "manage_card_limits_and_lock"
    card = rollback_token.get("before_snapshot", {}).get("card")
    try:
        with db_session(db_path) as connection:
            if card:
                connection.execute(
                    "UPDATE cards SET status=?, daily_limit=?, night_lock=?, overseas_lock=? WHERE card_id=?",
                    (card["status"], card["daily_limit"], card["night_lock"], card["overseas_lock"], card["card_id"]),
                )
        return make_result(tool, "Saga补偿", "卡片状态、限额与境外开关已恢复。", risk_level="YELLOW", risk_reason="恢复 before_snapshot。", data={"card_id": (card or {}).get("card_id")})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"卡片回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")


def report_card_loss(card_id: str, reason: str = "疑似盗刷或遗失", reissue_card: bool = True,
                     user_id: str = DEFAULT_USER_ID, db_path=None) -> dict:
    tool = "report_card_loss"
    try:
        with db_session(db_path) as connection:
            card = row_dict(connection.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (card_id, user_id)).fetchone())
            if not card:
                return make_result(tool, "卡片管理", "没有找到这张银行卡。", status="error", risk_level="RED", risk_reason="卡片不存在。")
            before = {"card": card}
            work_order = f"WO_{uuid.uuid4().hex[:12].upper()}" if reissue_card else ""
            connection.execute(
                "UPDATE cards SET status='LOST_FROZEN', night_lock=1, overseas_lock=1, reissue_work_order=? WHERE card_id=?",
                (work_order, card_id),
            )
        return make_result(tool, "卡片管理", f"已正式挂失 {card['card_no']}，全部支付通道已关闭。", risk_level="RED", risk_reason="挂失为高风险不可逆操作，需红级多因子验证。", data={"card_id": card_id, "status": "LOST_FROZEN", "reissue_card": bool(reissue_card), "work_order": work_order, "reason": reason}, rollback_token=make_rollback_token(tool, before))
    except sqlite3.Error as exc:
        return make_result(tool, "卡片管理", f"挂失失败：{exc}", status="error", risk_level="RED", risk_reason="数据库更新失败。")


def compensate_report_card_loss(rollback_token: dict, db_path=None) -> dict:
    tool = "report_card_loss"
    card = rollback_token.get("before_snapshot", {}).get("card")
    try:
        with db_session(db_path) as connection:
            if card:
                connection.execute(
                    "UPDATE cards SET status=?, night_lock=?, overseas_lock=?, reissue_work_order=? WHERE card_id=?",
                    (card["status"], card["night_lock"], card["overseas_lock"], card["reissue_work_order"], card["card_id"]),
                )
        return make_result(tool, "Saga补偿", "卡片挂失状态已恢复为挂失前快照。", risk_level="YELLOW", risk_reason="多步任务回滚时恢复挂失前状态。", data={"card_id": (card or {}).get("card_id")})
    except Exception as exc:
        return make_result(tool, "Saga补偿", f"挂失回滚失败：{exc}", status="error", risk_level="RED", risk_reason="逆向补偿异常。")
