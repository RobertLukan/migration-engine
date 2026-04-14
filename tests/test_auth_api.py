from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def test_jobs_requires_session(client: TestClient) -> None:
    r = client.get("/jobs")
    assert r.status_code == 401


def test_login_and_credentials_roundtrip(client: TestClient) -> None:
    bad = client.post("/api/auth/login", json={"password": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/api/auth/login", json={"password": "test-dashboard-secret"})
    assert ok.status_code == 200

    creds = client.get("/api/ui/credentials")
    assert creds.status_code == 200
    data = creds.json()
    assert "vmware" in data and "proxmox" in data
    assert data["proxmox"].get("host") == "127.0.0.1"
