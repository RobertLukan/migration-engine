"""Redis-backed job index and guest migration history merge (fakeredis)."""

from __future__ import annotations

from typing import Any

import fakeredis
import pytest

from main import (
    _active_migration_index_by_vm_name,
    _assert_no_active_migration_for_vm_names,
    _merge_migration_history_into_vm_rows,
)


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "redis": {
            "job_key_prefix": "migration:test:job:",
            "job_log_suffix": ":log",
            "queue_key": "migration:test:queue:",
        }
    }


@pytest.fixture
def r() -> fakeredis.FakeStrictRedis:
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _seed_job(
    r: fakeredis.FakeStrictRedis,
    cfg: dict[str, Any],
    *,
    job_id: str,
    vm_name: str,
    state: str,
    created_at: str,
    updated_at: str,
    error: str = "",
) -> None:
    key = f"{cfg['redis']['job_key_prefix']}{job_id}"
    r.hset(
        key,
        mapping={
            "job_id": job_id,
            "vm_name": vm_name,
            "state": state,
            "created_at": created_at,
            "updated_at": updated_at,
            "proxmox_vmid": "3501",
            "proxmox_node": "pve",
            "vmdk_path_on_node": "store:dc/ds/guest/guest.vmdk",
            "winrm_user": "Administrator",
            "winrm_password": "",
            "guest_ip": "",
            "error": error,
            "last_failed_stage": "",
        },
    )


def test_active_index_picks_newest_non_terminal_by_timestamp(r: fakeredis.FakeStrictRedis, cfg: dict[str, Any]) -> None:
    _seed_job(
        r,
        cfg,
        job_id="older",
        vm_name="GuestA",
        state="IMPORTING",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    _seed_job(
        r,
        cfg,
        job_id="newer",
        vm_name="GuestA",
        state="PENDING",
        created_at="2026-01-02T00:00:00",
        updated_at="2026-01-03T00:00:00",
    )
    idx = _active_migration_index_by_vm_name(r, cfg)
    assert idx["guesta"]["job_id"] == "newer"
    assert idx["guesta"]["state"] == "PENDING"


def test_active_index_ignores_terminal_states(r: fakeredis.FakeStrictRedis, cfg: dict[str, Any]) -> None:
    _seed_job(
        r,
        cfg,
        job_id="done",
        vm_name="GuestB",
        state="COMPLETED",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-02T00:00:00",
    )
    idx = _active_migration_index_by_vm_name(r, cfg)
    assert "guestb" not in idx


def test_assert_no_active_migration_raises_409(r: fakeredis.FakeStrictRedis, cfg: dict[str, Any]) -> None:
    from fastapi import HTTPException

    _seed_job(
        r,
        cfg,
        job_id="run1",
        vm_name="BusyVM",
        state="SATA_BOOT",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T12:00:00",
    )
    with pytest.raises(HTTPException) as ei:
        _assert_no_active_migration_for_vm_names(["BusyVM", "busyvm"], r, cfg)
    assert ei.value.status_code == 409
    assert "BusyVM" in ei.value.detail
    assert "SATA_BOOT" in ei.value.detail


def test_merge_migration_history_sets_active_and_last_failed(r: fakeredis.FakeStrictRedis, cfg: dict[str, Any]) -> None:
    _seed_job(
        r,
        cfg,
        job_id="j1",
        vm_name="GuestC",
        state="FAILED",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-02T00:00:00",
        error="disk full " + ("x" * 200),
    )
    rows = [{"name": "GuestC", "uuid": None}]
    _merge_migration_history_into_vm_rows(rows, r, cfg)
    assert "migration_active_state" not in rows[0]
    assert rows[0].get("migration_last_terminal_state") == "FAILED"
    assert rows[0].get("migration_last_job_id") == "j1"
    preview = rows[0].get("migration_last_error_preview", "")
    assert preview.endswith("…")
    assert len(preview) <= 161
