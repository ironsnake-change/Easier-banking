import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from core import database
from main import app


def test_business_flow_isolated():
    with tempfile.TemporaryDirectory() as temp:
        database.DB_PATH = Path(temp) / "business.sqlite3"
        with TestClient(app) as client:
            login = client.post("/api/auth/login", json={"username_or_card": "zhangjg", "password": "Demo@2026"})
            assert login.status_code == 200, login.text
            token = login.json()["session_token"]
            headers = {"X-Session-Token": token}
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
            print(
                {
                    "login": "ok",
                    "balance_before": before,
                    "yellow_confirmation": "ok",
                    "transfer": "ok",
                    "receipt_id": payload["receiptData"]["receipt_id"],
                    "balance_after": after,
                    "route": payload.get("route_channel"),
                }
            )
