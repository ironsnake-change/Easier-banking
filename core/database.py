"""SQLite demo bank database with deterministic, realistic seed data.

Run python -m core.database from the Easier-banking directory to initialize it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "bank_core.db"
_DB_LOCK = threading.RLock()


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a thread-shareable SQLite connection that yields dictionary rows."""
    path = Path(db_path) if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def _connection_scope(db_path: str | Path | None = None):
    """Commit on success and always close a connection after its operation."""
    connection = get_connection(db_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def fetch_all(sql: str, parameters: tuple[Any, ...] = (), db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Execute a read query and return rows as ordinary dictionaries."""
    with _connection_scope(db_path) as connection:
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, name TEXT NOT NULL, age INTEGER NOT NULL,
            risk_level TEXT NOT NULL, daily_transfer_used REAL NOT NULL DEFAULT 0,
            elderly_mode INTEGER NOT NULL DEFAULT 0 CHECK (elderly_mode IN (0, 1)),
            session_locked INTEGER NOT NULL DEFAULT 0 CHECK (session_locked IN (0, 1)),
            fail_count INTEGER NOT NULL DEFAULT 0, risk_score INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
            account_name TEXT NOT NULL, balance REAL NOT NULL CHECK (balance >= 0),
            locked_balance REAL NOT NULL DEFAULT 0 CHECK (locked_balance >= 0),
            currency TEXT NOT NULL DEFAULT 'CNY'
        );
        CREATE TABLE IF NOT EXISTS contacts (
            contact_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
            name TEXT NOT NULL, alias TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL,
            bank_card TEXT NOT NULL, birthday TEXT, usual_amount REAL,
            is_risky INTEGER NOT NULL DEFAULT 0 CHECK (is_risky IN (0, 1)),
            risk_tags TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
            account_id TEXT NOT NULL REFERENCES accounts(account_id), occurred_at TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('IN', 'OUT')), category TEXT NOT NULL,
            merchant TEXT NOT NULL, amount REAL NOT NULL CHECK (amount > 0),
            balance_after REAL NOT NULL, channel TEXT NOT NULL,
            is_abnormal INTEGER NOT NULL DEFAULT 0 CHECK (is_abnormal IN (0, 1)),
            abnormal_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS wealth_products (
            product_id TEXT PRIMARY KEY, name TEXT NOT NULL, risk_grade TEXT NOT NULL,
            expected_yield TEXT NOT NULL, min_amount REAL NOT NULL, term_days INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS wealth_holdings (
            holding_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
            product_id TEXT NOT NULL REFERENCES wealth_products(product_id),
            amount REAL NOT NULL CHECK (amount >= 0), accrued_profit REAL NOT NULL DEFAULT 0,
            purchased_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cards (
            card_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
            card_no TEXT NOT NULL, card_type TEXT NOT NULL, status TEXT NOT NULL,
            daily_limit REAL NOT NULL, night_lock INTEGER NOT NULL DEFAULT 0 CHECK (night_lock IN (0, 1)),
            overseas_lock INTEGER NOT NULL DEFAULT 0 CHECK (overseas_lock IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            sub_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
            merchant TEXT NOT NULL, amount REAL NOT NULL CHECK (amount >= 0), cycle TEXT NOT NULL,
            next_deduct_date TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CANCELLED'))
        );
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
            task_type TEXT NOT NULL CHECK (task_type IN ('SCHEDULED_TRANSFER', 'FUND_LOCK', 'LIFESTYLE_ORDER', 'AA_SPLIT')),
            description TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0, target_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CANCELLED', 'ROLLED_BACK')),
            metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_user_time ON transactions(user_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_contacts_user_name ON contacts(user_id, name);
        CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON scheduled_tasks(user_id, status);
        """
    )


def _seed(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("U1001", "张建国", 65, "C2(稳健型)", 400.0, 1, 0, 0, 0),
    )
    connection.execute(
        "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)",
        ("ACC_8888", "U1001", "活期主账户", 28500.00, 0.00, "CNY"),
    )
    contacts = [
        ("CONTACT_01", "王芳", "爱人,老婆,老伴", "13800138001", "622202******1101", "11-15", None, 0, ""),
        ("CONTACT_02", "许小宝", "大孙子,孙子,小宝", "13900139002", "622202******2202", None, 500.0, 0, ""),
        ("CONTACT_03", "李强", "房东,收租李哥", "13700137003", "622202******3303", None, 2500.0, 0, ""),
        ("CONTACT_04", "张伟", "单位同事,张伟同事", "13600136004", "622202******4404", None, None, 0, ""),
        ("CONTACT_05", "张伟", "老家表弟,张伟表弟", "13500135005", "622202******5505", None, None, 0, ""),
        ("CONTACT_06", "林某(外部陌生账户)", "安全账户,公检法核查专户,高息返利专员", "17099998888", "621700******9999", None, None, 1, "反诈黑灰名单;疑似冒充公检法;高息返利"),
    ]
    connection.executemany(
        "INSERT INTO contacts (contact_id, user_id, name, alias, phone, bank_card, birthday, usual_amount, is_risky, risk_tags) VALUES (?, 'U1001', ?, ?, ?, ?, ?, ?, ?, ?)",
        contacts,
    )

    # Twenty transactions across August, September, and October 2026.
    transactions = [
        ("TX20260801001", "2026-08-01 08:12:20", "OUT", "餐饮美食", "社区早餐铺", 18.00, 28482.00, "银行卡"),
        ("TX20260805002", "2026-08-05 09:00:00", "IN", "工资养老金收入", "养老金发放", 4200.00, 32682.00, "银行入账"),
        ("TX20260808003", "2026-08-08 16:34:12", "OUT", "商超购物", "永辉超市", 286.40, 32395.60, "银行卡"),
        ("TX20260812004", "2026-08-12 10:15:43", "OUT", "医疗健康", "社区卫生服务中心", 96.00, 32299.60, "银行卡"),
        ("TX20260816005", "2026-08-16 19:20:01", "OUT", "水电燃气", "南方电网缴费", 138.70, 32160.90, "生活缴费"),
        ("TX20260822006", "2026-08-22 18:40:16", "OUT", "休闲娱乐", "人民公园茶座", 42.00, 32118.90, "银行卡"),
        ("TX20260827007", "2026-08-27 12:06:35", "OUT", "餐饮美食", "老街饭店", 128.00, 31990.90, "银行卡"),
        ("TX20260901008", "2026-09-01 08:33:09", "OUT", "餐饮美食", "社区早餐铺", 21.00, 31969.90, "银行卡"),
        ("TX20260905009", "2026-09-05 09:00:00", "IN", "工资养老金收入", "养老金发放", 4200.00, 36169.90, "银行入账"),
        ("TX20260909010", "2026-09-09 14:12:08", "OUT", "商超购物", "华润万家", 413.60, 35756.30, "银行卡"),
        ("TX20260913011", "2026-09-13 11:26:50", "OUT", "医疗健康", "仁心药房", 186.50, 35569.80, "银行卡"),
        ("TX20260918012", "2026-09-18 20:10:22", "OUT", "水电燃气", "燃气缴费", 76.30, 35493.50, "生活缴费"),
        ("TX20260922013", "2026-09-22 15:42:33", "OUT", "休闲娱乐", "社区老年活动中心", 60.00, 35433.50, "银行卡"),
        ("TX20260928014", "2026-09-28 12:35:11", "OUT", "餐饮美食", "邻里家常菜", 88.00, 35345.50, "银行卡"),
        ("TX20261005015", "2026-10-05 09:00:00", "IN", "工资养老金收入", "养老金发放", 4200.00, 39545.50, "银行入账"),
        ("TX20261010016", "2026-10-10 17:18:04", "OUT", "商超购物", "永辉超市", 532.80, 39012.70, "银行卡"),
        ("TX20261018017", "2026-10-18 10:04:55", "OUT", "医疗健康", "市人民医院门诊", 328.00, 38684.70, "银行卡"),
        ("TX2026102418A", "2026-10-24 14:20:05", "OUT", "休闲娱乐", "星光视频SVIP", 25.00, 38659.70, "快捷支付"),
        ("TX2026102418B", "2026-10-24 14:20:08", "OUT", "休闲娱乐", "星光视频SVIP", 25.00, 38634.70, "快捷支付"),
        ("TX20261025019", "2026-10-25 03:15:00", "OUT", "休闲娱乐", "OVERSEAS_CASINO_LUX", 680.00, 37954.70, "境外无卡支付"),
    ]
    for row in transactions:
        abnormal = row[0] in ("TX2026102418A", "TX2026102418B", "TX20261025019")
        reason = "凌晨境外高危商户疑似盗刷" if row[0] == "TX20261025019" else ("短时间毫秒级重复扣费" if abnormal else "")
        connection.execute(
            "INSERT INTO transactions (transaction_id, user_id, account_id, occurred_at, direction, category, merchant, amount, balance_after, channel, is_abnormal, abnormal_reason) VALUES (?, 'U1001', 'ACC_8888', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row[0], *row[1:], int(abnormal), reason),
        )

    products = [
        ("PROD_01", "微众活期+朝朝宝", "R1", "2.35%", 1.0, 0, "活期低风险产品"),
        ("PROD_02", "微众稳健季季盈", "R2", "3.45%", 100.0, 90, "稳健型定期产品，适配C2风险等级"),
        ("PROD_03", "全球科技激进股票精选", "R4", "8.80% (高波动)", 1000.0, 180, "中高风险产品，申购前需进行适当性评估"),
    ]
    connection.executemany("INSERT INTO wealth_products VALUES (?, ?, ?, ?, ?, ?, ?)", products)
    connection.execute(
        "INSERT INTO wealth_holdings VALUES (?, 'U1001', 'PROD_01', ?, ?, ?)",
        ("HOLD_01", 10000.00, 186.50, "2026-07-15 10:30:00"),
    )
    cards = [
        ("CARD_001", "U1001", "622208******8888", "借记卡(主力卡)", "ACTIVE", 20000.0, 0, 0),
        ("CARD_002", "U1001", "625965******6666", "信用卡", "ACTIVE", 10000.0, 0, 0),
    ]
    connection.executemany("INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)", cards)
    subscriptions = [
        ("SUB_01", "U1001", "星光视频SVIP连续包月", 25.0, "每月15日", "2026-11-15", "ACTIVE"),
        ("SUB_02", "U1001", "极速云盘自动续费会员", 18.0, "每月28日", "2026-10-28", "ACTIVE"),
        ("SUB_03", "U1001", "夕阳红百万医疗险月缴", 120.0, "每月01日", "2026-11-01", "ACTIVE"),
    ]
    connection.executemany("INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)", subscriptions)
    tasks = [
        ("TASK_001", "SCHEDULED_TRANSFER", "每月向房东李强支付房租", 2500.0, "2026-11-01", "ACTIVE", {"contact_id": "CONTACT_03", "recurrence": "MONTHLY"}, "2026-10-01 09:00:00"),
        ("TASK_002", "FUND_LOCK", "为爱人王芳生日预留专项资金", 2000.0, "2026-11-15", "ACTIVE", {"contact_id": "CONTACT_01", "purpose": "生日安排"}, "2026-10-20 11:30:00"),
        ("TASK_003", "LIFESTYLE_ORDER", "家庭聚餐待确认订单", 688.0, "2026-11-15", "ACTIVE", {"merchant": "邻里家宴", "guests": 6}, "2026-10-22 16:45:00"),
        ("TASK_004", "AA_SPLIT", "社区旅行AA费用待收款", 320.0, "2026-10-30", "ACTIVE", {"participants": 4, "collected": 2}, "2026-10-23 08:20:00"),
    ]
    connection.executemany(
        "INSERT INTO scheduled_tasks VALUES (?, 'U1001', ?, ?, ?, ?, ?, ?, ?)",
        [(task_id, task_type, description, amount, target_date, status,
          json.dumps(metadata, ensure_ascii=False), created_at)
         for task_id, task_type, description, amount, target_date, status, metadata, created_at in tasks],
    )


def init_and_seed_db(db_path: str | Path | None = None) -> Path:
    """Create tables and seed the demo once; safe to call repeatedly."""
    path = Path(db_path) if db_path is not None else DB_PATH
    with _DB_LOCK, _connection_scope(path) as connection:
        _create_schema(connection)
        existing = connection.execute("SELECT 1 FROM users WHERE user_id = 'U1001'").fetchone()
        if existing is None:
            _seed(connection)
        connection.commit()
    return path


def reset_demo_db(db_path: str | Path | None = None) -> Path:
    """Drop and recreate demo tables, restoring the original seed data."""
    path = Path(db_path) if db_path is not None else DB_PATH
    table_names = (
        "scheduled_tasks", "subscriptions", "cards", "wealth_holdings",
        "wealth_products", "transactions", "contacts", "accounts", "users",
    )
    with _DB_LOCK, _connection_scope(path) as connection:
        for table_name in table_names:
            connection.execute(f"DROP TABLE IF EXISTS {table_name}")
        connection.commit()
        _create_schema(connection)
        _seed(connection)
        connection.commit()
    return path


def _print_database_summary(db_path: str | Path | None = None) -> None:
    path = init_and_seed_db(db_path)
    tables = (
        "users", "accounts", "contacts", "transactions", "wealth_products",
        "wealth_holdings", "cards", "subscriptions", "scheduled_tasks",
    )
    print(f"SQLite database ready: {path}")
    with _connection_scope(path) as connection:
        for table_name in tables:
            count = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            print(f"{table_name}: {count}")
        user = dict(connection.execute("SELECT * FROM users WHERE user_id = 'U1001'").fetchone())
        account = dict(connection.execute("SELECT * FROM accounts WHERE account_id = 'ACC_8888'").fetchone())
    print(
        "Account summary: "
        f"{user['name']} ({user['user_id']}), {account['account_id']}, "
        f"balance={account['balance']:.2f} {account['currency']}, "
        f"locked={account['locked_balance']:.2f}, "
        f"daily_transfer_used={user['daily_transfer_used']:.2f}, "
        f"elderly_mode={user['elderly_mode']}"
    )


if __name__ == "__main__":
    _print_database_summary()
