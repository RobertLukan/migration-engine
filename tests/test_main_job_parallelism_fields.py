"""Job API parallelism fields derived from import path and permit state."""

from __future__ import annotations

from typing import Any

from main import JobRecord, MigrationState, _job_parallelism_api_fields


def _base_rec(**kwargs: Any) -> JobRecord:
    d: dict[str, Any] = {
        "job_id": "j1",
        "state": MigrationState.PENDING.value,
        "vm_name": "vm1",
        "proxmox_vmid": 3501,
        "proxmox_node": "pve",
        "vmdk_path_on_node": "sh12:dc/ds/vm/disk.vmdk",
    }
    d.update(kwargs)
    return JobRecord(**d)


def test_parallelism_bucket_for_pve_volid_path() -> None:
    cfg: dict[str, Any] = {"vmware": {"host": "vc.example"}, "worker": {"max_concurrent_imports_per_esxi_storage": 2}}
    rec = _base_rec(state=MigrationState.IMPORTING.value)
    data = {
        "vmdk_path_on_node": "sh13:ha-datacenter/DS1/guest/guest.vmdk",
        "proxmox_node": "pve",
        "proxmox_vmid": "3501",
        "import_permit_state": "ACQUIRED",
    }
    extra = _job_parallelism_api_fields(cfg, data, rec)
    assert extra["import_serialization_bucket"] == "sh13"
    assert "PVE ESXi store sh13" in (extra.get("import_parallelism_summary") or "")
    assert extra.get("import_activity_status") and "sh13" in extra["import_activity_status"]


def test_parallelism_preimported_skips_bucket() -> None:
    cfg: dict[str, Any] = {"vmware": {}, "worker": {}}
    rec = _base_rec(
        state=MigrationState.IMPORTING.value,
        vmdk_path_on_node="preimported:manual",
        preimported_disk_mode=True,
    )
    data = {
        "vmdk_path_on_node": "preimported:manual",
        "proxmox_node": "pve",
        "proxmox_vmid": "3501",
        "preimported_disk_mode": "1",
    }
    extra = _job_parallelism_api_fields(cfg, data, rec)
    assert extra["import_serialization_bucket"] is None
    assert "Preimported" in (extra.get("import_parallelism_summary") or "")
