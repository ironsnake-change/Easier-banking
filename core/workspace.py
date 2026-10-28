"""Account, session, preference, and persona data for the Easier-banking workbench.

The banking tables from step 1 remain the source of truth for balances and
transactions. This module adds the small amount of application state needed by
the authenticated workbench without putting credentials in the browser.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core import database

DEMO_PASSWORD = "Demo@2026"
DEFAULT_USER_ID = "U1001"
STANDARD_USER_ID = "U2001"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _password_record(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180_000)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        _, candidate = _password_record(password, bytes.fromhex(salt_hex))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, digest_hex)


def _safe_username(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value or ""))


def _user_id() -> str:
    return "U" + uuid.uuid4().hex[:10].upper()


def _account_id() -> str:
    return "ACC_" + uuid.uuid4().hex[:8].upper()


def ensure_workspace_schema(db_path: str | Path | None = None) -> Path:
    """Create application tables and idempotently seed both demo personas."""
    path = database.init_and_seed_db(db_path)
    with _LOCK:
        connection = database.get_connection(path)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_accounts (
                    username TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    display_card_tail TEXT NOT NULL,
                    account_type TEXT NOT NULL CHECK (account_type IN ('standard', 'elder')),
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    kyc_verified INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id TEXT PRIMARY KEY,
                    chart_type TEXT NOT NULL DEFAULT 'line',
                    elder_mode INTEGER NOT NULL DEFAULT 0,
                    tts_rate REAL NOT NULL DEFAULT 1.0,
                    sound_enabled INTEGER NOT NULL DEFAULT 1,
                    auto_read INTEGER NOT NULL DEFAULT 0,
                    transfer_threshold REAL NOT NULL DEFAULT 1000.0,
                    engine_mode TEXT NOT NULL DEFAULT 'auto'
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    receipt_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pockets (
                    pocket_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    icon TEXT NOT NULL,
                    name TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_user_time ON chat_messages(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_pockets_user ON pockets(user_id);
                """
            )
            table_extensions = {
                "transactions": {
                    "status": "TEXT NOT NULL DEFAULT 'POSTED'",
                    "remark": "TEXT NOT NULL DEFAULT ''",
                },
                "subscriptions": {
                    "last_used_at": "TEXT NOT NULL DEFAULT ''",
                    "price_history_json": "TEXT NOT NULL DEFAULT '[]'",
                },
                "contacts": {
                    "bank_name": "TEXT NOT NULL DEFAULT ''",
                    "relation": "TEXT NOT NULL DEFAULT ''",
                },
                "cards": {
                    "full_card_no": "TEXT NOT NULL DEFAULT ''",
                    "expiry_date": "TEXT NOT NULL DEFAULT ''",
                    "cvv_code": "TEXT NOT NULL DEFAULT ''",
                    "recurring_debit_lock": "INTEGER NOT NULL DEFAULT 0",
                    "atm_magnet_lock": "INTEGER NOT NULL DEFAULT 0",
                    "night_limit": "REAL NOT NULL DEFAULT 5000",
                    "virtual_parent_card_id": "TEXT",
                    "usage_label": "TEXT NOT NULL DEFAULT ''",
                    "valid_until": "TEXT",
                    "credit_limit": "REAL",
                    "reissue_work_order": "TEXT NOT NULL DEFAULT ''",
                },
            }
            for table, extensions in table_extensions.items():
                existing_columns = {
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, definition in extensions.items():
                    if column not in existing_columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            _seed_zhangjg(connection)
            _seed_linyan(connection)
            connection.executemany(
                "INSERT OR IGNORE INTO transactions "
                "(transaction_id,user_id,account_id,occurred_at,direction,category,merchant,amount,balance_after,channel,is_abnormal,abnormal_reason,status,remark) "
                "VALUES (?,?,'ACC_8848',?,?,?,?,?,?,?,?,?,'POSTED','')",
                [
                    ("LY_CHART_01", "U2001", "2026-06-27 10:20:00", "IN", "季度奖金", "季度绩效奖金入账", 6200.0, 85432.0, "银行入账", 0, ""),
                    ("LY_CHART_02", "U2001", "2026-07-15 09:30:00", "IN", "理财收益", "稳健理财季度派息", 428.5, 93660.5, "理财账户", 0, ""),
                    ("LY_CHART_03", "U2001", "2026-08-22 14:10:00", "OUT", "旅游出行", "川西旅行集中支出", 5600.0, 106107.0, "银行卡", 0, ""),
                    ("LY_CHART_04", "U2001", "2026-09-28 09:00:00", "IN", "项目奖金", "季度项目奖金入账", 3800.0, 132913.0, "银行入账", 0, ""),
                    ("LY_CHART_05", "U2001", "2026-10-02 08:30:00", "OUT", "旅游出行", "川西酒店与机票结算", 1250.0, 146345.0, "银行卡", 0, ""),
                ],
            )
            connection.execute(
                "INSERT OR IGNORE INTO wealth_holdings "
                "(holding_id,user_id,product_id,amount,accrued_profit,purchased_at) "
                "VALUES ('HOLD_LY_02','U2001','PROD_02',20000.0,386.7,?)",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO cards "
                "(card_id,user_id,card_no,card_type,status,daily_limit,night_lock,overseas_lock,full_card_no,expiry_date,cvv_code,recurring_debit_lock,atm_magnet_lock,night_limit) "
                "VALUES ('CARD_LY_02','U2001','625965******4806','私人银行信用卡','ACTIVE',30000,0,0,'6259 6501 2031 4806','09/31','482',0,0,8000)"
            )
            connection.execute(
                "UPDATE cards SET full_card_no='6229 8801 2026 0921',expiry_date='10/30',cvv_code='921' "
                "WHERE card_id='CARD_001' AND user_id='U1001'"
            )
            connection.execute(
                "UPDATE cards SET full_card_no='6229 8801 2026 8848',expiry_date='10/30',cvv_code='819' "
                "WHERE card_id='CARD_LY_01' AND user_id='U2001'"
            )
            connection.commit()
        finally:
            connection.close()
    return path


def _insert_preference(connection: sqlite3.Connection, user_id: str, elder: bool) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO user_preferences "
        "(user_id,chart_type,elder_mode,tts_rate,sound_enabled,auto_read,transfer_threshold,engine_mode) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user_id, "line", int(elder), 1.0, 1, int(elder), 2000.0 if elder else 1000.0, "auto"),
    )


def _insert_pockets(connection: sqlite3.Connection, user_id: str, rows: list[tuple[str, str, float, int]]) -> None:
    for icon, name, amount, locked in rows:
        pocket_id = "POCKET_" + hashlib.sha256(f"{user_id}:{name}".encode("utf-8")).hexdigest()[:12].upper()
        connection.execute(
            "INSERT OR IGNORE INTO pockets (pocket_id,user_id,icon,name,amount,locked,created_at) VALUES (?,?,?,?,?,?,?)",
            (pocket_id, user_id, icon, name, amount, locked, _now()),
        )


def _insert_auth(connection: sqlite3.Connection, username: str, user_id: str, tail: str, account_type: str) -> None:
    salt, digest = _password_record(DEMO_PASSWORD)
    connection.execute(
        "INSERT OR IGNORE INTO auth_accounts "
        "(username,user_id,display_card_tail,account_type,password_salt,password_hash,kyc_verified,created_at) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (username, user_id, tail, account_type, salt, digest, _now()),
    )


def _seed_zhangjg(connection: sqlite3.Connection) -> None:
    user_id = DEFAULT_USER_ID
    _insert_auth(connection, "zhangjg", user_id, "0921", "elder")
    _insert_preference(connection, user_id, True)
    _insert_pockets(connection, user_id, [("🏥", "医疗健康备用金", 40000.0, 1), ("🎁", "大孙子教育基金", 10000.0, 0)])


def _seed_linyan(connection: sqlite3.Connection) -> None:
    user_id = STANDARD_USER_ID
    exists = connection.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not exists:
        connection.execute(
            "INSERT INTO users (user_id,name,age,risk_level,daily_transfer_used,elderly_mode,session_locked,fail_count,risk_score) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, "林砚", 26, "C1(成长型)", 320.0, 0, 0, 0, 0),
        )
        account_id = "ACC_8848"
        connection.execute(
            "INSERT INTO accounts (account_id,user_id,account_name,balance,locked_balance,currency) VALUES (?,?,?,?,?,?)",
            (account_id, user_id, "智享主账户", 42850.0, 6500.0, "CNY"),
        )
        contacts = [
            ("CONTACT_LY_01", "房东陈姐", "房东,陈姐", "13610006612", "622202******6612", 0, ""),
            ("CONTACT_LY_02", "合租室友阿泽", "室友,阿泽", "13710001094", "622202******1094", 0, ""),
        ]
        connection.executemany(
            "INSERT INTO contacts (contact_id,user_id,name,alias,phone,bank_card,birthday,usual_amount,is_risky,risk_tags) "
            "VALUES (?,?,?,?,?,?,'',?,0,?)",
            [(cid, user_id, name, alias, phone, card, usual, tags) for cid, name, alias, phone, card, usual, tags in contacts],
        )
        transactions = [
            ("LY_TX_01", "2026-05-05 09:00:00", "IN", "工资养老金收入", "月度工资入账", 18500.0, 61000.0, "银行入账", 0, ""),
            ("LY_TX_02", "2026-05-08 20:15:22", "OUT", "餐饮美食", "社区餐厅", 186.0, 60814.0, "银行卡", 0, ""),
            ("LY_TX_03", "2026-05-16 11:02:04", "OUT", "交通出行", "网约车", 82.0, 60732.0, "快捷支付", 0, ""),
            ("LY_TX_04", "2026-06-05 09:00:00", "IN", "工资养老金收入", "月度工资入账", 18500.0, 79232.0, "银行入账", 0, ""),
            ("LY_TX_05", "2026-06-12 15:20:11", "OUT", "商超购物", "数码城", 4500.0, 74732.0, "银行卡", 0, ""),
            ("LY_TX_06", "2026-07-05 09:00:00", "IN", "工资养老金收入", "月度工资入账", 18500.0, 93232.0, "银行入账", 0, ""),
            ("LY_TX_07", "2026-07-21 19:18:06", "OUT", "休闲娱乐", "流媒体UHD", 25.0, 93207.0, "自动代扣", 0, ""),
            ("LY_TX_08", "2026-08-05 09:00:00", "IN", "工资养老金收入", "月度工资入账", 18500.0, 111707.0, "银行入账", 0, ""),
            ("LY_TX_09", "2026-08-18 13:40:00", "OUT", "交通出行", "高铁出行", 368.0, 111339.0, "银行卡", 0, ""),
            ("LY_TX_10", "2026-09-05 09:00:00", "IN", "工资养老金收入", "月度工资入账", 18500.0, 129839.0, "银行入账", 0, ""),
            ("LY_TX_11", "2026-09-23 18:26:40", "OUT", "商超购物", "家居生活馆", 726.0, 129113.0, "银行卡", 0, ""),
            ("LY_TX_12", "2026-10-05 09:00:00", "IN", "工资养老金收入", "月度工资入账", 18500.0, 147613.0, "银行入账", 0, ""),
            ("LY_TX_13", "2026-10-18 10:08:21", "OUT", "数码订阅", "云空间自动续费", 18.0, 147595.0, "自动代扣", 0, ""),
            ("LY_TX_14", "2026-10-24 03:15:00", "OUT", "境外消费", "境外未知商户小额探测扣款", 68.0, 147527.0, "境外无卡支付", 1, "凌晨境外未知商户小额探测扣款"),
        ]
        connection.executemany(
            "INSERT INTO transactions (transaction_id,user_id,account_id,occurred_at,direction,category,merchant,amount,balance_after,channel,is_abnormal,abnormal_reason) "
            "VALUES (?,?,'ACC_8848',?,?,?,?,?,?,?,?,?)",
            [(tid, user_id, *row) for tid, *row in transactions],
        )
        connection.executemany(
            "INSERT INTO cards (card_id,user_id,card_no,card_type,status,daily_limit,night_lock,overseas_lock) VALUES (?,?,?,?,?,?,?,?)",
            [("CARD_LY_01", user_id, "622208******8848", "智享金卡(主力卡)", "ACTIVE", 20000.0, 0, 0)],
        )
        connection.executemany(
            "INSERT INTO subscriptions (sub_id,user_id,merchant,amount,cycle,next_deduct_date,status) VALUES (?,?,?,?,?,?,?)",
            [
                ("SUB_LY_01", user_id, "流媒体UHD连续包月", 25.0, "每月15日", "2026-11-15", "ACTIVE"),
                ("SUB_LY_02", user_id, "云空间自动续费", 18.0, "每月28日", "2026-10-28", "ACTIVE"),
                ("SUB_LY_03", user_id, "健身房会员隐蔽代扣", 199.0, "每月08日", "2026-11-08", "ACTIVE"),
            ],
        )
        connection.execute(
            "INSERT INTO wealth_holdings (holding_id,user_id,product_id,amount,accrued_profit,purchased_at) VALUES (?,?,?,?,?,?)",
            ("HOLD_LY_01", user_id, "PROD_01", 30000.0, 412.5, "2026-05-18 10:00:00"),
        )
    _insert_auth(connection, "linyan", user_id, "8848", "standard")
    _insert_preference(connection, user_id, False)
    _insert_pockets(connection, user_id, [("✈️", "川西旅行基金", 2000.0, 1), ("💻", "数码装备储备", 4500.0, 0)])


def get_auth_account(username_or_card: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    ensure_workspace_schema(db_path)
    connection = database.get_connection(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM auth_accounts WHERE username=? OR display_card_tail=? LIMIT 1",
            ((username_or_card or "").strip(), (username_or_card or "").strip()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def authenticate(username_or_card: str, password: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    account = get_auth_account(username_or_card, db_path)
    if not account or not verify_password(password, account["password_salt"], account["password_hash"]):
        return None
    return account


def create_session(user_id: str, db_path: str | Path | None = None) -> str:
    ensure_workspace_schema(db_path)
    token = secrets.token_urlsafe(32)
    stamp = _now()
    connection = database.get_connection(db_path)
    try:
        connection.execute(
            "INSERT INTO app_sessions (token_hash,user_id,created_at,last_seen_at) VALUES (?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), user_id, stamp, stamp),
        )
        connection.commit()
    finally:
        connection.close()
    return token


def resolve_session(token: str | None, db_path: str | Path | None = None) -> str | None:
    if not token:
        return None
    ensure_workspace_schema(db_path)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection = database.get_connection(db_path)
    try:
        row = connection.execute("SELECT user_id FROM app_sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if not row:
            return None
        connection.execute("UPDATE app_sessions SET last_seen_at=? WHERE token_hash=?", (_now(), token_hash))
        connection.commit()
        return str(row["user_id"])
    finally:
        connection.close()


def delete_session(token: str | None, db_path: str | Path | None = None) -> None:
    if not token:
        return
    connection = database.get_connection(db_path)
    try:
        connection.execute("DELETE FROM app_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        connection.commit()
    finally:
        connection.close()


def register_account(name: str, username: str, password: str, account_type: str, db_path: str | Path | None = None) -> dict[str, Any]:
    ensure_workspace_schema(db_path)
    username = (username or "").strip()
    name = (name or "").strip()
    account_type = account_type if account_type in {"standard", "elder"} else "standard"
    if not name or len(name) > 40:
        raise ValueError("请输入有效的真实姓名")
    if not _safe_username(username):
        raise ValueError("账号需以字母开头，长度为4到32位")
    if len(password or "") < 6:
        raise ValueError("安全密码至少需要6位")
    connection = database.get_connection(db_path)
    try:
        if connection.execute("SELECT 1 FROM auth_accounts WHERE username=?", (username,)).fetchone():
            raise ValueError("该登录账号已存在")
        user_id = _user_id()
        account_id = _account_id()
        elder = account_type == "elder"
        salt, digest = _password_record(password)
        connection.execute(
            "INSERT INTO users (user_id,name,age,risk_level,daily_transfer_used,elderly_mode,session_locked,fail_count,risk_score) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, name, 65 if elder else 35, "C2(稳健型)" if elder else "C1(成长型)", 0.0, int(elder), 0, 0, 0),
        )
        connection.execute(
            "INSERT INTO accounts (account_id,user_id,account_name,balance,locked_balance,currency) VALUES (?,?,?,?,?,?)",
            (account_id, user_id, "颐养安心主账户" if elder else "标准智享主账户", 50000.0, 0.0, "CNY"),
        )
        tail = account_id[-4:]
        connection.execute(
            "INSERT INTO auth_accounts (username,user_id,display_card_tail,account_type,password_salt,password_hash,kyc_verified,created_at) VALUES (?,?,?,?,?,?,1,?)",
            (username, user_id, tail, account_type, salt, digest, _now()),
        )
        connection.execute(
            "INSERT INTO cards (card_id,user_id,card_no,card_type,status,daily_limit,night_lock,overseas_lock) VALUES (?,?,?,?,?,?,?,?)",
            ("CARD_" + uuid.uuid4().hex[:8].upper(), user_id, "622208******" + tail, "颐养安心卡" if elder else "智享金卡", "ACTIVE", 2000.0 if elder else 20000.0, 0, 0),
        )
        connection.commit()
        _seed_registered_user(connection, user_id, account_id, elder)
        connection.commit()
    finally:
        connection.close()
    return get_auth_account(username, db_path) or {}


def _seed_registered_user(connection: sqlite3.Connection, user_id: str, account_id: str, elder: bool) -> None:
    now = _now()
    names = [("常用联系人", "家人,亲属", "13800138000", "622202******0000", 0, "")]
    connection.executemany(
        "INSERT INTO contacts (contact_id,user_id,name,alias,phone,bank_card,birthday,usual_amount,is_risky,risk_tags) VALUES (?,?,?,?,?,?,'',?,0,?)",
        [("CONTACT_" + uuid.uuid4().hex[:8].upper(), user_id, *row) for row in names],
    )
    monthly_amount = 6800.0 if elder else 12000.0
    txs = []
    for index, month in enumerate(("2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"), 1):
        txs.append((f"REG_{user_id}_{index}_IN", user_id, account_id, f"{month}-05 09:00:00", "IN", "工资养老金收入", "工资或养老金入账", monthly_amount, 50000 + index * 100, "银行入账", 0, ""))
        txs.append((f"REG_{user_id}_{index}_OUT", user_id, account_id, f"{month}-18 12:30:00", "OUT", "日常生活", "社区生活消费", 680.0 if elder else 1280.0, 49320 - index * 10, "银行卡", 0, ""))
    connection.executemany(
        "INSERT INTO transactions (transaction_id,user_id,account_id,occurred_at,direction,category,merchant,amount,balance_after,channel,is_abnormal,abnormal_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        txs,
    )
    connection.execute(
        "INSERT INTO subscriptions (sub_id,user_id,merchant,amount,cycle,next_deduct_date,status) VALUES (?,?,?,?,?,?,?)",
        ("SUB_" + uuid.uuid4().hex[:8].upper(), user_id, "基础生活服务代扣", 39.0 if elder else 18.0, "每月28日", "2026-11-28", "ACTIVE"),
    )
    _insert_preference(connection, user_id, elder)
    _insert_pockets(connection, user_id, [("🏥" if elder else "🎯", "健康备用金" if elder else "自由目标基金", 10000.0, int(elder)), ("🎁" if elder else "💻", "家庭关怀基金" if elder else "数码储备", 5000.0, 0)])


def get_user_profile(user_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    ensure_workspace_schema(db_path)
    connection = database.get_connection(db_path)
    try:
        row = connection.execute(
            "SELECT a.username,a.user_id,a.display_card_tail,a.account_type,a.kyc_verified,a.created_at,"
            "u.name,u.age,u.risk_level,u.elderly_mode "
            "FROM auth_accounts a JOIN users u ON u.user_id=a.user_id WHERE a.user_id=?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        connection.close()


def get_preferences(user_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    ensure_workspace_schema(db_path)
    connection = database.get_connection(db_path)
    try:
        row = connection.execute("SELECT * FROM user_preferences WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        connection.close()


def update_preferences(user_id: str, values: dict[str, Any], db_path: str | Path | None = None) -> dict[str, Any]:
    ensure_workspace_schema(db_path)
    allowed = {"chart_type", "elder_mode", "tts_rate", "sound_enabled", "auto_read", "transfer_threshold", "engine_mode"}
    values = {key: value for key, value in values.items() if key in allowed}
    if "chart_type" in values and values["chart_type"] not in {"line", "bar", "doughnut"}:
        values["chart_type"] = "line"
    if "tts_rate" in values:
        values["tts_rate"] = max(0.8, min(1.4, float(values["tts_rate"])))
    if "transfer_threshold" in values:
        values["transfer_threshold"] = max(500.0, min(10000.0, float(values["transfer_threshold"])))
    if "engine_mode" in values and values["engine_mode"] not in {"auto", "local"}:
        values["engine_mode"] = "auto"
    if not values:
        return get_preferences(user_id, db_path)
    columns = ", ".join(f"{key}=?" for key in values)
    connection = database.get_connection(db_path)
    try:
        connection.execute(f"UPDATE user_preferences SET {columns} WHERE user_id=?", (*values.values(), user_id))
        if "elder_mode" in values:
            connection.execute("UPDATE users SET elderly_mode=? WHERE user_id=?", (int(bool(values["elder_mode"])), user_id))
        connection.commit()
    finally:
        connection.close()
    return get_preferences(user_id, db_path)


def get_chat_messages(user_id: str, limit: int = 80, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    ensure_workspace_schema(db_path)
    connection = database.get_connection(db_path)
    try:
        rows = connection.execute("SELECT * FROM chat_messages WHERE user_id=? ORDER BY message_id DESC LIMIT ?", (user_id, max(1, min(limit, 200)))).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["receipt"] = json.loads(item.pop("receipt_json"))
            except json.JSONDecodeError:
                item["receipt"] = {}
            result.append(item)
        return result
    finally:
        connection.close()


def append_chat_message(user_id: str, role: str, content: str, receipt: dict | None = None, db_path: str | Path | None = None) -> int:
    ensure_workspace_schema(db_path)
    connection = database.get_connection(db_path)
    try:
        cursor = connection.execute("INSERT INTO chat_messages (user_id,role,content,receipt_json,created_at) VALUES (?,?,?,?,?)", (user_id, role, content, _json(receipt), _now()))
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def get_pockets(user_id: str, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    ensure_workspace_schema(db_path)
    connection = database.get_connection(db_path)
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM pockets WHERE user_id=? ORDER BY pocket_id", (user_id,)).fetchall()]
    finally:
        connection.close()


def reset_workspace_data(db_path: str | Path | None = None) -> Path:
    path = database.reset_demo_db(db_path)
    connection = database.get_connection(path)
    try:
        for table in ("app_sessions", "chat_messages", "pockets", "user_preferences", "auth_accounts"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
    finally:
        connection.close()
    return ensure_workspace_schema(path)


__all__ = [
    "DEMO_PASSWORD", "DEFAULT_USER_ID", "STANDARD_USER_ID", "ensure_workspace_schema",
    "authenticate", "create_session", "resolve_session", "delete_session", "register_account",
    "get_user_profile", "get_preferences", "update_preferences", "get_chat_messages",
    "append_chat_message", "get_pockets", "reset_workspace_data",
]
