from fastapi.testclient import TestClient

from core import database, workspace
from main import app


def test_authenticated_handover_persists_context_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "handover.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "1")
    workspace.ensure_workspace_schema()
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username_or_card": "linyan", "password": workspace.DEMO_PASSWORD},
        )
        assert login.status_code == 200, login.text
        response = client.post(
            "/api/support/handover",
            headers={"X-Session-Token": login.json()["session_token"]},
            json={"reason": "需要人工核对异常交易"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "success"
        assert payload["ticket_id"].startswith("CS-")
        stored = database.fetch_all(
            "SELECT * FROM support_tickets WHERE ticket_id=?", (payload["ticket_id"],)
        )
        assert len(stored) == 1
        assert stored[0]["user_id"] == "U2001"
        assert stored[0]["status"] == "OPEN"
        assert "recent_messages" in stored[0]["snapshot_json"]
    logs = database.fetch_all(
        "SELECT event_type,status FROM audit_logs WHERE event_type='HUMAN_HANDOVER_CREATED'"
    )
    assert logs and logs[-1]["status"] == "OPEN"
