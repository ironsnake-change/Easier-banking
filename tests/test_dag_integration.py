import pytest
from fastapi.testclient import TestClient

from core import database, workspace
from core.security import PENDING_ACTIONS, security_gateway
from main import app


@pytest.fixture(autouse=True)
def reset_shared_gateway_state():
    PENDING_ACTIONS.clear()
    security_gateway.reset_circuit_breaker("U2001")
    yield
    PENDING_ACTIONS.clear()
    security_gateway.reset_circuit_breaker("U2001")


def test_assistant_chat_routes_compound_request_into_real_dag(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dag-integration.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    workspace.ensure_workspace_schema()
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username_or_card": "linyan", "password": workspace.DEMO_PASSWORD},
        )
        assert login.status_code == 200, login.text
        response = client.post(
            "/api/assistant/chat",
            headers={"X-Session-Token": login.json()["session_token"]},
            json={
                "message": "下个月十五号是我的爱人的生日，先锁一千元，再预定鲜花跟蛋糕",
                "force_local_fallback": True,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["dag"]["dag_id"].startswith("DAG-")
        assert len(payload["dag"]["nodes"]) >= 3
        assert payload["pending_interaction"]["dag_id"] == payload["dag"]["dag_id"]
        assert payload["pending_interaction"]["risk_level"] in {"YELLOW", "RED"}
        assert [node["risk_level"] for node in payload["dag"]["nodes"]] == ["GREEN", "YELLOW", "YELLOW"]
        assert not any(word in payload["reply_text"] for word in ("card_control", "<bank_action>", '"card_tail"'))

        headers = {"X-Session-Token": login.json()["session_token"]}
        current = payload
        confirmations = 0
        while current.get("pending_interaction") and confirmations < 5:
            pending = current["pending_interaction"]
            resumed = client.post(
                "/api/dag/resume",
                headers=headers,
                json={
                    "dag_id": pending["dag_id"],
                    "pending_action_id": pending["pending_action_id"],
                    "action": "approve",
                    "sms_code": (pending.get("demo_hint") or {}).get("otp"),
                },
            )
            assert resumed.status_code == 200, resumed.text
            current = resumed.json()
            confirmations += 1
        assert confirmations >= 1
        assert current["dag"]["overall_status"] == "COMPLETED"
        assert all(node["status"] == "COMPLETED" for node in current["dag"]["nodes"])


def test_cancelling_second_dag_node_rolls_back_completed_node(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dag-rollback.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    workspace.ensure_workspace_schema()
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username_or_card": "linyan", "password": workspace.DEMO_PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"X-Session-Token": login.json()["session_token"]}
        started = client.post(
            "/api/assistant/chat",
            headers=headers,
            json={
                "message": "帮我锁定1000元旅游基金并预订机票订单",
                "force_local_fallback": True,
            },
        ).json()
        first = started["pending_interaction"]
        resumed = client.post(
            "/api/dag/resume",
            headers=headers,
            json={
                "dag_id": first["dag_id"],
                "pending_action_id": first["pending_action_id"],
                "action": "approve",
            },
        )
        assert resumed.status_code == 200, resumed.text
        paused = resumed.json()
        assert paused.get("pending_interaction")
        rolled_back = client.post(
            "/api/dag/rollback",
            headers=headers,
            json={"dag_id": first["dag_id"], "reason": "用户取消后续订单"},
        )
        assert rolled_back.status_code == 200, rolled_back.text
        payload = rolled_back.json()
        assert payload["dag"]["overall_status"] == "ROLLED_BACK"
        assert any(node["status"] == "ROLLED_BACK" for node in payload["dag"]["nodes"])


def test_dag_red_node_is_blocked_after_three_bad_otp_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dag-otp.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    workspace.ensure_workspace_schema()
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username_or_card": "linyan", "password": workspace.DEMO_PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"X-Session-Token": login.json()["session_token"]}
        started = client.post(
            "/api/chat",
            headers=headers,
            json={"message": "给房东陈姐转2000元", "force_local_fallback": True},
        ).json()
        pending = started["pending_interaction"]
        assert pending["risk_level"] == "RED"
        payload = None
        for _ in range(3):
            response = client.post(
                "/api/dag/resume",
                headers=headers,
                json={
                    "dag_id": pending["dag_id"],
                    "pending_action_id": pending["pending_action_id"],
                    "action": "approve",
                    "sms_code": "000000" if (pending.get("demo_hint") or {}).get("otp") != "000000" else "111111",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
        assert payload["status"] == "BLOCKED_BY_SECURITY"
        assert payload["pending_interaction"] is None
        assert payload["dag"]["overall_status"] == "BLOCKED_BY_SECURITY"
