"""Shared helpers, extended-schema migration, and response builders for tools."""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core import database

# The seed user created in step 1. The pasted spec used "U001" as a placeholder;
# the real demo user is U1001, so tools default here to stay runnable out of the box.
DEFAULT_USER_ID = "U1001"

SCAM_KEYWORDS = ("安全账户", "公检法", "验资", "解冻费", "高息返利", "刷单", "冒充客服")

_schema_lock = threading.Lock()
_migrated_paths: set[str] = set()


def _alter_table(connection, table: str, column_ddl: str) -> None:
    try:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
    except Exception:
        # Column already exists or the database rejected a duplicate; either way it is safe.
        pass


def ensure_extended_schema(db_path: str | Path | None = None) -> None:
    """Idempotently add auxiliary columns and tables used only by the tool layer."""
    path = str(db_path or database.DB_PATH)
    with _schema_lock:
        if path in _migrated_paths:
            return
        connection = database.get_connection(path)
        try:
            _alter_table(connection, "contacts", "bank_name TEXT NOT NULL DEFAULT ''")
            _alter_table(connection, "contacts", "relation TEXT NOT NULL DEFAULT ''")
            _alter_table(connection, "transactions", "status TEXT NOT NULL DEFAULT 'POSTED'")
            _alter_table(connection, "transactions", "remark TEXT NOT NULL DEFAULT ''")
            _alter_table(connection, "cards", "virtual_parent_card_id TEXT")
            _alter_table(connection, "cards", "usage_label TEXT NOT NULL DEFAULT ''")
            _alter_table(connection, "cards", "valid_until TEXT")
            _alter_table(connection, "cards", "credit_limit REAL")
            _alter_table(connection, "cards", "reissue_work_order TEXT NOT NULL DEFAULT ''")
            _alter_table(connection, "subscriptions", "last_used_at TEXT")
            _alter_table(connection, "subscriptions", "price_history_json TEXT NOT NULL DEFAULT '{}'")

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS aa_batches (
                    batch_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    total_amount REAL NOT NULL,
                    participant_count INTEGER NOT NULL,
                    per_share REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS aa_details (
                    detail_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES aa_batches(batch_id),
                    participant_name TEXT NOT NULL,
                    participant_account TEXT NOT NULL DEFAULT '',
                    share_amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL
                );
                """
            )

            backfill = {
                "CONTACT_01": ("工商银行", "爱人"),
                "CONTACT_02": ("建设银行", "孙子"),
                "CONTACT_03": ("招商银行", "房东"),
                "CONTACT_04": ("交通银行", "单位同事"),
                "CONTACT_05": ("农业银行", "老家表弟"),
                "CONTACT_06": ("来源不明", "外部陌生账户"),
            }
            for contact_id, (bank_name, relation) in backfill.items():
                connection.execute(
                    "UPDATE contacts SET bank_name=CASE WHEN bank_name='' THEN ? ELSE bank_name END, "
                    "relation=CASE WHEN relation='' THEN ? ELSE relation END WHERE contact_id=?",
                    (bank_name, relation, contact_id),
                )
            connection.commit()
        finally:
            connection.close()
        _migrated_paths.add(path)


@contextmanager
def db_session(db_path: str | Path | None = None):
    """Open a migrated, locked, auto-committing connection for one operation."""
    ensure_extended_schema(db_path)
    connection = database.get_connection(db_path)
    try:
        with database._DB_LOCK:
            with connection:
                yield connection
    finally:
        connection.close()


def row_dict(row) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def make_result(
    tool_name: str,
    scene: str,
    summary: str,
    *,
    status: str = "success",
    risk_level: str = "GREEN",
    risk_reason: str = "查询操作，不改变账户状态。",
    elder_voice_text: str | None = None,
    data: Any = None,
    ground_truth_numbers: list[Any] | None = None,
    rollback_token: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"success", "clarification_needed", "blocked", "error"}:
        status = "error"
    if risk_level not in {"GREEN", "YELLOW", "RED"}:
        risk_level = "RED"
    return {
        "status": status,
        "tool_name": tool_name,
        "scene": scene,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "summary": summary,
        "elder_voice_text": elder_voice_text or summary,
        "data": data if data is not None else {},
        "ground_truth_numbers": list(ground_truth_numbers or []),
        "rollback_token": rollback_token,
    }


def make_rollback_token(tool_name: str, before_snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "action_id": str(uuid.uuid4()),
        "before_snapshot": before_snapshot,
    }


def now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def days_later_text(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).date().isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def get_user(connection, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
    return row_dict(connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def get_account(connection, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
    return row_dict(connection.execute(
        "SELECT * FROM accounts WHERE user_id = ? ORDER BY account_id LIMIT 1", (user_id,)
    ).fetchone())


def account_available(account: dict[str, Any]) -> float:
    return round(float(account["balance"]) - float(account["locked_balance"]), 2)


def parse_period(period: str) -> tuple[str, str]:
    """Resolve a friendly period label into an inclusive date range."""
    today = datetime.now()
    if period == "本月":
        start = today.replace(day=1).date().isoformat()
        end = ((today.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)).date().isoformat()
    elif period == "上月":
        start = ((today.replace(day=1) - timedelta(days=1)).replace(day=1)).date().isoformat()
        end = (today.replace(day=1) - timedelta(days=1)).date().isoformat()
    elif period == "近三月":
        start = (today - timedelta(days=90)).replace(day=1).date().isoformat()
        end = today.date().isoformat()
    elif ".." in period:
        start, end = period.split("..", 1)
    elif period and len(period) == 7:  # YYYY-MM
        start = period + "-01"
        end = period + "-31"
    else:
        start, end = period, period
    end_with_time = end if len(end) > 10 else end + " 23:59:59"
    return start, end_with_time


def resolve_payee_rows(connection, payee: str, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """Match a payee by contact id, name, alias, phone, or masked card tail."""
    payee = (payee or "").strip()
    if not payee:
        return []
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM contacts WHERE user_id=? ORDER BY contact_id", (user_id,)
    ).fetchall()]
    exact = [
        row for row in rows
        if payee in (row["contact_id"], row["name"], row["phone"], row["bank_card"])
        or payee in [alias.strip() for alias in row["alias"].split(",")]
    ]
    if exact:
        return exact
    tail = payee[-4:] if len(payee) >= 4 else ""
    tail_matches = [row for row in rows if tail and row["bank_card"].endswith(tail)]
    if tail_matches:
        return tail_matches
    return [row for row in rows if payee in row["name"] or payee in row["alias"]]
