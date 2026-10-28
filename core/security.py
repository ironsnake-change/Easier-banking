"""Five-layer security gateway, circuit breaker, and SHA-256 audit chain.

All authorization decisions are made by Python policy code. Model output is
never treated as an authorization signal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from core import config, database
from core.config import MAX_AUTH_FAILURES, RISK_SCORE_THRESHOLD
from core.sandbox import verify_numerical_grounding

DEFAULT_USER_ID = "U1001"
_GENESIS = "0" * 64
_STATE_LOCK = threading.RLock()
_DB_LOCK = threading.RLock()
PENDING_ACTIONS: dict[str, dict[str, Any]] = {}
_SESSION_STATE: dict[str, dict[str, Any]] = {}


def _canonical_user(user_id: str | None) -> str:
    """Accept the old demo placeholder while targeting the seeded U1001 row."""
    value = str(user_id or DEFAULT_USER_ID)
    return DEFAULT_USER_ID if value == "U001" else value


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _ensure_security_schema() -> None:
    with _DB_LOCK:
        connection = database.get_connection()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    tool_name TEXT NOT NULL DEFAULT '',
                    risk_level TEXT NOT NULL DEFAULT 'GREEN',
                    status TEXT NOT NULL DEFAULT 'SUCCESS',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    prev_hash TEXT NOT NULL,
                    curr_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS security_sessions (
                    user_id TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    cumulative_risk_score INTEGER NOT NULL DEFAULT 0,
                    is_locked INTEGER NOT NULL DEFAULT 0,
                    lock_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                    pending_action_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_reason TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_action_otp (
                    pending_action_id TEXT PRIMARY KEY,
                    otp_hash TEXT NOT NULL,
                    expires_epoch INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()


class HashChainAuditLogger:
    """Append-only structured audit events linked by SHA-256 hashes."""

    def log_event(
        self, user_id: str, event_type: str, tool_name: str,
        risk_level: str, payload: dict, status: str = "SUCCESS",
    ) -> dict:
        _ensure_security_schema()
        user = _canonical_user(user_id)
        timestamp = _timestamp()
        payload_json = _json(payload or {})
        with _DB_LOCK:
            connection = database.get_connection()
            try:
                previous = connection.execute(
                    "SELECT curr_hash FROM audit_logs ORDER BY log_id DESC LIMIT 1"
                ).fetchone()
                prev_hash = previous[0] if previous else _GENESIS
                raw = f"{prev_hash}|{timestamp}|{user}|{event_type}|{tool_name}|{risk_level}|{status}|{payload_json}"
                curr_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    "INSERT INTO audit_logs (timestamp,user_id,event_type,tool_name,risk_level,status,payload_json,prev_hash,curr_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    (timestamp, user, event_type, tool_name, risk_level, status, payload_json, prev_hash, curr_hash),
                )
                connection.commit()
                return {"log_id": cursor.lastrowid, "prev_hash": prev_hash, "curr_hash": curr_hash, "timestamp": timestamp}
            finally:
                connection.close()

    def verify_audit_chain(self, limit: int = 100) -> dict:
        _ensure_security_schema()
        connection = database.get_connection()
        try:
            rows = connection.execute("SELECT * FROM audit_logs ORDER BY log_id ASC").fetchall()
        finally:
            connection.close()
        total = len(rows)
        selected = rows[-max(0, int(limit)):] if limit else []
        previous_hash = _GENESIS
        tampered = None
        valid = True
        # Verify the full chain, even when the caller asks for a small display limit.
        for row in rows:
            record = dict(row)
            if record["prev_hash"] != previous_hash:
                valid, tampered = False, record["log_id"]
                break
            raw = (
                f"{record['prev_hash']}|{record['timestamp']}|{record['user_id']}|"
                f"{record['event_type']}|{record['tool_name']}|{record['risk_level']}|"
                f"{record['status']}|{record['payload_json']}"
            )
            expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if expected != record["curr_hash"]:
                valid, tampered = False, record["log_id"]
                break
            previous_hash = record["curr_hash"]
        return {
            "is_integrity_valid": valid,
            "total_blocks": total,
            "genesis_hash": rows[0]["prev_hash"] if rows else _GENESIS,
            "latest_hash": rows[-1]["curr_hash"] if rows else _GENESIS,
            "tampered_block_id": tampered,
            "verified_at": _timestamp(),
        }

    def get_recent_audit_logs(self, limit: int = 20, user_id: str | None = None) -> list[dict]:
        _ensure_security_schema()
        connection = database.get_connection()
        try:
            if user_id:
                rows = connection.execute(
                    "SELECT * FROM audit_logs WHERE user_id=? ORDER BY log_id DESC LIMIT ?",
                    (_canonical_user(user_id), max(0, int(limit))),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_logs ORDER BY log_id DESC LIMIT ?", (max(0, int(limit)),)
                ).fetchall()
            result = []
            for item in rows:
                row = dict(item)
                row["payload"] = json.loads(row.pop("payload_json"))
                row["prev_hash_short"] = row["prev_hash"][:12]
                row["curr_hash_short"] = row["curr_hash"][:12]
                result.append(row)
            return result
        finally:
            connection.close()


class CircuitBreaker:
    """Persist risk and failure counters and lock unsafe sessions."""

    def _load(self, user_id: str) -> dict[str, Any]:
        user = _canonical_user(user_id)
        with _STATE_LOCK:
            if user in _SESSION_STATE:
                return dict(_SESSION_STATE[user])
        _ensure_security_schema()
        connection = database.get_connection()
        try:
            row = connection.execute("SELECT * FROM security_sessions WHERE user_id=?", (user,)).fetchone()
            if row:
                state = dict(row)
            else:
                user_row = connection.execute("SELECT session_locked,fail_count,risk_score FROM users WHERE user_id=?", (user,)).fetchone()
                state = {
                    "user_id": user,
                    "consecutive_failures": int(user_row["fail_count"]) if user_row else 0,
                    "cumulative_risk_score": int(user_row["risk_score"]) if user_row else 0,
                    "is_locked": int(user_row["session_locked"]) if user_row else 0,
                    "lock_reason": "数据库用户状态已锁定" if user_row and user_row["session_locked"] else "",
                }
                connection.execute(
                    "INSERT OR IGNORE INTO security_sessions (user_id,consecutive_failures,cumulative_risk_score,is_locked,lock_reason,updated_at) VALUES (?,?,?,?,?,?)",
                    (user, state["consecutive_failures"], state["cumulative_risk_score"], state["is_locked"], state["lock_reason"], _timestamp()),
                )
                connection.commit()
        finally:
            connection.close()
        with _STATE_LOCK:
            _SESSION_STATE[user] = state
        return dict(state)

    def _save(self, user_id: str, state: dict[str, Any]) -> None:
        user = _canonical_user(user_id)
        state["user_id"] = user
        with _STATE_LOCK:
            _SESSION_STATE[user] = dict(state)
        _ensure_security_schema()
        connection = database.get_connection()
        try:
            connection.execute(
                "INSERT INTO security_sessions (user_id,consecutive_failures,cumulative_risk_score,is_locked,lock_reason,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET consecutive_failures=excluded.consecutive_failures,cumulative_risk_score=excluded.cumulative_risk_score,is_locked=excluded.is_locked,lock_reason=excluded.lock_reason,updated_at=excluded.updated_at",
                (user, state["consecutive_failures"], state["cumulative_risk_score"], int(bool(state["is_locked"])), state["lock_reason"], _timestamp()),
            )
            connection.execute(
                "UPDATE users SET fail_count=?,risk_score=?,session_locked=? WHERE user_id=?",
                (state["consecutive_failures"], state["cumulative_risk_score"], int(bool(state["is_locked"])), user),
            )
            connection.commit()
        finally:
            connection.close()

    def get_security_status(self, user_id: str = DEFAULT_USER_ID) -> dict:
        state = self._load(user_id)
        return {
            "user_id": _canonical_user(user_id),
            "is_locked": bool(state["is_locked"]),
            "consecutive_failures": int(state["consecutive_failures"]),
            "cumulative_risk_score": int(state["cumulative_risk_score"]),
            "max_risk_threshold": RISK_SCORE_THRESHOLD,
            "max_failures_threshold": MAX_AUTH_FAILURES,
            "lock_reason": state["lock_reason"],
        }

    def record_event_and_check(self, user_id: str, risk_delta: int = 0, is_failure: bool = False) -> dict:
        state = self._load(user_id)
        state["cumulative_risk_score"] = max(0, int(state["cumulative_risk_score"]) + int(risk_delta))
        if is_failure:
            state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
        else:
            state["consecutive_failures"] = 0
        if not state["is_locked"]:
            if state["consecutive_failures"] >= MAX_AUTH_FAILURES:
                state["is_locked"] = 1
                state["lock_reason"] = f"连续验证失败达到 {MAX_AUTH_FAILURES} 次"
            elif state["cumulative_risk_score"] >= RISK_SCORE_THRESHOLD:
                state["is_locked"] = 1
                state["lock_reason"] = f"累计风险分达到 {RISK_SCORE_THRESHOLD} 分"
        self._save(user_id, state)
        return self.get_security_status(user_id)

    def reset_circuit_breaker(self, user_id: str = DEFAULT_USER_ID) -> dict:
        state = {"consecutive_failures": 0, "cumulative_risk_score": 0, "is_locked": 0, "lock_reason": ""}
        self._save(user_id, state)
        audit_logger.log_event(user_id, "CIRCUIT_BREAKER_MANUAL_RESET", "", "GREEN", {"reason": "人工客服审核解除风控熔断"}, "RESET")
        return {"status": "RESET", **self.get_security_status(user_id)}


class InputSecurityShield:
    """Detect instruction attacks and common social-engineering scams."""

    INJECTION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"ignore\s+(?:all\s+)?previous\s+instructions?", r"忽略.{0,8}(?:之前|所有).{0,6}(?:指令|安全规则)",
        r"system\s*prompt", r"开发者模式", r"伪造权限", r"直接改成\s*green", r"drop\s+table",
        r"delete\s+from", r"union\s+select", r"__builtins__", r"绕过验证码", r"免密直接转",
    ))
    FRAUD_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"安全账户", r"公检法", r"警官.{0,12}转", r"涉嫌洗钱", r"资产清查", r"配合调查.{0,8}保密",
        r"不能告诉.{0,5}(?:儿女|子女|家人)", r"屏幕共享", r"会议软件.{0,8}验资", r"高息保本内幕",
        r"解冻费", r"保证金",
    ))

    def inspect_user_input(self, user_text: str, user_id: str = DEFAULT_USER_ID) -> dict:
        text = str(user_text or "")
        if circuit_breaker.get_security_status(user_id)["is_locked"]:
            ticket = "CS-" + secrets.token_hex(4).upper()
            return {"passed": False, "threat_type": "SUSPICIOUS_INTENT", "risk_score_delta": 0,
                    "reason": "会话已触发安全熔断，敏感对话暂由人工客服接管。",
                    "advice": f"请联系人工客服，工单号 {ticket}。",
                    "elder_voice_text": f"为了保护您的资金，智能服务暂时停下了。人工客服工单号 {ticket}，请稍候专员协助。"}
        injection = [p.pattern for p in self.INJECTION_PATTERNS if p.search(text)]
        if injection:
            circuit_breaker.record_event_and_check(user_id, risk_delta=100, is_failure=True)
            result = {"passed": False, "threat_type": "PROMPT_INJECTION", "risk_score_delta": 100,
                      "reason": "检测到试图劫持系统规则或绕过金融安全控制的内容。",
                      "advice": "已拦截该指令，请用正常方式描述您需要办理的业务。",
                      "elder_voice_text": "这句话里有要求绕过安全规则的内容，系统先为您拦下了。请告诉我您实际想办理什么。"}
            audit_logger.log_event(user_id, "INPUT_PROMPT_INJECTION", "", "RED", {"matches": injection, "text": text[:500]}, "BLOCKED")
            return result
        fraud = [p.pattern for p in self.FRAUD_PATTERNS if p.search(text)]
        if fraud:
            circuit_breaker.record_event_and_check(user_id, risk_delta=60, is_failure=False)
            name = "您好"
            try:
                row = database.fetch_all("SELECT name FROM users WHERE user_id=?", (_canonical_user(user_id),))
                if row:
                    name = str(row[0]["name"] or "您好")
            except (sqlite3.Error, KeyError, TypeError, ValueError):
                name = "您好"
            result = {"passed": False, "threat_type": "TELECOM_FRAUD_RISK", "risk_score_delta": 60,
                      "reason": "内容包含公检法冒充、安全账户或转账保密等高危诈骗特征。",
                      "advice": "请立即停止转账和屏幕共享，通过银行官方客服电话或 96110 核实，并联系家人陪同处理。",
                      "elder_voice_text": f"{name}，公检法不会设立所谓的安全账户，也不会让您转账验资。这很可能是电信诈骗，系统已拦截，请先联系家人和银行官方客服核实。"}
            audit_logger.log_event(user_id, "INPUT_TELECOM_FRAUD_RISK", "", "RED", {"matches": fraud, "text": text[:500]}, "BLOCKED")
            return result
        return {"passed": True, "threat_type": "NONE", "risk_score_delta": 0, "reason": "未发现已知注入或诈骗特征。", "advice": "可继续进行常规意图识别。", "elder_voice_text": "好的，我来帮您处理。"}


class SecurityGateway:
    """Unified facade joining input, policy, approval, output, and audit layers."""

    def __init__(self):
        self.audit_logger = audit_logger
        self.circuit_breaker = circuit_breaker
        self.input_shield = InputSecurityShield()

    def inspect_user_input(self, user_text: str, user_id: str = DEFAULT_USER_ID) -> dict:
        return self.input_shield.inspect_user_input(user_text, user_id)

    def get_security_status(self, user_id: str = DEFAULT_USER_ID) -> dict:
        return self.circuit_breaker.get_security_status(user_id)

    def reset_circuit_breaker(self, user_id: str = DEFAULT_USER_ID) -> dict:
        return self.circuit_breaker.reset_circuit_breaker(user_id)

    def log_event(self, user_id: str, event_type: str, tool_name: str, risk_level: str, payload: dict, status: str = "SUCCESS") -> dict:
        return self.audit_logger.log_event(user_id, event_type, tool_name, risk_level, payload, status)

    def verify_audit_chain(self, limit: int = 100) -> dict:
        return self.audit_logger.verify_audit_chain(limit)

    def get_recent_audit_logs(self, limit: int = 20, user_id: str | None = None) -> list[dict]:
        return self.audit_logger.get_recent_audit_logs(limit, user_id)

    def _registry(self):
        try:
            from tools import registry
            return registry
        except (ImportError, AttributeError) as exc:
            raise RuntimeError("工具注册中心 tools.registry 不可用；请先完成工具注册中心模块。") from exc

    def _locked_response(self, user_id: str) -> dict:
        status = self.circuit_breaker.get_security_status(user_id)
        ticket = "CS-" + secrets.token_hex(4).upper()
        return {"gate_status": "CIRCUIT_BREAKER_LOCKED", "status": "blocked", "ticket_id": ticket,
                "lock_reason": status["lock_reason"], "summary": "为保护您的资金，已暂停本次服务并转接人工客服。",
                "elder_voice_text": f"您的账户安全保护已启动，资金不会继续操作。人工客服工单号 {ticket}，请稍候由专员协助。"}

    def intercept_tool_call(self, tool_name: str, arguments: dict, user_id: str = DEFAULT_USER_ID, task_id: str | None = None) -> dict:
        user = _canonical_user(user_id)
        current = self.circuit_breaker.get_security_status(user)
        if current["is_locked"]:
            response = self._locked_response(user)
            self.log_event(user, "TOOL_CALL_CIRCUIT_BREAKER_REJECT", tool_name, "RED", {"task_id": task_id}, "BLOCKED")
            return response
        registry = self._registry()
        args = dict(arguments or {})
        risk = registry.evaluate_tool_risk(tool_name, args, user)
        if risk.get("blocked"):
            status = self.circuit_breaker.record_event_and_check(user, risk_delta=50, is_failure=True)
            self.log_event(user, "TOOL_CALL_POLICY_BLOCKED", tool_name, risk.get("risk_level", "RED"), {"arguments": args, "risk": risk}, "BLOCKED")
            return {"gate_status": "BLOCKED", "status": "blocked", "risk": risk,
                    "summary": risk.get("block_reason") or risk.get("risk_reason", "操作被安全规则拦截。"),
                    "security_status": status}
        level = str(risk.get("risk_level", "GREEN")).upper()
        if level == "GREEN":
            result = registry.execute_tool(tool_name, args, user)
            success = result.get("status") == "success"
            if success:
                self.circuit_breaker.record_event_and_check(user, 0, False)
            else:
                self.circuit_breaker.record_event_and_check(user, 0, True)
            self.log_event(user, "GREEN_TOOL_EXECUTED", tool_name, "GREEN", {"arguments": args, "result": result}, "SUCCESS" if success else "ERROR")
            return {"gate_status": "EXECUTED", "status": result.get("status"), "tool_result": result}
        if level not in {"YELLOW", "RED"}:
            level = "RED"
        action_id = "ACT-" + secrets.token_hex(4).upper()
        pending = {"pending_action_id": action_id, "user_id": user, "tool_name": tool_name,
                   "arguments": args, "risk_level": level,
                   "risk_reason": risk.get("risk_reason", "需要额外授权。"), "task_id": task_id or "",
                   "created_at": _timestamp(), "status": "PENDING"}
        with _STATE_LOCK:
            PENDING_ACTIONS[action_id] = pending
        _ensure_security_schema()
        connection = database.get_connection()
        try:
            connection.execute("INSERT INTO pending_actions VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (action_id, user, tool_name, _json(args), level, pending["risk_reason"], pending["task_id"], "PENDING", pending["created_at"], pending["created_at"]))
            connection.commit()
        finally:
            connection.close()
        event = "YELLOW_GATE_PENDING" if level == "YELLOW" else "RED_GATE_PENDING_MFA"
        demo_hint = None
        if level == "RED":
            otp = f"{secrets.randbelow(1_000_000):06d}"
            pepper = config.OTP_PEPPER
            otp_hash = hmac.new(pepper.encode("utf-8"), f"{action_id}|{otp}".encode("utf-8"), hashlib.sha256).hexdigest()
            expires_epoch = int(time.time()) + 120
            connection = database.get_connection()
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO pending_action_otp "
                    "(pending_action_id,otp_hash,expires_epoch,attempts,created_at) VALUES (?,?,?,0,?)",
                    (action_id, otp_hash, expires_epoch, _timestamp()),
                )
                connection.commit()
            finally:
                connection.close()
            if config.demo_mode():
                demo_hint = {"otp": otp, "expires_in": 120}
        self.log_event(user, event, tool_name, level, {"pending_action_id": action_id, "arguments": args, "risk_reason": pending["risk_reason"]}, "PENDING")
        result = {"gate_status": "PENDING_YELLOW_CONFIRM" if level == "YELLOW" else "PENDING_RED_MFA",
                "status": "pending", "pending_action_id": action_id, "risk_level": level,
                "risk_reason": pending["risk_reason"], "confirmation_prompt": "请确认本次操作。" if level == "YELLOW" else "请输入一次性验证码完成强验证。",
                "demo_hint": demo_hint}
        return result

    def _get_pending(self, action_id: str) -> dict[str, Any] | None:
        with _STATE_LOCK:
            pending = PENDING_ACTIONS.get(action_id)
            if pending:
                return dict(pending)
        _ensure_security_schema()
        connection = database.get_connection()
        try:
            row = connection.execute("SELECT * FROM pending_actions WHERE pending_action_id=?", (action_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["arguments"] = json.loads(item.pop("arguments_json"))
            with _STATE_LOCK:
                PENDING_ACTIONS[action_id] = item
            return item
        finally:
            connection.close()

    def _set_pending_status(self, action_id: str, status: str) -> None:
        with _STATE_LOCK:
            if action_id in PENDING_ACTIONS:
                PENDING_ACTIONS[action_id]["status"] = status
        connection = database.get_connection()
        try:
            connection.execute("UPDATE pending_actions SET status=?,updated_at=? WHERE pending_action_id=?", (status, _timestamp(), action_id))
            connection.commit()
        finally:
            connection.close()

    def approve_pending_action(self, pending_action_id: str, confirm_type: str, sms_code: str | None = None,
                               user_id: str = DEFAULT_USER_ID,
                               expected_tool_name: str | None = None) -> dict:
        """Approve a legacy pending action with server-issued, expiring OTP.

        Caller-provided boolean flags are never accepted as identity proof.
        Red actions require a one-time code generated by this server.
        """
        user = _canonical_user(user_id)
        if self.circuit_breaker.get_security_status(user)["is_locked"]:
            return self._locked_response(user)
        pending = self._get_pending(pending_action_id)
        if (not pending or pending["user_id"] != user or pending["status"] != "PENDING"
                or (expected_tool_name and pending["tool_name"] != expected_tool_name)):
            return {"status": "error", "gate_status": "PENDING_ACTION_NOT_FOUND", "summary": "待确认操作不存在、已处理或不属于当前用户。"}
        if pending["risk_level"] == "YELLOW":
            if str(confirm_type).upper() not in {"CONFIRM", "YELLOW", "APPROVE", "YES"}:
                return {"status": "error", "gate_status": "CONFIRMATION_REQUIRED", "summary": "请先明确确认本次操作。"}
        else:
            connection = database.get_connection()
            try:
                challenge = connection.execute(
                    "SELECT * FROM pending_action_otp WHERE pending_action_id=?",
                    (pending_action_id,),
                ).fetchone()
                if challenge and int(challenge["expires_epoch"]) < int(time.time()):
                    connection.execute(
                        "UPDATE pending_actions SET status='EXPIRED',updated_at=? "
                        "WHERE pending_action_id=? AND status='PENDING'",
                        (_timestamp(), pending_action_id),
                    )
                    connection.commit()
                    challenge = None
            finally:
                connection.close()
            pepper = config.OTP_PEPPER
            expected = hmac.new(
                pepper.encode("utf-8"),
                f"{pending_action_id}|{str(sms_code or '')}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            valid = bool(challenge) and hmac.compare_digest(str(challenge["otp_hash"]), expected)
            if not valid:
                attempts = int(challenge["attempts"] if challenge else 0) + 1
                connection = database.get_connection()
                try:
                    if challenge:
                        connection.execute(
                            "UPDATE pending_action_otp SET attempts=? WHERE pending_action_id=?",
                            (attempts, pending_action_id),
                        )
                    if attempts >= 3 or not challenge:
                        connection.execute(
                            "UPDATE pending_actions SET status='REJECTED_MFA',updated_at=? "
                            "WHERE pending_action_id=? AND status='PENDING'",
                            (_timestamp(), pending_action_id),
                        )
                    connection.commit()
                finally:
                    connection.close()
                state = self.circuit_breaker.record_event_and_check(user, risk_delta=35, is_failure=True)
                invalidated = attempts >= 3 or not challenge
                self._set_pending_status(pending_action_id, "REJECTED_MFA" if invalidated else "PENDING")
                self.log_event(user, "RED_MFA_VERIFICATION_FAILED", pending["tool_name"], "RED", {
                    "pending_action_id": pending_action_id, "attempts": attempts,
                    "invalidated": invalidated,
                }, "FAILED")
                remaining = max(0, 3 - attempts)
                return {
                    "status": "blocked",
                    "gate_status": "MFA_VERIFICATION_FAILED",
                    "summary": f"一次性验证码未通过，剩余 {remaining} 次尝试。" if remaining else "验证码连续错误，操作已作废。",
                    "remaining_attempts": remaining,
                    "security_status": state,
                }
        claim_connection = database.get_connection()
        try:
            cursor = claim_connection.execute(
                "UPDATE pending_actions SET status='CONFIRMED',updated_at=? "
                "WHERE pending_action_id=? AND user_id=? AND status='PENDING'",
                (_timestamp(), pending_action_id, user),
            )
            claim_connection.commit()
        finally:
            claim_connection.close()
        if cursor.rowcount != 1:
            return {"status": "error", "gate_status": "PENDING_ACTION_NOT_FOUND", "summary": "待确认操作已被处理。"}
        with _STATE_LOCK:
            if pending_action_id in PENDING_ACTIONS:
                PENDING_ACTIONS[pending_action_id]["status"] = "CONFIRMED"
        registry = self._registry()
        result = registry.execute_tool(
            pending["tool_name"], pending["arguments"], user,
            mfa_verified=(pending["risk_level"] == "RED"),
        )
        if result.get("status") == "success":
            self._set_pending_status(pending_action_id, "APPROVED_AND_EXECUTED")
            self.circuit_breaker.record_event_and_check(user, 0, False)
            self.log_event(user, "PENDING_ACTION_APPROVED", pending["tool_name"], pending["risk_level"], {"pending_action_id": pending_action_id, "result": result}, "SUCCESS")
            return {"status": "success", "gate_status": "APPROVED_AND_EXECUTED", "tool_result": result,
                    "rollback_token": result.get("rollback_token"), "pending_action_id": pending_action_id}
        self._set_pending_status(pending_action_id, "EXECUTION_FAILED")
        self.circuit_breaker.record_event_and_check(user, 0, True)
        self.log_event(user, "PENDING_ACTION_EXECUTION_FAILED", pending["tool_name"], pending["risk_level"], {"pending_action_id": pending_action_id, "result": result}, "ERROR")
        return {"status": "error", "gate_status": "EXECUTION_FAILED", "tool_result": result}

    def reject_pending_action(self, pending_action_id: str, reason: str = "用户主动取消",
                              user_id: str = DEFAULT_USER_ID, expected_tool_name: str | None = None) -> dict:
        user = _canonical_user(user_id)
        pending = self._get_pending(pending_action_id)
        if (not pending or pending["user_id"] != user or pending["status"] != "PENDING"
                or (expected_tool_name and pending["tool_name"] != expected_tool_name)):
            return {"status": "error", "gate_status": "PENDING_ACTION_NOT_FOUND", "summary": "待确认操作不存在或已处理。"}
        self._set_pending_status(pending_action_id, "REJECTED_BY_USER")
        self.log_event(user, "PENDING_ACTION_REJECTED", pending["tool_name"], pending["risk_level"], {"pending_action_id": pending_action_id, "reason": reason}, "CANCELLED")
        return {"status": "success", "gate_status": "REJECTED_BY_USER", "summary": "好的，已取消这项操作，没有为您办理。", "elder_voice_text": "好的，已经取消了，资金没有转出。"}

    def inspect_and_sanitize_output(self, response_text: str, ground_truth_numbers: list | None = None, scene: str | None = None) -> dict:
        text = str(response_text or "")
        pii_masked = False
        def mask_id(match):
            nonlocal pii_masked
            pii_masked = True
            return match.group(0)[:4] + "**********" + match.group(0)[-4:]
        text = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", mask_id, text)
        def mask_card(match):
            nonlocal pii_masked
            digits = re.sub(r"\D", "", match.group(0))
            pii_masked = True
            return digits[:4] + " **** **** " + digits[-4:]
        text = re.sub(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)", mask_card, text)
        # Mask account identifiers before grounding so their digit groups are
        # never mistaken for financial amounts in the assistant response.
        grounding_text = re.sub(r"\d{4}\s+\*{4}\s+\*{4}\s+\d{4}|\d{4}\*{10}\d{4}", " ", text)
        grounding = verify_numerical_grounding(grounding_text, ground_truth_numbers or [])
        hallucinated = not grounding["passed"]
        if hallucinated:
            text += "\n\n安全提示：回复中的部分金额无法由本次银行数据核实，请以交易明细或银行页面显示为准。"
        compliance_added = False
        if scene and "wealth" in scene.lower() and not re.search(r"理财非存款|产品有风险|投资须谨慎", text):
            text += "\n（合规提示：理财非存款，产品有风险，投资须谨慎。）"
            compliance_added = True
        return {"sanitized_text": text, "grounding_check": grounding, "hallucination_detected": hallucinated,
                "pii_masked": pii_masked, "compliance_tag_added": compliance_added}


audit_logger = HashChainAuditLogger()
circuit_breaker = CircuitBreaker()
security_gateway = SecurityGateway()

__all__ = ["InputSecurityShield", "CircuitBreaker", "HashChainAuditLogger", "SecurityGateway",
           "PENDING_ACTIONS", "audit_logger", "circuit_breaker", "security_gateway"]
