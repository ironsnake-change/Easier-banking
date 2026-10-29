import pytest
from fastapi.testclient import TestClient

from core import database, policy, workspace
from main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "business-flow.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    workspace.ensure_workspace_schema()
    with TestClient(app) as test_client:
        yield test_client


def test_elder_transfer_requires_confirmation_and_updates_ledger_once(client):
    login = client.post(
        "/api/auth/login",
        json={"username_or_card": "zhangjg", "password": workspace.DEMO_PASSWORD},
    )
    assert login.status_code == 200, login.text
    headers = {"X-Session-Token": login.json()["session_token"]}
    state = client.get("/api/state", headers=headers)
    assert state.status_code == 200, state.text
    before = state.json()["user_profile"]["available_balance"]

    chat = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "我想给大孙子转500元", "force_local_fallback": True},
    )
    assert chat.status_code == 200, chat.text
    pending = chat.json().get("pending_interaction")
    assert pending and pending["risk_level"] == "YELLOW", chat.text
    assert client.get("/api/state", headers=headers).json()["user_profile"]["available_balance"] == before

    resume = client.post(
        "/api/dag/resume",
        headers=headers,
        json={
            "dag_id": pending["dag_id"],
            "pending_action_id": pending["pending_action_id"],
            "action": "approve",
        },
    )
    assert resume.status_code == 200, resume.text
    payload = resume.json()
    assert payload["status"] == "COMPLETED", resume.text
    assert payload.get("receiptData"), resume.text
    after = payload["live_state"]["user_profile"]["available_balance"]
    assert round(before - after, 2) == 500.0, (before, after, payload)
    assert database.fetch_all(
        "SELECT COUNT(*) AS count FROM transactions WHERE user_id='U1001' AND merchant LIKE '%许小宝%'"
    )[0]["count"] == 1


def test_elder_daily_transfer_limit_uses_universal_1000_threshold(client):
    login = client.post(
        "/api/auth/login",
        json={"username_or_card": "zhangjg", "password": workspace.DEMO_PASSWORD},
    )
    assert login.status_code == 200, login.text

    preference = database.fetch_all(
        "SELECT transfer_threshold FROM user_preferences WHERE user_id='U1001'"
    )[0]
    assert preference["transfer_threshold"] == 1000.0
    assert policy.effective_daily_limit("U1001") == 1000.0
    classified = policy.classify(
        "U1001",
        {"action": "transfer", "params": {"recipient": "大孙子", "amount": 700}},
    )
    assert classified["level"] == "L3"


def test_wealth_purchase_and_large_redemption_require_l3(client):
    purchase = policy.classify(
        "U2001",
        {"action": "subscribe_wealth", "params": {"product_name": "活期+日日享", "amount": 1000}},
    )
    large_redemption = policy.classify(
        "U2001",
        {"action": "redeem_wealth", "params": {"product_name": "活期+日日享", "amount": 1000.01}},
    )
    assert purchase["level"] == "L3"
    assert "otp" in purchase["requires"]
    assert large_redemption["level"] == "L3"
