from __future__ import annotations

import pytest

from core import database, workspace
from core.ai_banking import _extract_json_action, authorization_level, execute_action, infer_action
from main import _tts_summary


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ai-actions.sqlite3")
    workspace.ensure_workspace_schema()
    return database.DB_PATH


def test_semantic_actions_and_levels_are_grounded(seeded_db):
    cancel = infer_action("帮我退订 MuseScore 的订阅")
    assert cancel["action"] == "cancel_subscription"
    assert cancel["params"]["service_name"] == "MuseScore"
    assert authorization_level("U2001", cancel)[0] == "L2"

    transfer = infer_action("帮我给周叙白转 500 元")
    assert transfer == {
        "action": "transfer",
        "params": {"recipient": "周叙白", "amount": 500.0, "remark": "私人金融助理发起"},
    }
    assert authorization_level("U2001", transfer)[0] == "L3"
    known_transfer = infer_action("帮我给房东陈姐转 500 元")
    assert authorization_level("U2001", known_transfer)[0] == "L2"
    assert authorization_level("U2001", {**transfer, "params": {**transfer["params"], "amount": 2000.0}})[0] == "L3"

    card = infer_action("帮我把 8848 卡的境外支付锁死")
    assert card["action"] == "card_control"
    assert card["params"] == {"card_tail": "8848", "operation": "overseas_lock", "value": True}
    assert authorization_level("U2001", card)[0] == "L1"


def test_actions_mutate_balances_subscriptions_and_cards(seeded_db):
    before = database.fetch_all("SELECT balance FROM accounts WHERE user_id='U2001'")[0]["balance"]
    transfer = {"action": "transfer", "params": {"recipient": "房东陈姐", "amount": 300, "remark": "房租"}}
    result = execute_action("U2001", transfer, level="L2")
    assert result["status"] == "success"
    after = database.fetch_all("SELECT balance FROM accounts WHERE user_id='U2001'")[0]["balance"]
    assert round(before - after, 2) == 300.0
    assert database.fetch_all(
        "SELECT 1 FROM transactions WHERE user_id='U2001' AND merchant='房东陈姐'"
    )

    cancel = execute_action("U2001", infer_action("帮我退订 MuseScore 的订阅"), level="L2")
    assert cancel["status"] == "success"
    dynamic = database.fetch_all(
        "SELECT merchant,amount,status FROM subscriptions WHERE user_id='U2001' AND merchant LIKE '%MuseScore%'"
    )
    assert dynamic and dynamic[0]["amount"] == 38.0 and dynamic[0]["status"] == "CANCELLED"

    card = execute_action("U2001", infer_action("帮我把 8848 卡的境外支付锁死"), level="L1")
    assert card["status"] == "success"
    locked = database.fetch_all(
        "SELECT overseas_lock FROM cards WHERE user_id='U2001' AND card_no LIKE '%8848'"
    )
    assert locked and locked[0]["overseas_lock"] == 1

    pocket = execute_action(
        "U2001",
        {"action": "manage_pocket", "params": {"operation": "create", "name": "购琴基金", "amount": 3000}},
        level="L2",
    )
    assert pocket["status"] == "success"
    created = database.fetch_all("SELECT amount FROM pockets WHERE user_id='U2001' AND name='购琴基金'")
    assert created and created[0]["amount"] == 3000.0

    balance_before_redeem = database.fetch_all("SELECT balance FROM accounts WHERE user_id='U2001'")[0]["balance"]
    redeemed = execute_action(
        "U2001",
        {"action": "redeem_wealth", "params": {"product_name": "活期+日日享", "amount": 1000}},
        level="L2",
    )
    assert redeemed["status"] == "success"
    assert redeemed["data"]["holding_id"] == "HOLD_LY_01"
    assert round(
        database.fetch_all("SELECT balance FROM accounts WHERE user_id='U2001'")[0]["balance"]
        - balance_before_redeem,
        2,
    ) == 1000.0


def test_voice_summary_is_a_short_grounded_brief(seeded_db):
    summary = _tts_summary("U2001", "为您汇总近六个月收支情况。")
    assert "林砚您好" in summary
    assert "累计收入" in summary
    assert "总支出" in summary
    assert "尾号8848" in summary
    assert "SHA-256" not in summary


def test_raw_tool_json_is_parsed_and_removed_from_visible_text():
    action, visible = _extract_json_action(
        '已选择您的智享金卡进行操作。\ncard_control {"card_tail":"8848","operation":"adjust_limit","value":1000}'
    )
    assert action == {
        "action": "card_control",
        "params": {"card_tail": "8848", "operation": "adjust_limit", "value": 1000},
    }
    assert visible == "已选择您的智享金卡进行操作。"
