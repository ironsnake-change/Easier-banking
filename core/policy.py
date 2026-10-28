"""Single source of truth for banking action risk, proposals, and confirmations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from core import config, database
from core.security import security_gateway


PROPOSAL_TTL_SECONDS = max(30, int(os.getenv("ACTION_PROPOSAL_TTL_SECONDS", "120")))
LIMIT_OVERRIDE_TTL_SECONDS = max(60, int(os.getenv("LIMIT_OVERRIDE_TTL_SECONDS", "900")))
OTP_MAX_ATTEMPTS = 3
OTP_PEPPER = config.OTP_PEPPER

ACTION_LABELS = {
    "cancel_subscription": "周期代扣拦截",
    "restore_subscription": "恢复周期代扣授权",
    "subscribe_service": "新增付费订阅",
    "transfer": "账户转账",
    "manage_pocket": "专项资金口袋",
    "card_control": "银行卡风控变更",
    "limit_change": "单日风控限额调整",
    "subscribe_wealth": "理财产品申购",
    "redeem_wealth": "理财产品赎回",
}

WRITE_ACTIONS = set(ACTION_LABELS)
PROTECTIVE_CARD_OPERATIONS = {"freeze", "overseas_lock", "night_lock", "recurring_lock", "atm_lock"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _after(seconds: int) -> str:
    return _iso(_utc_now() + timedelta(seconds=seconds))


def ensure_schema() -> None:
    connection = database.get_connection()
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_proposals (
                proposal_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action_json TEXT NOT NULL,
                params_hash TEXT NOT NULL,
                level TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                executed_at TEXT,
                confirm_token_hash TEXT NOT NULL DEFAULT '',
                otp_hash TEXT,
                otp_expires_at TEXT,
                otp_attempts INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_action_proposals_pending
                ON action_proposals(user_id,status,expires_at);
            CREATE TABLE IF NOT EXISTS limit_overrides (
                override_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                amount REAL NOT NULL CHECK (amount >= 0),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_limit_overrides_active
                ON limit_overrides(user_id,expires_at);
            CREATE TABLE IF NOT EXISTS audit_outbox (
                outbox_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                error_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _money(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return round(default, 2)


def _clean_service(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch not in "\"“”‘’'").strip()
    for suffix in ("连续包月", "自动续费", "自动扣费", "订阅服务", "订阅", "会员", "服务"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text[:80]


def normalize_action(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("action") or "").strip()
    if name not in WRITE_ACTIONS:
        return None
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    clean: dict[str, Any] = {"action": name, "params": {}}
    if name == "cancel_subscription":
        service = _clean_service(params.get("service_name") or params.get("service") or "")
        if not service:
            return None
        clean["params"] = {"service_name": service, "amount": _money(params.get("amount"))}
    elif name == "restore_subscription":
        sub_id = str(params.get("sub_id") or "").strip()[:64]
        if not sub_id:
            return None
        clean["params"] = {
            "sub_id": sub_id,
            "service_name": _clean_service(params.get("service_name") or ""),
        }
    elif name == "subscribe_service":
        service = _clean_service(params.get("service_name") or params.get("service") or "")
        amount = _money(params.get("amount"))
        if not service or amount <= 0:
            return None
        clean["params"] = {
            "service_name": service,
            "amount": amount,
            "cycle": str(params.get("cycle") or "每月")[:20],
        }
    elif name == "transfer":
        recipient = str(params.get("recipient") or "").strip()[:40]
        amount = _money(params.get("amount"))
        if not recipient or amount <= 0:
            return None
        clean["params"] = {
            "recipient": recipient,
            "amount": amount,
            "remark": str(params.get("remark") or "用户发起转账")[:80],
        }
    elif name == "manage_pocket":
        operation = str(params.get("operation") or "deposit").lower()
        if operation not in {"create", "deposit", "withdraw"}:
            operation = "deposit"
        clean["params"] = {
            "operation": operation,
            "name": str(params.get("name") or "专项资金").strip()[:30],
            "amount": _money(params.get("amount")),
        }
    elif name == "card_control":
        operation = str(params.get("operation") or "").lower()
        if operation not in {"freeze", "unfreeze", "overseas_lock", "night_lock", "recurring_lock", "atm_lock", "adjust_limit"}:
            return None
        value = params.get("value")
        clean["params"] = {
            "card_tail": str(params.get("card_tail") or "")[-4:],
            "operation": operation,
            "value": _money(value) if operation == "adjust_limit" else bool(value),
        }
    elif name == "limit_change":
        value = _money(params.get("value"))
        if value <= 0:
            return None
        clean["params"] = {"value": value}
    elif name in {"subscribe_wealth", "redeem_wealth"}:
        clean["params"] = {
            "product_name": str(params.get("product_name") or "").strip()[:80],
            "amount": _money(params.get("amount")),
        }
    return clean


def _base_limit(user_id: str) -> tuple[float, float, float]:
    connection = database.get_connection()
    try:
        pref = connection.execute(
            "SELECT transfer_threshold FROM user_preferences WHERE user_id=?", (user_id,)
        ).fetchone()
        user = connection.execute(
            "SELECT daily_transfer_used FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        connection.close()
    return (
        _money(pref["transfer_threshold"] if pref else 1000, 1000),
        _money(user["daily_transfer_used"] if user else 0),
        0.0,
    )


def effective_daily_limit(user_id: str) -> float:
    ensure_schema()
    connection = database.get_connection()
    try:
        return effective_daily_limit_on_connection(connection, user_id)
    finally:
        connection.close()


def effective_daily_limit_on_connection(connection: sqlite3.Connection, user_id: str) -> float:
    pref = connection.execute(
        "SELECT transfer_threshold FROM user_preferences WHERE user_id=?", (user_id,)
    ).fetchone()
    base = _money(pref["transfer_threshold"] if pref else 1000, 1000)
    row = connection.execute(
        "SELECT COALESCE(SUM(amount),0) AS total FROM limit_overrides "
        "WHERE user_id=? AND expires_at>?",
        (user_id, _iso(_utc_now())),
    ).fetchone()
    return round(base + _money(row["total"] if row else 0), 2)


def pruned_overrides(user_id: str) -> list[dict[str, Any]]:
    ensure_schema()
    now = _iso(_utc_now())
    connection = database.get_connection()
    try:
        expired = [
            dict(row) for row in connection.execute(
                "SELECT * FROM limit_overrides WHERE user_id=? AND expires_at<=?",
                (user_id, now),
            ).fetchall()
        ]
        connection.execute("DELETE FROM limit_overrides WHERE user_id=? AND expires_at<=?", (user_id, now))
        connection.commit()
        active = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM limit_overrides WHERE user_id=? AND expires_at>? ORDER BY expires_at",
                (user_id, now),
            ).fetchall()
        ]
    finally:
        connection.close()
    for item in expired:
        _audit(user_id, "LIMIT_OVERRIDE_EXPIRED", "limit_override", "L3", {
            "override_id": item["override_id"], "amount": item["amount"],
        }, "EXPIRED")
    return active


def _card(user_id: str, tail: str = "") -> dict[str, Any] | None:
    connection = database.get_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM cards WHERE user_id=? ORDER BY card_id", (user_id,)
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return None
    if tail:
        for row in rows:
            if str(row["card_no"]).endswith(str(tail)):
                return dict(row)
        return None
    return dict(rows[0]) if len(rows) == 1 else None


def _recipient_registered(user_id: str, recipient: str) -> bool:
    connection = database.get_connection()
    try:
        rows = connection.execute(
            "SELECT contact_id,name,alias FROM contacts WHERE user_id=?", (user_id,)
        ).fetchall()
    finally:
        connection.close()
    return any(
        recipient == str(row["contact_id"]) or recipient == str(row["name"]) or recipient in str(row["alias"] or "").split(",")
        for row in rows
    )


def classify(user_id: str, action: dict[str, Any] | None) -> dict[str, Any]:
    """Return the authoritative level for a write action.

    L1 protective lock activation needs a confirmation card only.
    L2 routine financial mutation needs a confirmation card only.
    L3 risk expansion needs a confirmation card and a one-time password.
    """
    normalized = normalize_action(action)
    if not normalized:
        return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": ["未知或不完整的写操作，按默认拒绝策略处理"]}
    name = normalized["action"]
    params = normalized["params"]
    base, used, _ = _base_limit(user_id)
    effective = effective_daily_limit(user_id)
    remaining = max(0.0, effective - used)

    if name == "transfer":
        amount = _money(params.get("amount"))
        registered = _recipient_registered(user_id, params.get("recipient", ""))
        reasons: list[str] = []
        if amount > remaining:
            reasons.append(f"转账金额超过今日剩余额度 {remaining:.2f} 元")
        if not registered:
            reasons.append("收款人尚未登记，属于首次转账")
        if reasons:
            return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": reasons}
        return {"level": "L2", "requires": ["confirmation"], "reasons": ["转账金额未超过今日剩余额度且收款人已登记"]}

    if name == "card_control":
        operation = params.get("operation")
        value = params.get("value")
        card = _card(user_id, params.get("card_tail", ""))
        if operation == "unfreeze":
            return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": ["解冻银行卡会恢复支付权限"]}
        if operation in {"overseas_lock", "night_lock", "recurring_lock", "atm_lock"} and value is False:
            return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": ["关闭银行卡安全锁会扩大交易权限"]}
        if operation == "adjust_limit":
            new_limit = _money(value)
            old_limit = _money(card.get("daily_limit") if card else 0)
            if new_limit > old_limit:
                return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": [f"银行卡日限额将从 {old_limit:.2f} 元调高至 {new_limit:.2f} 元"]}
            return {"level": "L2", "requires": ["confirmation"], "reasons": ["银行卡限额调整不会增加现有风险边界"]}
        if operation == "freeze" or (operation in PROTECTIVE_CARD_OPERATIONS and value is not False):
            return {"level": "L1", "requires": ["confirmation"], "reasons": ["保护性安全操作，一键确认后即时生效"]}
        return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": ["银行卡权限变更风险未明确"]}

    if name == "limit_change":
        value = _money(params.get("value"))
        if value > base:
            return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": [f"单日风控限额将从 {base:.2f} 元调高至 {value:.2f} 元"]}
        return {"level": "L2", "requires": ["confirmation"], "reasons": ["单日风控限额调整不会突破当前风险边界"]}

    if name in {"cancel_subscription", "restore_subscription", "subscribe_service", "manage_pocket", "subscribe_wealth", "redeem_wealth"}:
        labels = {
            "cancel_subscription": "取消订阅会改变第三方扣款授权",
            "restore_subscription": "恢复订阅会重新建立第三方扣款授权",
            "subscribe_service": "新增订阅会建立持续扣款授权",
            "manage_pocket": "专项资金划拨会改变可用余额与锁定资金",
            "subscribe_wealth": "理财申购涉及风险适当性与资金划转",
            "redeem_wealth": "理财赎回会改变持仓与活期余额",
        }
        return {"level": "L2", "requires": ["confirmation"], "reasons": [labels[name]]}

    return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": ["动作未命中放宽规则，采用最保守分级"]}


def _params_hash(action: dict[str, Any]) -> str:
    canonical = json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(("confirm|" + str(token)).encode("utf-8")).hexdigest()


def _otp_hash(proposal_id: str, otp: str) -> str:
    return hmac.new(OTP_PEPPER.encode("utf-8"), f"{proposal_id}|{otp}".encode("utf-8"), hashlib.sha256).hexdigest()


def _audit(user_id: str, event_type: str, action: str, level: str, payload: dict[str, Any], status: str = "SUCCESS") -> dict[str, Any]:
    return audit_event(user_id, event_type, action, level, payload, status)


def audit_event(user_id: str, event_type: str, action: str, level: str,
                payload: dict[str, Any], status: str = "SUCCESS") -> dict[str, Any]:
    """Append to the hash chain; persist an outbox record if auditing fails."""
    try:
        return security_gateway.log_event(user_id, event_type, action, level, payload, status)
    except Exception as exc:
        ensure_schema()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        outbox_id = "AUDIT-OUTBOX-" + uuid.uuid4().hex[:14].upper()
        connection = database.get_connection()
        try:
            connection.execute(
                "INSERT INTO audit_outbox "
                "(outbox_id,user_id,event_type,tool_name,risk_level,payload_json,error_text,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (outbox_id, user_id, event_type, action, level, encoded, str(exc)[:300], _iso(_utc_now())),
            )
            connection.commit()
        finally:
            connection.close()
        digest = hashlib.sha256((outbox_id + encoded).encode("utf-8")).hexdigest()
        return {"prev_hash": "", "curr_hash": digest, "status": status,
                "audit_outbox_id": outbox_id, "audit_deferred": True}


def _expire_stale(user_id: str | None = None) -> int:
    ensure_schema()
    now = _iso(_utc_now())
    connection = database.get_connection()
    try:
        rows = connection.execute(
            "SELECT proposal_id,user_id,action_json,level,message_id FROM action_proposals "
            "WHERE status='PENDING' AND expires_at<=?" + (" AND user_id=?" if user_id else ""),
            (now, user_id) if user_id else (now,),
        ).fetchall()
    finally:
        connection.close()
    expired = 0
    for row in rows:
        connection = database.get_connection()
        try:
            cursor = connection.execute(
                "UPDATE action_proposals SET status='EXPIRED',last_error='expired' "
                "WHERE proposal_id=? AND status='PENDING' AND expires_at<=?",
                (row["proposal_id"], now),
            )
            connection.commit()
        finally:
            connection.close()
        if cursor.rowcount == 1:
            expired += 1
            _audit(
                row["user_id"], "ACTION_PROPOSAL_EXPIRED", row["action_json"], row["level"],
                {"proposal_id": row["proposal_id"]}, "EXPIRED",
            )
            _update_message_receipt(row, "EXPIRED", error="proposal_expired")
    return expired


def expire_stale_proposals(user_id: str | None = None) -> int:
    return _expire_stale(user_id)


def _card_payload(user_id: str, proposal_id: str, action: dict[str, Any], info: dict[str, Any],
                  expires_at: str, override_to: float | None = None) -> dict[str, Any]:
    params = action.get("params", {})
    amount = _money(params.get("amount"))
    if action.get("action") == "card_control" and params.get("operation") == "adjust_limit":
        amount = _money(params.get("value"))
    if action.get("action") == "limit_change":
        amount = _money(params.get("value"))
    card = _card(user_id, params.get("card_tail", ""))
    base, used, _ = _base_limit(user_id)
    effective = effective_daily_limit(user_id)
    remaining = max(0.0, effective - used)
    target = params.get("recipient") or params.get("service_name") or params.get("name") or params.get("card_tail") or "当前账户"
    if action.get("action") == "restore_subscription":
        connection = database.get_connection()
        try:
            row = connection.execute(
                "SELECT merchant FROM subscriptions WHERE sub_id=? AND user_id=?",
                (params.get("sub_id", ""), user_id),
            ).fetchone()
            target = str(row["merchant"]) if row else str(params.get("service_name") or "已取消订阅")
        finally:
            connection.close()
    return {
        "proposal_id": proposal_id,
        "level": info["level"],
        "requires": info["requires"],
        "reasons": info["reasons"],
        "status": "PENDING",
        "action": action,
        "business_type": ACTION_LABELS.get(action["action"], "账户操作"),
        "target": target,
        "amount": amount,
        "card_tail": params.get("card_tail") or (str(card["card_no"])[-4:] if card else ""),
        "remaining_limit": round(remaining, 2),
        "daily_limit": round(effective, 2),
        "base_limit": round(base, 2),
        "otp_required": info["level"] == "L3",
        "expires_at": expires_at,
        "override_to": override_to,
    }


def create_proposal(user_id: str, action: dict[str, Any], *, message_id: int | None = None) -> dict[str, Any]:
    normalized = normalize_action(action)
    if not normalized:
        raise ValueError("业务动作不在许可白名单中")
    _expire_stale(user_id)
    info = classify(user_id, normalized)
    proposal_id = "PROP-" + uuid.uuid4().hex[:16].upper()
    confirm_token = secrets.token_urlsafe(32)
    action_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    params_hash = _params_hash(normalized)
    expires_at = _after(PROPOSAL_TTL_SECONDS)
    otp = None
    otp_hash = None
    demo_hint = None
    override_to = None
    if info["level"] == "L3":
        otp = f"{secrets.randbelow(1_000_000):06d}"
        otp_hash = _otp_hash(proposal_id, otp)
        if config.demo_mode():
            demo_hint = {"otp": otp, "expires_at": expires_at, "notice": "仅 DEMO_MODE=1 时返回，生产环境必须接入短信或验证器"}
    if normalized["action"] == "transfer":
        base, used, _ = _base_limit(user_id)
        effective = effective_daily_limit(user_id)
        if _money(normalized["params"].get("amount")) > max(0.0, effective - used):
            override_to = max(5000.0, round(_money(normalized["params"].get("amount")) + used, 2))
    connection = database.get_connection()
    try:
        connection.execute(
            "INSERT INTO action_proposals "
            "(proposal_id,user_id,action_json,params_hash,level,status,expires_at,created_at,"
            "confirm_token_hash,otp_hash,otp_expires_at,otp_attempts,message_id,last_error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, user_id, action_json, params_hash, info["level"], "PENDING", expires_at,
             _iso(_utc_now()), _token_hash(confirm_token), otp_hash, expires_at, 0, message_id, ""),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    finally:
        connection.close()
    _audit(user_id, "ACTION_PROPOSAL_CREATED", normalized["action"], info["level"], {
        "proposal_id": proposal_id, "requires": info["requires"], "reason": info["reasons"],
        "message_id": message_id,
    }, "PENDING")
    card = _card_payload(user_id, proposal_id, normalized, info, expires_at, override_to)
    return {
        "proposal_id": proposal_id,
        "confirm_token": confirm_token,
        "level": info["level"],
        "requires": info["requires"],
        "reasons": info["reasons"],
        "expires_at": expires_at,
        "card": card,
        "demo_hint": demo_hint,
        "requires_otp": info["level"] == "L3",
        "override_to": override_to,
    }


def attach_message(user_id: str, proposal_id: str, message_id: int) -> None:
    connection = database.get_connection()
    try:
        connection.execute(
            "UPDATE action_proposals SET message_id=? WHERE proposal_id=? AND user_id=?",
            (message_id, proposal_id, user_id),
        )
        connection.commit()
    finally:
        connection.close()


def _proposal_row(proposal_id: str) -> dict[str, Any] | None:
    ensure_schema()
    connection = database.get_connection()
    try:
        row = connection.execute(
            "SELECT * FROM action_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def _record_otp_failure(row: dict[str, Any], missing: bool = False) -> int:
    connection = database.get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT otp_attempts,status FROM action_proposals WHERE proposal_id=?",
            (row["proposal_id"],),
        ).fetchone()
        attempts = int(current["otp_attempts"] if current else 0) + 1
        status = "CANCELLED" if attempts >= OTP_MAX_ATTEMPTS else "PENDING"
        connection.execute(
            "UPDATE action_proposals SET otp_attempts=?,status=?,last_error=? WHERE proposal_id=? AND status='PENDING'",
            (attempts, status, "otp_missing" if missing else "otp_invalid", row["proposal_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    _audit(row["user_id"], "ACTION_PROPOSAL_OTP_REJECTED", row["action_json"], row["level"], {
        "proposal_id": row["proposal_id"], "attempts": attempts, "missing": missing,
        "invalidated": attempts >= OTP_MAX_ATTEMPTS,
    }, "BLOCKED")
    return attempts


def _create_limit_override(user_id: str, action: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    params = action.get("params", {})
    amount = _money(params.get("amount"))
    base, used, _ = _base_limit(user_id)
    current_effective = effective_daily_limit(user_id)
    target = max(5000.0, round(amount + used, 2))
    supplement = max(0.0, round(target - current_effective, 2))
    if supplement <= 0:
        return {"created": False, "target": current_effective, "amount": 0.0}
    override_id = "OVR-" + uuid.uuid4().hex[:14].upper()
    expires_at = _after(LIMIT_OVERRIDE_TTL_SECONDS)
    connection = database.get_connection()
    try:
        connection.execute(
            "INSERT INTO limit_overrides (override_id,user_id,amount,expires_at,created_at) VALUES (?,?,?,?,?)",
            (override_id, user_id, supplement, expires_at, _iso(_utc_now())),
        )
        connection.commit()
    finally:
        connection.close()
    _audit(user_id, "LIMIT_OVERRIDE_CREATED", action.get("action", ""), "L3", {
        "proposal_id": proposal_id, "override_id": override_id, "amount": supplement,
        "expires_at": expires_at, "base_limit": base, "target_limit": target,
    }, "SUCCESS")
    return {"created": True, "override_id": override_id, "amount": supplement,
            "expires_at": expires_at, "target": target}


def _update_message_receipt(row: dict[str, Any], status: str, *, receipt_data: dict[str, Any] | None = None,
                            summary: str = "", error: str = "") -> None:
    if not isinstance(row, dict):
        row = dict(row)
    message_id = row.get("message_id")
    if not message_id:
        return
    connection = database.get_connection()
    try:
        message = connection.execute(
            "SELECT receipt_json FROM chat_messages WHERE message_id=? AND user_id=?",
            (message_id, row["user_id"]),
        ).fetchone()
        if not message:
            return
        try:
            receipt = json.loads(message["receipt_json"] or "{}")
        except json.JSONDecodeError:
            receipt = {}
        card = receipt.get("authorization") if isinstance(receipt.get("authorization"), dict) else {}
        card.update({"status": status, "proposal_id": row["proposal_id"], "updated_at": _iso(_utc_now())})
        if status == "AUTHORIZED":
            card["executed_at"] = _iso(_utc_now())
        if error:
            card["error"] = error[:240]
        receipt["authorization"] = card
        if summary:
            receipt["summary"] = summary
        if receipt_data:
            receipt["receiptData"] = receipt_data
            receipt["status"] = "COMPLETED"
        connection.execute(
            "UPDATE chat_messages SET receipt_json=? WHERE message_id=? AND user_id=?",
            (json.dumps(receipt, ensure_ascii=False), message_id, row["user_id"]),
        )
        connection.commit()
    finally:
        connection.close()


def confirm_proposal(user_id: str, proposal_id: str, confirm_token: str, *,
                     otp: str | None = None, cancel: bool = False) -> dict[str, Any]:
    _expire_stale(user_id)
    row = _proposal_row(proposal_id)
    if not row:
        return {"status": "NOT_FOUND", "error": "proposal_not_found"}
    if row["user_id"] != user_id:
        _audit(user_id, "ACTION_PROPOSAL_OWNER_MISMATCH", row["action_json"], row["level"],
               {"proposal_id": proposal_id}, "BLOCKED")
        return {"status": "FORBIDDEN", "error": "proposal_owner_mismatch"}
    if not hmac.compare_digest(str(row["confirm_token_hash"]), _token_hash(confirm_token)):
        _audit(user_id, "ACTION_PROPOSAL_TOKEN_MISMATCH", row["action_json"], row["level"],
               {"proposal_id": proposal_id}, "BLOCKED")
        return {"status": "FORBIDDEN", "error": "confirm_token_invalid"}
    try:
        action = json.loads(row["action_json"])
    except json.JSONDecodeError:
        return {"status": "CONFLICT", "error": "proposal_action_corrupted"}
    if not hmac.compare_digest(str(row["params_hash"]), _params_hash(action)):
        _audit(user_id, "ACTION_PROPOSAL_PARAMS_TAMPERED", action.get("action", ""), row["level"],
               {"proposal_id": proposal_id}, "BLOCKED")
        return {"status": "CONFLICT", "error": "proposal_params_tampered"}
    if row["status"] != "PENDING":
        return {"status": "CONFLICT", "error": f"proposal_{str(row['status']).lower()}"}
    now = _iso(_utc_now())
    if row["expires_at"] <= now:
        _expire_stale(user_id)
        return {"status": "CONFLICT", "error": "proposal_expired"}
    if cancel:
        connection = database.get_connection()
        try:
            cursor = connection.execute(
                "UPDATE action_proposals SET status='CANCELLED',last_error='cancelled_by_user' "
                "WHERE proposal_id=? AND user_id=? AND status='PENDING' AND expires_at>? "
                "AND confirm_token_hash=? AND params_hash=?",
                (proposal_id, user_id, now, row["confirm_token_hash"], row["params_hash"]),
            )
            connection.commit()
        finally:
            connection.close()
        if cursor.rowcount != 1:
            return {"status": "CONFLICT", "error": "proposal_not_cancellable"}
        _audit(user_id, "ACTION_PROPOSAL_CANCELLED", action.get("action", ""), row["level"],
               {"proposal_id": proposal_id}, "CANCELLED")
        _update_message_receipt(row, "CANCELLED", summary="用户取消，未执行账务变更")
        return {"status": "CANCELLED", "proposal_id": proposal_id, "level": row["level"], "action": action}
    if row["level"] == "L3":
        if not otp:
            attempts = _record_otp_failure(row, missing=True)
            return {"status": "OTP_REQUIRED", "error": "otp_required", "attempts": attempts,
                    "remaining_attempts": max(0, OTP_MAX_ATTEMPTS - attempts)}
        if not row.get("otp_expires_at") or row["otp_expires_at"] <= now:
            _expire_stale(user_id)
            return {"status": "CONFLICT", "error": "otp_expired"}
        if not hmac.compare_digest(str(row["otp_hash"] or ""), _otp_hash(proposal_id, str(otp))):
            attempts = _record_otp_failure(row)
            return {"status": "OTP_INVALID", "error": "otp_invalid", "attempts": attempts,
                    "remaining_attempts": max(0, OTP_MAX_ATTEMPTS - attempts)}
    connection = database.get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE action_proposals SET status='CONFIRMED',confirmed_at=?,last_error='' "
            "WHERE proposal_id=? AND user_id=? AND status='PENDING' AND expires_at>? "
            "AND confirm_token_hash=? AND params_hash=?",
            (_iso(_utc_now()), proposal_id, user_id, now, row["confirm_token_hash"], row["params_hash"]),
        )
        connection.commit()
    finally:
        connection.close()
    if cursor.rowcount != 1:
        return {"status": "CONFLICT", "error": "proposal_already_claimed"}
    _audit(user_id, "ACTION_PROPOSAL_CONFIRMED", action.get("action", ""), row["level"],
           {"proposal_id": proposal_id}, "CONFIRMED")
    override = None
    if row["level"] == "L3" and action.get("action") == "transfer":
        try:
            override = _create_limit_override(user_id, action, proposal_id)
        except sqlite3.Error as exc:
            _audit(user_id, "ACTION_COMPENSATION_REQUIRED", action.get("action", ""), row["level"],
                   {"proposal_id": proposal_id, "phase": "limit_override", "error": str(exc)[:200]}, "ERROR")
            connection = database.get_connection()
            try:
                connection.execute(
                    "UPDATE action_proposals SET status='CANCELLED',last_error=? WHERE proposal_id=?",
                    (f"limit_override_failed:{exc}", proposal_id),
                )
                connection.commit()
            finally:
                connection.close()
            return {"status": "ERROR", "error": "limit_override_failed"}
    from core.ai_banking import execute_action
    try:
        executed = execute_action(user_id, action, level=row["level"])
    except Exception as exc:
        if override and override.get("created"):
            connection = database.get_connection()
            try:
                connection.execute("DELETE FROM limit_overrides WHERE override_id=? AND user_id=?",
                                   (override.get("override_id"), user_id))
                connection.commit()
            finally:
                connection.close()
            _audit(user_id, "LIMIT_OVERRIDE_COMPENSATED", action.get("action", ""), row["level"], {
                "proposal_id": proposal_id, "override_id": override.get("override_id"),
            }, "ROLLED_BACK")
        connection = database.get_connection()
        try:
            connection.execute(
                "UPDATE action_proposals SET status='CANCELLED',last_error=? WHERE proposal_id=? AND status='CONFIRMED'",
                (str(exc)[:300], proposal_id),
            )
            connection.commit()
        finally:
            connection.close()
        _audit(user_id, "ACTION_PROPOSAL_EXECUTION_FAILED", action.get("action", ""), row["level"], {
            "proposal_id": proposal_id, "error": str(exc)[:300],
            "compensation": "业务账本由单个 SQLite 事务提交，异常时已回滚，无部分资金提交",
        }, "ERROR")
        _update_message_receipt(row, "FAILED", error=str(exc))
        return {"status": "ERROR", "error": str(exc), "proposal_id": proposal_id}
    connection = database.get_connection()
    try:
        cursor = connection.execute(
            "UPDATE action_proposals SET status='EXECUTED',executed_at=?,last_error='' "
            "WHERE proposal_id=? AND status='CONFIRMED'",
            (_iso(_utc_now()), proposal_id),
        )
        connection.commit()
    finally:
        connection.close()
    if cursor.rowcount != 1:
        _audit(user_id, "ACTION_COMPENSATION_REQUIRED", action.get("action", ""), row["level"],
               {"proposal_id": proposal_id, "phase": "finalize", "executed": True}, "ERROR")
    _audit(user_id, "ACTION_PROPOSAL_EXECUTED", action.get("action", ""), row["level"], {
        "proposal_id": proposal_id, "summary": executed.get("summary", ""),
        "receipt_id": (executed.get("receipt") or {}).get("receipt_id", ""),
        "limit_override": override,
    }, "SUCCESS")
    _update_message_receipt(row, "AUTHORIZED", receipt_data=executed.get("receipt"),
                            summary=executed.get("summary", ""))
    return {
        "status": "EXECUTED",
        "proposal_id": proposal_id,
        "level": row["level"],
        "action": action,
        "summary": executed.get("summary", ""),
        "receiptData": executed.get("receipt"),
        "limit_override": override,
    }


def classify_tool(user_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any] | None:
    args = dict(arguments or {})
    if tool_name == "execute_transfer":
        return classify(user_id, {"action": "transfer", "params": {
            "recipient": args.get("payee_id_or_account", ""),
            "amount": args.get("amount", 0),
            "remark": args.get("remark", ""),
        }})
    if tool_name == "schedule_transfer":
        return classify(user_id, {"action": "transfer", "params": {
            "recipient": args.get("payee_id_or_account", ""),
            "amount": args.get("amount", 0),
            "remark": "预约转账",
        }})
    if tool_name == "split_aa_bill":
        return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": ["多人分账涉及多个外部收款人"]}
    if tool_name == "cancel_subscription":
        return classify(user_id, {"action": "cancel_subscription", "params": {
            "service_name": args.get("subscription_id_or_name", ""),
            "amount": 0,
        }})
    if tool_name == "manage_card_limits_and_lock":
        action = args.get("action")
        operation = {"lock": "freeze", "unlock": "unfreeze", "overseas_switch": "overseas_lock", "adjust_limit": "adjust_limit"}.get(str(action), str(action))
        value = args.get("overseas_switch") if action == "overseas_switch" else args.get("new_daily_limit") if action == "adjust_limit" else True
        return classify(user_id, {"action": "card_control", "params": {
            "card_tail": str(args.get("card_id", ""))[-4:],
            "operation": operation,
            "value": value,
        }})
    if tool_name == "report_card_loss":
        return {"level": "L3", "requires": ["confirmation", "otp"], "reasons": ["银行卡挂失属于不可逆高风险操作"]}
    if tool_name == "lock_demand_funds":
        return classify(user_id, {"action": "manage_pocket", "params": {
            "operation": "deposit", "name": args.get("purpose", "专项资金"), "amount": args.get("amount", 0),
        }})
    if tool_name == "create_lifestyle_order":
        return {"level": "L2", "requires": ["confirmation"], "reasons": ["生活服务订单会占用或划转资金"]}
    if tool_name in {"subscribe_wealth_product", "redeem_wealth_product", "apply_virtual_card"}:
        return {"level": "L2", "requires": ["confirmation"], "reasons": ["理财或虚拟卡业务会改变账户风险边界"]}
    return None


__all__ = [
    "ACTION_LABELS",
    "audit_event",
    "classify",
    "classify_tool",
    "normalize_action",
    "ensure_schema",
    "create_proposal",
    "confirm_proposal",
    "attach_message",
    "effective_daily_limit",
    "effective_daily_limit_on_connection",
    "pruned_overrides",
    "expire_stale_proposals",
    "PROPOSAL_TTL_SECONDS",
    "LIMIT_OVERRIDE_TTL_SECONDS",
]
