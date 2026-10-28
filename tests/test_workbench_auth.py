from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from core import database
    from core.agent import _TASKS
    from core.security import PENDING_ACTIONS, _SESSION_STATE
    from main import app

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "workbench-test.sqlite3")
    _TASKS.clear()
    PENDING_ACTIONS.clear()
    _SESSION_STATE.clear()
    with TestClient(app) as test_client:
        yield test_client
    _TASKS.clear()
    PENDING_ACTIONS.clear()
    _SESSION_STATE.clear()


def _login(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username_or_card": username, "password": "Demo@2026"},
    )
    assert response.status_code == 200, response.text
    return response.json()["session_token"]


def test_demo_logins_are_isolated_and_forged_user_id_is_ignored(client: TestClient):
    assert client.get("/api/state?user_id=U2001").status_code == 401

    linyan_token = _login(client, "linyan")
    zhang_token = _login(client, "zhangjg")
    linyan = client.get("/api/state?user_id=U1001", headers={"X-Session-Token": linyan_token}).json()
    zhang = client.get("/api/state?user_id=U2001", headers={"X-Session-Token": zhang_token}).json()

    assert linyan["user_profile"]["user_id"] == "U2001"
    assert linyan["user_profile"]["name"] == "林砚"
    assert linyan["user_profile"]["display_card_tail"] == "8848"
    assert "password_salt" not in linyan["user_profile"]
    assert "password_hash" not in linyan["user_profile"]
    assert zhang["user_profile"]["user_id"] == "U1001"
    assert zhang["user_profile"]["name"] == "张建国"
    assert zhang["user_profile"]["elderly_mode"] is True
    assert all(row["user_id"] == "U2001" for row in linyan["transactions"])
    assert all(row["user_id"] == "U1001" for row in zhang["transactions"])


def test_registration_preferences_chat_subscription_and_logout(client: TestClient):
    registered = client.post(
        "/api/auth/register",
        json={"name": "测试长者", "username": "testelder", "password": "SafePass2026", "account_type": "elder"},
    )
    assert registered.status_code == 200, registered.text
    token = registered.json()["session_token"]
    headers = {"X-Session-Token": token}
    initial = registered.json()["live_state"]
    user_id = initial["user_profile"]["user_id"]
    assert initial["user_profile"]["elderly_mode"] is True
    assert len(initial["transactions"]) == 12
    assert len(initial["subscriptions"]) == 1
    assert len(initial["pockets"]) == 2

    changed = client.post(
        "/api/preferences",
        headers=headers,
        json={"elder_mode": False, "engine_mode": "local", "chart_type": "bar"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["live_state"]["user_profile"]["elderly_mode"] is False
    assert len(changed.json()["live_state"]["chat_messages"]) == 0

    chat = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "分析本月账单", "user_id": "U1001"},
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["live_state"]["user_profile"]["user_id"] == user_id
    assert len(chat.json()["live_state"]["chat_messages"]) == 2
    assert client.get("/api/chat/history", headers=headers).json()["messages"]

    pending = client.post(
        "/api/subscriptions/cancel",
        headers=headers,
        json={"subscription_id_or_name": initial["subscriptions"][0]["sub_id"]},
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"
    assert pending.json()["risk_level"] == "YELLOW"
    assert client.get("/api/state", headers=headers).json()["subscriptions"][0]["status"] == "ACTIVE"

    cancelled = client.post(
        "/api/subscriptions/cancel/confirm",
        headers=headers,
        json={"pending_action_id": pending.json()["pending_action_id"], "confirm": True},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "success"
    assert cancelled.json()["live_state"]["subscriptions"][0]["status"] == "CANCELLED"

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_invalid_credentials_and_session_are_rejected(client: TestClient):
    assert client.post(
        "/api/auth/login", json={"username_or_card": "linyan", "password": "incorrect"}
    ).status_code == 401
    assert client.get("/api/auth/me", headers={"X-Session-Token": "not-a-session"}).status_code == 401
