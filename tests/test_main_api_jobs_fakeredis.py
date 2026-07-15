"""Authenticated ``/jobs`` flows with Redis mocked via fakeredis."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import fakeredis
import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch, main_module: Any) -> fakeredis.FakeStrictRedis:
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(main_module, "redis_client", lambda: r)
    return r


@pytest.fixture
def authed_client(client: TestClient) -> TestClient:
    r = client.post("/api/auth/login", json={"password": "test-dashboard-secret"})
    assert r.status_code == 200
    return client


def test_post_jobs_simulated_enqueues_and_writes_hash(
    authed_client: TestClient,
    fake_redis: fakeredis.FakeStrictRedis,
    monkeypatch: pytest.MonkeyPatch,
    main_module: Any,
) -> None:
    monkeypatch.setattr(main_module, "connect_proxmox", lambda c: MagicMock())
    monkeypatch.setattr(main_module, "qemu_vmids_in_use_cluster", lambda prox: {999})
    monkeypatch.setattr(main_module, "qemu_vmids_in_use_on_node", lambda prox, node: {999})

    resp = authed_client.post(
        "/jobs",
        json={
            "vm_name": "ApiGuest",
            "proxmox_vmid": 3501,
            "proxmox_node": "pve",
            "proxmox_storage": "local-lvm",
            "simulated_import": True,
            "preimported_disk_mode": False,
            "take_snapshot": True,
            "winrm_user": "Administrator",
            "winrm_password": "",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vm_name"] == "ApiGuest"
    assert body["state"] == "PENDING"
    assert body["proxmox_vmid"] == 3501
    assert "simulated" in (body.get("vmdk_path_on_node") or "").lower() or body.get("simulated_import") is True

    cfg = main_module.load_config()
    q = cfg["redis"]["queue_key"]
    assert fake_redis.llen(q) == 1
    queued = fake_redis.lpop(q)
    assert queued == body["job_id"]
    key = main_module.job_key(cfg, body["job_id"])
    assert fake_redis.hget(key, "vm_name") == "ApiGuest"


def test_post_jobs_409_when_proxmox_vmid_already_in_use(
    authed_client: TestClient,
    fake_redis: fakeredis.FakeStrictRedis,
    monkeypatch: pytest.MonkeyPatch,
    main_module: Any,
) -> None:
    monkeypatch.setattr(main_module, "connect_proxmox", lambda c: MagicMock())
    monkeypatch.setattr(main_module, "qemu_vmids_in_use_cluster", lambda prox: {3501, 3502})
    monkeypatch.setattr(main_module, "qemu_vmids_in_use_on_node", lambda prox, node: {3501, 3502})

    resp = authed_client.post(
        "/jobs",
        json={
            "vm_name": "InUse",
            "proxmox_vmid": 3501,
            "proxmox_node": "pve",
            "proxmox_storage": "local-lvm",
            "simulated_import": True,
            "preimported_disk_mode": False,
            "take_snapshot": True,
        },
    )
    assert resp.status_code == 409
    assert "already in use" in resp.json()["detail"].lower()
    assert "3503" in resp.json()["detail"] or "proxmox_vmid=" in resp.json()["detail"]


def test_migrate_selected_409_when_guest_has_active_job(
    authed_client: TestClient,
    fake_redis: fakeredis.FakeStrictRedis,
    monkeypatch: pytest.MonkeyPatch,
    main_module: Any,
) -> None:
    cfg = main_module.load_config()
    jid = str(uuid.uuid4())
    key = main_module.job_key(cfg, jid)
    fake_redis.hset(
        key,
        mapping={
            "job_id": jid,
            "vm_name": "BlockedGuest",
            "state": "IMPORTING",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
            "proxmox_vmid": "3501",
            "proxmox_node": "pve",
            "vmdk_path_on_node": "simulated:clone",
            "winrm_user": "Administrator",
            "winrm_password": "",
            "guest_ip": "",
            "error": "",
            "last_failed_stage": "",
        },
    )

    monkeypatch.setattr(
        main_module.ui_module,
        "enumerate_guests_from_pve_esxi",
        lambda c, n: [{"name": "BlockedGuest", "esxi_import_uri": "", "esxi_storage_ids": []}],
    )
    monkeypatch.setattr(main_module, "connect_proxmox", lambda c: MagicMock())
    monkeypatch.setattr(main_module, "qemu_vmids_in_use_cluster", lambda prox: set())
    monkeypatch.setattr(main_module, "qemu_vmids_in_use_on_node", lambda prox, node: set())

    resp = authed_client.post(
        "/jobs/migrate-selected",
        json={
            "vm_names": ["BlockedGuest"],
            "proxmox_vmid_start": 3502,
            "proxmox_storage": "local-lvm",
            "memory_mb": 8192,
            "cores": 4,
            "simulated_import": True,
        },
    )
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"].lower()


def test_migration_defaults_contains_expected_keys(
    authed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    main_module: Any,
) -> None:
    monkeypatch.setattr(main_module, "connect_proxmox", lambda c: MagicMock())
    monkeypatch.setattr(main_module, "qemu_vmids_in_use_cluster", lambda prox: {3500, 3501})
    r = authed_client.get("/migration-defaults")
    assert r.status_code == 200
    data = r.json()
    assert "storage" in data
    assert "default_node" in data
    assert "net_mtu" in data
    assert "simulated_import_default" in data


def test_enqueue_efi_redo_pending_enqueues_only_tagged_vms(
    authed_client: TestClient,
    fake_redis: fakeredis.FakeStrictRedis,
    monkeypatch: pytest.MonkeyPatch,
    main_module: Any,
) -> None:
    class _FakeResources:
        def get(self, **kwargs: Any) -> list[dict[str, Any]]:
            assert kwargs.get("type") == "vm"
            return [
                {"type": "qemu", "vmid": 3601, "node": "pve-a", "name": "Redo-A", "tags": "migrated;efi-redo-pending"},
                {"type": "qemu", "vmid": 3602, "node": "pve-a", "name": "Skip-A", "tags": "migrated"},
                {"type": "qemu", "vmid": 3603, "node": "pve-b", "name": "Redo-B", "tags": "efi-redo-pending;migrated"},
            ]

    class _FakeCluster:
        resources = _FakeResources()

    class _FakeProxmox:
        cluster = _FakeCluster()

    monkeypatch.setattr(main_module, "connect_proxmox", lambda c: _FakeProxmox())

    resp = authed_client.post(
        "/jobs/efi-redo-pending",
        json={"run_guest_script": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued"] == 2
    assert body["matched"] == 2
    assert len(body["job_ids"]) == 2

    cfg = main_module.load_config()
    q = cfg["redis"]["queue_key"]
    assert fake_redis.llen(q) == 2
    jid = fake_redis.lindex(q, 0)
    key = main_module.job_key(cfg, jid)
    assert fake_redis.hget(key, "job_kind") == "efi_redo"
    assert fake_redis.hget(key, "state") == "PENDING"


def test_enqueue_efi_redo_pending_subset_rejects_missing_required_tags(
    authed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    main_module: Any,
) -> None:
    class _FakeResources:
        def get(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"type": "qemu", "vmid": 3701, "node": "pve", "name": "OnlyMigrated", "tags": "migrated"}]

    class _FakeCluster:
        resources = _FakeResources()

    class _FakeProxmox:
        cluster = _FakeCluster()

    monkeypatch.setattr(main_module, "connect_proxmox", lambda c: _FakeProxmox())
    resp = authed_client.post(
        "/jobs/efi-redo-pending",
        json={"vm_names": ["OnlyMigrated"]},
    )
    assert resp.status_code == 400
    assert "required tags" in resp.json()["detail"].lower()
