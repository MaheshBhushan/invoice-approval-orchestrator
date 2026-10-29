import pytest
from api.main import app
from fastapi.testclient import TestClient


def test_disabled_adapter_fails_closed(monkeypatch):
    monkeypatch.delenv("APPROVAL_API_TOKEN", raising=False)
    with TestClient(app) as client:
        assert client.get("/approvals/pending").status_code == 503


def test_invalid_token_cannot_discover_reviews(monkeypatch):
    monkeypatch.setenv("APPROVAL_API_TOKEN", "synthetic-unit-test-token")
    with TestClient(app) as client:
        assert client.get("/approvals/pending").status_code == 401
        assert client.get("/approvals/pending", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_non_ascii_token_is_rejected_without_server_error(monkeypatch):
    monkeypatch.setenv("APPROVAL_API_TOKEN", "synthetic-unit-test-token")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/approvals/pending", headers=[(b"Authorization", b"Bearer \xff")]
        )
        assert response.status_code == 401


@pytest.mark.parametrize("reviewer_id", ["", "   ", "\t\n"])
def test_blank_reviewer_cannot_record_a_decision(monkeypatch, reviewer_id):
    monkeypatch.setenv("APPROVAL_API_TOKEN", "synthetic-unit-test-token")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/approvals/00000000-0000-0000-0000-000000000001/response",
            headers={"Authorization": "Bearer synthetic-unit-test-token"},
            json={"task_key": "1", "decision": "approved", "reviewer_id": reviewer_id},
        )
        assert response.status_code == 422
