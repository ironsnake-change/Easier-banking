from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core import database, policy, workspace
from core.security import security_gateway
from main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "confirm-gate.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    workspace.ensure_workspace_schema()
    policy.ensure_schema()
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, username: str = "linyan") -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username_or_card": username, "password": "Demo@2026"})
    assert response.status_code == 200, response.text
    return {"X-Session-Token": response.json()["session_token"]}


def _propose(client: TestClient, headers: dict[str, str], action: dict) -> dict:
    response = client.post("/api/assistant/propose", headers=headers, json={"action": action})
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(client: TestClient, headers: dict[str, str], proposal: dict, otp: str | None = None, cancel: bool = False):
    return client.post(
        "/api/assistant/confirm",
        headers=headers,
        json={
            "proposal_id": proposal["proposal_id"],
            "confirm_token": proposal["confirm_token"],
            "otp": otp,
            "cancel": cancel,
        },
    )


def test_deprecated_execution_endpoints_reject_client_confirmation(client):
    headers = _login(client)
    action = {"action": "transfer", "params": {"recipient": "房东陈姐", "amount": 100, "remark": "test"}}
    for path in ("/api/assistant/action", "/api/assistant/execute"):
        response = client.post(path, headers=headers, json={"action": action, "confirmed": True, "temporary_limit_raise": True})
        assert response.status_code == 410
        assert "propose" in response.json()["detail"]
    rejected = client.post(
        "/api/assistant/propose",
        headers=headers,
        json={"action": action, "confirmed": True, "temporary_limit_raise": True},
    )
    assert rejected.status_code == 422
    blocked_preference = client.post(
        "/api/preferences",
        headers=headers,
        json={"transfer_threshold": 5000},
    )
    assert blocked_preference.status_code == 409


def test_l2_confirm_is_claimed_once_and_replay_is_rejected(client):
    headers = _login(client)
    before = client.get("/api/state", headers=headers).json()["user_profile"]["available_balance"]
    proposal = _propose(client, headers, {
        "action": "transfer",
        "params": {"recipient": "房东陈姐", "amount": 300, "remark": "房租"},
    })
    assert proposal["level"] == "L2"
    assert proposal["requires"] == ["confirmation"]
    assert proposal["demo_hint"] is None

    first = _confirm(client, headers, proposal)
    second = _confirm(client, headers, proposal)
    assert first.status_code == 200 and first.json()["status"] == "EXECUTED"
    assert second.status_code == 409
    after = client.get("/api/state", headers=headers).json()["user_profile"]["available_balance"]
    assert round(before - after, 2) == 300.0


def test_chat_creates_proposal_and_never_executes_l1_directly(client, monkeypatch):
    import main

    headers = _login(client)
    action = {"action": "card_control", "params": {"card_tail": "8848", "operation": "overseas_lock", "value": True}}
    monkeypatch.setattr(
        main.ai_banking_assistant,
        "chat",
        lambda message, user_id, elder_mode: {
            "status": "SUCCESS",
            "reply_text": "请确认开启境外交易锁。",
            "route_channel": "test",
            "latency_ms": 1,
            "bank_action": action,
            "authorization_level": "L1",
            "demo_hint": None,
        },
    )
    response = client.post("/api/assistant/chat", headers=headers, json={"message": "帮我开启 8848 的境外锁"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["authorization"]["level"] == "L1"
    assert payload["authorization"]["confirm_token"]
    assert database.fetch_all(
        "SELECT overseas_lock FROM cards WHERE user_id='U2001' AND card_no LIKE '%8848'"
    )[0]["overseas_lock"] == 0

    stored = database.fetch_all(
        "SELECT receipt_json FROM chat_messages WHERE message_id=?", (payload["message_id"],)
    )[0]["receipt_json"]
    assert "confirm_token" not in stored
    confirmed = client.post(
        "/api/assistant/confirm",
        headers=headers,
        json={
            "proposal_id": payload["authorization"]["proposal_id"],
            "confirm_token": payload["authorization"]["confirm_token"],
            "cancel": False,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert database.fetch_all(
        "SELECT overseas_lock FROM cards WHERE user_id='U2001' AND card_no LIKE '%8848'"
    )[0]["overseas_lock"] == 1


def test_concurrent_confirm_executes_exactly_once(client):
    headers = _login(client)
    before = client.get("/api/state", headers=headers).json()["user_profile"]["available_balance"]
    proposal = _propose(client, headers, {
        "action": "transfer",
        "params": {"recipient": "房东陈姐", "amount": 200, "remark": "并发测试"},
    })

    def invoke():
        with TestClient(app) as worker:
            return _confirm(worker, headers, proposal).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _: invoke(), range(2)))
    assert statuses == [200, 409]
    after = client.get("/api/state", headers=headers).json()["user_profile"]["available_balance"]
    assert round(before - after, 2) == 200.0


def test_owner_token_params_and_expiry_are_enforced(client):
    owner_headers = _login(client, "linyan")
    other_headers = _login(client, "zhangjg")
    action = {"action": "transfer", "params": {"recipient": "房东陈姐", "amount": 100, "remark": "边界测试"}}

    owner_proposal = _propose(client, owner_headers, action)
    wrong_owner = _confirm(client, other_headers, owner_proposal)
    assert wrong_owner.status_code == 403

    wrong_token = client.post(
        "/api/assistant/confirm",
        headers=owner_headers,
        json={"proposal_id": owner_proposal["proposal_id"], "confirm_token": "x" * 40},
    )
    assert wrong_token.status_code == 403

    tampered = _propose(client, owner_headers, action)
    connection = database.get_connection()
    try:
        connection.execute(
            "UPDATE action_proposals SET action_json=? WHERE proposal_id=?",
            ('{"action":"transfer","params":{"recipient":"房东陈姐","amount":1,"remark":"tampered"}}',
             tampered["proposal_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    assert _confirm(client, owner_headers, tampered).status_code == 409

    expired = _propose(client, owner_headers, action)
    connection = database.get_connection()
    try:
        connection.execute(
            "UPDATE action_proposals SET expires_at=? WHERE proposal_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="microseconds"),
             expired["proposal_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    assert _confirm(client, owner_headers, expired).status_code == 409


def test_l3_otp_is_server_issued_counted_and_required(client):
    headers = _login(client)
    action = {
        "action": "transfer",
        "params": {"recipient": "周叙白", "amount": 500, "remark": "首次转账"},
    }
    proposal = _propose(client, headers, action)
    assert proposal["level"] == "L3"
    assert proposal["requires"] == ["confirmation", "otp"]
    assert proposal["demo_hint"]["otp"].isdigit()
    stored = database.fetch_all(
        "SELECT confirm_token_hash,otp_hash FROM action_proposals WHERE proposal_id=?",
        (proposal["proposal_id"],),
    )[0]
    assert stored["confirm_token_hash"] != proposal["confirm_token"]
    assert stored["otp_hash"] != proposal["demo_hint"]["otp"]

    missing = _confirm(client, headers, proposal)
    assert missing.status_code == 428
    wrong_value = "000000" if proposal["demo_hint"]["otp"] != "000000" else "111111"
    assert _confirm(client, headers, proposal, wrong_value).status_code == 401
    assert _confirm(client, headers, proposal, wrong_value).status_code == 401
    assert _confirm(client, headers, proposal, proposal["demo_hint"]["otp"]).status_code == 409

    fresh = _propose(client, headers, action)
    success = _confirm(client, headers, fresh, fresh["demo_hint"]["otp"])
    assert success.status_code == 200, success.text
    assert success.json()["status"] == "EXECUTED"


def test_l3_demo_hint_is_absent_when_demo_mode_is_disabled(client, monkeypatch):
    headers = _login(client)
    monkeypatch.setenv("DEMO_MODE", "0")
    proposal = _propose(client, headers, {
        "action": "transfer",
        "params": {"recipient": "周叙白", "amount": 500, "remark": "首次转账"},
    })
    assert proposal["level"] == "L3"
    assert proposal["demo_hint"] is None


def test_temporary_limit_override_expires_without_mutating_base_limit(client):
    headers = _login(client)
    state_before = client.get("/api/state", headers=headers).json()
    base_limit = state_before["preferences"]["transfer_threshold"]
    proposal = _propose(client, headers, {
        "action": "transfer",
        "params": {"recipient": "周叙白", "amount": 2000, "remark": "大额首次转账"},
    })
    assert proposal["level"] == "L3"
    response = _confirm(client, headers, proposal, proposal["demo_hint"]["otp"])
    assert response.status_code == 200, response.text
    assert policy.effective_daily_limit("U2001") > base_limit
    assert response.json()["live_state"]["user_profile"]["daily_transfer_limit"] == policy.effective_daily_limit("U2001")
    assert database.fetch_all("SELECT transfer_threshold FROM user_preferences WHERE user_id='U2001'")[0]["transfer_threshold"] == base_limit

    connection = database.get_connection()
    try:
        connection.execute(
            "UPDATE limit_overrides SET expires_at=? WHERE user_id='U2001'",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="microseconds"),),
        )
        connection.commit()
    finally:
        connection.close()
    assert policy.effective_daily_limit("U2001") == base_limit
    policy.pruned_overrides("U2001")
    assert client.get("/api/state", headers=headers).json()["user_profile"]["daily_transfer_limit"] == base_limit


def test_policy_classifies_l1_protection_and_l3_risk_expansion(client):
    freeze = {"action": "card_control", "params": {"card_tail": "8848", "operation": "freeze"}}
    overseas_on = {"action": "card_control", "params": {"card_tail": "8848", "operation": "overseas_lock", "value": True}}
    overseas_off = {"action": "card_control", "params": {"card_tail": "8848", "operation": "overseas_lock", "value": False}}
    assert policy.classify("U2001", freeze) == {"level": "L1", "requires": ["confirmation"], "reasons": ["保护性安全操作，一键确认后即时生效"]}
    assert policy.classify("U2001", overseas_on)["level"] == "L1"
    assert policy.classify("U2001", overseas_off)["level"] == "L3"


def test_legacy_gateway_uses_random_server_issued_otp(client):
    _login(client)
    gate = security_gateway.intercept_tool_call(
        "execute_transfer",
        {"payee_id_or_account": "CONTACT_LY_01", "amount": 2000, "remark": "legacy-red-test"},
        "U2001",
    )
    assert gate["risk_level"] == "RED"
    assert gate["demo_hint"]["otp"].isdigit()
    otp = gate["demo_hint"]["otp"]
    wrong = "000000" if otp != "000000" else "111111"
    rejected = security_gateway.approve_pending_action(
        gate["pending_action_id"], "CONFIRM", wrong, user_id="U2001"
    )
    assert rejected["status"] == "blocked"
    approved = security_gateway.approve_pending_action(
        gate["pending_action_id"], "CONFIRM", otp, user_id="U2001"
    )
    assert approved["status"] == "success"


def test_proposal_lifecycle_is_audited(client):
    headers = _login(client)
    proposal = _propose(client, headers, {
        "action": "cancel_subscription",
        "params": {"service_name": "MuseScore", "amount": 38},
    })
    cancelled = _confirm(client, headers, proposal, cancel=True)
    assert cancelled.status_code == 200
    expired = _propose(client, headers, {
        "action": "cancel_subscription",
        "params": {"service_name": "Netflix", "amount": 68},
    })
    connection = database.get_connection()
    try:
        connection.execute(
            "UPDATE action_proposals SET expires_at=? WHERE proposal_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="microseconds"),
             expired["proposal_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    client.get("/api/state", headers=headers)
    event_types = {
        row["event_type"]
        for row in database.fetch_all(
            "SELECT event_type FROM audit_logs WHERE user_id='U2001'"
        )
    }
    assert "ACTION_PROPOSAL_CREATED" in event_types
    assert "ACTION_PROPOSAL_CANCELLED" in event_types
    assert "ACTION_PROPOSAL_EXPIRED" in event_types
