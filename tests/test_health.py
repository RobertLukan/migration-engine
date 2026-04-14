from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient


def test_health_ok(monkeypatch: pytest.MonkeyPatch, main_module: Any, ui_module: Any) -> None:
    cfg = {"redis": {"url": "redis://unused:6379/0", "job_key_prefix": "x:"}}

    monkeypatch.setattr(main_module, "load_config", lambda: cfg)

    fake_r = MagicMock()
    fake_r.ping.return_value = True
    monkeypatch.setattr(main_module, "redis_client", lambda: fake_r)
    monkeypatch.setattr(ui_module, "health_proxmox_component", lambda c: {"ok": True, "detail": "mock"})
    monkeypatch.setattr(
        ui_module,
        "health_vmware_component",
        lambda c: {"ok": True, "detail": "not configured (optional)", "optional": True},
    )

    client = TestClient(main_module.app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["components"]["config"]["ok"] is True
    assert body["components"]["redis"]["ok"] is True
    assert body["components"]["proxmox"]["ok"] is True


def test_health_degraded_when_redis_fails(monkeypatch: pytest.MonkeyPatch, main_module: Any, ui_module: Any) -> None:
    monkeypatch.setattr(main_module, "load_config", lambda: {"redis": {"url": "redis://x", "job_key_prefix": "p:"}})

    def boom() -> MagicMock:
        r = MagicMock()

        def ping() -> None:
            raise ConnectionError("redis down")

        r.ping = ping
        return r

    monkeypatch.setattr(main_module, "redis_client", boom)
    monkeypatch.setattr(ui_module, "health_proxmox_component", lambda c: {"ok": True, "detail": "mock"})
    monkeypatch.setattr(
        ui_module,
        "health_vmware_component",
        lambda c: {"ok": True, "detail": "optional", "optional": True},
    )

    client = TestClient(main_module.app)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["components"]["redis"]["ok"] is False


def test_health_vmware_optional_failure_still_200(monkeypatch: pytest.MonkeyPatch, main_module: Any, ui_module: Any) -> None:
    monkeypatch.setattr(main_module, "load_config", lambda: {"redis": {"url": "redis://x", "job_key_prefix": "p:"}})
    fake_r = MagicMock()
    fake_r.ping.return_value = True
    monkeypatch.setattr(main_module, "redis_client", lambda: fake_r)
    monkeypatch.setattr(ui_module, "health_proxmox_component", lambda c: {"ok": True, "detail": "mock"})
    monkeypatch.setattr(
        ui_module,
        "health_vmware_component",
        lambda c: {"ok": False, "detail": "vCenter down", "optional": True},
    )

    client = TestClient(main_module.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["components"]["vmware"]["ok"] is False
