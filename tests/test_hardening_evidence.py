from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from core import config, database, policy, workspace
from core.ai_banking import AIBankingAssistant, execute_action
from core.llm_router import LLMRouter
from core.security import PENDING_ACTIONS, _SESSION_STATE, security_gateway
from main import app
from tools import registry


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from core.agent import _TASKS

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "hardening.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    workspace.ensure_workspace_schema()
    policy.ensure_schema()
    _TASKS.clear()
    PENDING_ACTIONS.clear()
    _SESSION_STATE.clear()
    with TestClient(app) as test_client:
        yield test_client
    _TASKS.clear()
    PENDING_ACTIONS.clear()
    _SESSION_STATE.clear()


def _login(client: TestClient, username: str = "linyan") -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username_or_card": username, "password": workspace.DEMO_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"X-Session-Token": response.json()["session_token"]}


def _propose(client: TestClient, headers: dict[str, str], action: dict) -> dict:
    response = client.post("/api/assistant/propose", headers=headers, json={"action": action})
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(client: TestClient, headers: dict[str, str], proposal: dict, *, otp: str | None = None,
             cancel: bool = False):
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


def test_all_proposal_transition_event_types_are_audited(client):
    headers = _login(client)
    cancelled = _propose(client, headers, {
        "action": "cancel_subscription",
        "params": {"service_name": "MuseScore", "amount": 38},
    })
    assert _confirm(client, headers, cancelled, cancel=True).status_code == 200

    executed = _propose(client, headers, {
        "action": "transfer",
        "params": {"recipient": "房东陈姐", "amount": 100, "remark": "审计测试"},
    })
    assert _confirm(client, headers, executed).status_code == 200

    expired = _propose(client, headers, {
        "action": "transfer",
        "params": {"recipient": "房东陈姐", "amount": 120, "remark": "过期测试"},
    })
    connection = database.get_connection()
    try:
        connection.execute(
            "UPDATE action_proposals SET expires_at='2000-01-01T00:00:00.000000+00:00' WHERE proposal_id=?",
            (expired["proposal_id"],),
        )
        connection.commit()
    finally:
        connection.close()
    client.get("/api/state", headers=headers)

    event_types = {
        row["event_type"]
        for row in database.fetch_all("SELECT event_type FROM audit_logs WHERE user_id='U2001'")
    }
    assert {
        "ACTION_PROPOSAL_CREATED",
        "ACTION_PROPOSAL_CONFIRMED",
        "ACTION_PROPOSAL_CANCELLED",
        "ACTION_PROPOSAL_EXPIRED",
        "ACTION_PROPOSAL_EXECUTED",
    } <= event_types


def test_subscription_endpoint_and_dag_registry_both_call_policy_classify(client, monkeypatch):
    headers = _login(client)
    original = policy.classify
    calls: list[dict] = []

    def spy(user_id: str, action: dict):
        calls.append({"user_id": user_id, "action": action})
        return original(user_id, action)

    monkeypatch.setattr(policy, "classify", spy)
    endpoint = client.post(
        "/api/subscriptions/cancel",
        headers=headers,
        json={"subscription_id_or_name": "SUB_LY_02"},
    )
    assert endpoint.status_code == 200, endpoint.text
    assert endpoint.json()["status"] == "pending"
    assert calls and calls[-1]["action"]["action"] == "cancel_subscription"

    calls.clear()
    risk = registry.evaluate_tool_risk(
        "cancel_subscription",
        {"subscription_id_or_name": "SUB_LY_02"},
        "U2001",
    )
    assert risk["require_confirmation"] is True
    assert calls and calls[-1]["action"]["action"] == "cancel_subscription"


def test_receipt_trace_uses_measured_executor_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "trace.sqlite3")
    workspace.ensure_workspace_schema()
    result = execute_action(
        "U2001",
        {"action": "transfer", "params": {
            "recipient": "房东陈姐", "amount": 100, "remark": "追溯测试",
        }},
        level="L2",
    )
    chain = result["receipt"]["traceability_chain"]
    serialized = json.dumps(chain, ensure_ascii=False)
    assert "DAG · SAGA" not in serialized
    transaction_stage = next(item for item in chain if item["stage"] == "事务执行")
    assert "SQLite" in transaction_stage["channel"]
    assert isinstance(transaction_stage.get("duration_ms"), (int, float))
    assert transaction_stage["duration_ms"] >= 0
    assert all("duration_ms" not in item or item["duration_ms"] >= 0 for item in chain)


def test_demo_mode_reset_endpoints_are_forbidden_outside_demo(client, monkeypatch):
    headers = _login(client)
    monkeypatch.setenv("DEMO_MODE", "0")
    assert client.post("/api/security/reset-breaker", headers=headers).status_code == 403
    assert client.post("/api/system/reset-demo", headers=headers).status_code == 403


def test_reset_demo_preserves_audit_chain(client):
    headers = _login(client)
    security_gateway.log_event("U2001", "AUDIT_SURVIVES_RESET", "test", "GREEN", {"probe": True})
    before = database.fetch_all(
        "SELECT COUNT(*) AS count FROM audit_logs WHERE event_type='AUDIT_SURVIVES_RESET'"
    )[0]["count"]
    assert before == 1
    response = client.post("/api/system/reset-demo", headers=headers)
    assert response.status_code == 200, response.text
    after = database.fetch_all(
        "SELECT COUNT(*) AS count FROM audit_logs WHERE event_type='AUDIT_SURVIVES_RESET'"
    )[0]["count"]
    assert after == before
    assert security_gateway.verify_audit_chain()["is_integrity_valid"] is True


def test_session_expiry_column_and_default_ttl(client):
    headers = _login(client)
    row = database.fetch_all("SELECT created_at,expires_at FROM app_sessions")[0]
    created = time.mktime(time.strptime(row["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    expires = time.mktime(time.strptime(row["expires_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    assert round((expires - created) / 3600, 2) == 8.0
    connection = database.get_connection()
    try:
        connection.execute("UPDATE app_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        connection.commit()
    finally:
        connection.close()
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_login_is_locked_after_five_failures_for_same_identity(client):
    statuses = [
        client.post(
            "/api/auth/login",
            json={"username_or_card": "linyan", "password": "wrong-password"},
        ).status_code
        for _ in range(5)
    ]
    assert statuses[:4] == [401, 401, 401, 401]
    assert statuses[4] in {401, 429}
    blocked = client.post(
        "/api/auth/login",
        json={"username_or_card": "linyan", "password": workspace.DEMO_PASSWORD},
    )
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")


def test_login_ip_bucket_locks_across_distinct_unknown_usernames(client):
    for index in range(5):
        response = client.post(
            "/api/auth/login",
            json={"username_or_card": f"unknown_{index}", "password": "wrong-password"},
        )
        assert response.status_code in {401, 429}
    blocked = client.post(
        "/api/auth/login",
        json={"username_or_card": "linyan", "password": workspace.DEMO_PASSWORD},
    )
    assert blocked.status_code == 429


@pytest.mark.parametrize("demo_mode", ["1", "0"])
def test_application_starts_in_both_demo_modes(tmp_path, monkeypatch, demo_mode):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"startup-{demo_mode}.sqlite3")
    monkeypatch.setenv("DEMO_MODE", demo_mode)
    with TestClient(app) as local_client:
        health = local_client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["demo_mode"] == (demo_mode == "1")


def test_startup_log_reports_demo_and_gateway_mode(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "startup-log.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(app) as local_client:
            assert local_client.get("/api/health").status_code == 200
    assert "demo_mode=1" in caplog.text
    assert "gateway_mode=" in caplog.text


def test_env_loader_and_example_enable_demo_mode(tmp_path, monkeypatch):
    loader = getattr(config, "load_env_file", None)
    assert callable(loader), "core.config 必须提供标准库 .env 加载器"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEMO_MODE=1\n"
        "EASIER_BANKING_CLOUD_RATE_LIMIT=7\n"
        "EASIER_BANKING_SESSION_TTL_SECONDS=3600\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("EASIER_BANKING_CLOUD_RATE_LIMIT", raising=False)
    monkeypatch.delenv("EASIER_BANKING_SESSION_TTL_SECONDS", raising=False)
    loader(env_file)
    assert __import__("os").environ["DEMO_MODE"] == "1"
    assert __import__("os").environ["EASIER_BANKING_CLOUD_RATE_LIMIT"] == "7"
    assert (ROOT / ".env.example").read_text(encoding="utf-8").splitlines().count("DEMO_MODE=1") == 1


def test_empty_gateway_secret_never_calls_network(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "offline.sqlite3")
    workspace.ensure_workspace_schema()
    monkeypatch.setattr(config, "GATEWAY_APP_SECRET", "")
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "https://gateway.invalid")
    calls: list[object] = []

    def forbidden_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("未配置网关时不应发起网络请求")

    monkeypatch.setattr("core.ai_banking.httpx.post", forbidden_post)
    assistant = AIBankingAssistant()
    probe = assistant.probe()
    assert probe["status"] in {"OFFLINE", "LOCAL"}
    assert calls == []
    assert LLMRouter().route_channel() == "local_rule_fallback"
    with TestClient(app) as local_client:
        health = local_client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["gateway_mode"] == "offline"
        login = local_client.post(
            "/api/auth/login",
            json={"username_or_card": "linyan", "password": workspace.DEMO_PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"X-Session-Token": login.json()["session_token"]}
        chat = local_client.post(
            "/api/assistant/chat",
            headers=headers,
            json={"message": "查询账户余额"},
        )
        assert chat.status_code == 200, chat.text
        assert chat.json()["route_channel"] == "local_semantic"


def test_cloud_timeout_rate_limit_and_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "guard.sqlite3")
    workspace.ensure_workspace_schema()
    monkeypatch.setattr(config, "GATEWAY_APP_SECRET", "test-secret")
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "https://gateway.invalid")
    monkeypatch.setattr(config, "CLOUD_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(config, "CLOUD_FAILURE_THRESHOLD", 3)
    assert config.CLOUD_TIMEOUT_SECONDS <= 8

    calls: list[object] = []

    def failing_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise httpx.ConnectError("simulated outage")

    monkeypatch.setattr("core.ai_banking.httpx.post", failing_post)
    assistant = AIBankingAssistant(timeout=30)
    assert assistant.timeout <= config.CLOUD_TIMEOUT_SECONDS
    guard = getattr(assistant, "cloud_guard", None)
    assert guard is not None
    guard.reset("U2001")
    for _ in range(3):
        result = assistant.chat("查询账户余额", "U2001", False)
        assert result["route_channel"] == "local_semantic"
        assert "本地安全规则" in result["reply_text"]
    assert len(calls) == 2
    assert result.get("cloud_fallback_reason") == "rate_limited"

    guard.reset("U2001")
    calls.clear()
    monkeypatch.setattr(config, "CLOUD_RATE_LIMIT_PER_MINUTE", 20)
    for _ in range(4):
        result = assistant.chat("查询账户余额", "U2001", False)
    assert len(calls) == 3
    assert result.get("cloud_fallback_reason") == "circuit_open"
    assert "熔断" in result["reply_text"]


def test_cloud_payload_is_minimized_to_card_tail_only(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "privacy.sqlite3")
    workspace.ensure_workspace_schema()
    monkeypatch.setattr(config, "GATEWAY_APP_SECRET", "test-secret")
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "https://gateway.invalid")
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "只读回复"}}], "model": "fake"}

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("core.ai_banking.httpx.post", fake_post)
    assistant = AIBankingAssistant()
    guard = getattr(assistant, "cloud_guard", None)
    assert guard is not None
    guard.reset("U2001")
    workspace.append_chat_message(
        "U2001",
        "user",
        "历史消息：手机号 13800138001，证件号 110101199001011234，完整卡号 6229 8801 2026 8848",
    )
    result = assistant.chat(
        "查询账户余额，我的手机号 13900139002，证件号 11010119900101123X，卡号 6222081234568848",
        "U2001",
        False,
    )
    assert result["route_channel"] == "tencent_gateway"
    body = json.dumps(captured["json"], ensure_ascii=False)
    assert "13800138001" not in body
    assert "13900139002" not in body
    assert "110101199001011234" not in body
    assert "11010119900101123X" not in body
    assert "6222081234568848" not in body
    assert "6229 8801 2026 8848" not in body
    assert "card_no" not in body
    assert "8848" in body


def test_static_offline_mode_never_executes_actions():
    source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "服务暂不可用，操作未执行" in source
    assert "只读缓存" in source

    def function_body(name: str) -> str:
        marker = f"async function {name}("
        start = source.index(marker)
        candidates = [
            index for index in (
                source.find("\n  async function ", start + len(marker)),
                source.find("\n  function ", start + len(marker)),
            ) if index >= 0
        ]
        assert candidates, f"missing function boundary for {name}"
        return source[start:min(candidates)]

    assert "executeLocalAction" not in function_body("confirmProposalAuthorization")
    assert "makeLocalAuthorization" not in function_body("localAgentReply")
    assert "executeLocalAction" not in function_body("localAgentReply")
    for name in (
        "performLocalTransfer",
        "performFundLock",
        "performSubscriptionCancel",
        "executeLocalAction",
    ):
        body = function_body(name)
        assert 'throw new Error("服务暂不可用，操作未执行。")' in body
    local_auth_marker = "function makeLocalAuthorization(action, levelInfo) {"
    assert local_auth_marker in source
    assert source[source.index(local_auth_marker):].split("}", 1)[0].find("throw new Error") >= 0


def test_mode_badges_and_demo_otp_hint_are_visible_in_static_ui():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="runtimeModeBadge"' in index
    assert "云端" in app_js and "离线" in app_js
    assert "演示模式：验证码见提示" in app_js


def test_core_and_static_do_not_embed_legacy_persona_salutations():
    forbidden = ("张叔叔", "张建国", "张大爷")
    offenders = []
    for directory in (ROOT / "core", ROOT / "static"):
        for path in directory.rglob("*"):
            if path.suffix.lower() not in {".py", ".js", ".html", ".css"}:
                continue
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                if value in text:
                    offenders.append(f"{path.relative_to(ROOT)}::{value}")
    assert offenders == []


def test_gateway_secret_is_single_sourced_and_no_legacy_pin_exists():
    secret = config.GATEWAY_APP_SECRET
    assert secret and not secret.startswith("YOUR_")
    occurrences = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix.lower() in {".db", ".sqlite3", ".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if secret in text:
            occurrences.append(path.relative_to(ROOT).as_posix())
        legacy_placeholder = "8" * 6
        assert legacy_placeholder not in text or path.parts[0] not in {"core", "static"}
    assert occurrences == ["core/config.py"]


def test_otp_has_no_plaintext_in_production_response(client, monkeypatch):
    headers = _login(client)
    monkeypatch.setenv("DEMO_MODE", "0")
    proposal = _propose(client, headers, {
        "action": "transfer",
        "params": {"recipient": "周叙白", "amount": 500, "remark": "生产验证码测试"},
    })
    assert proposal["level"] == "L3"
    assert proposal["demo_hint"] is None
    serialized = json.dumps(proposal, ensure_ascii=False)
    assert '"otp":' not in serialized
