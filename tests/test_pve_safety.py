from __future__ import annotations

import pytest

from pve_safety import validate_job_vmid_and_storage, validate_scratch_purge_volume


def test_validate_vmid_in_range() -> None:
    cfg = {
        "proxmox": {
            "safety": {
                "allowed_vmid_min": 100,
                "allowed_vmid_max": 200,
            }
        }
    }
    validate_job_vmid_and_storage(cfg, 150, "local-lvm")


def test_validate_vmid_below_min() -> None:
    cfg = {"proxmox": {"safety": {"allowed_vmid_min": 500, "allowed_vmid_max": 600}}}
    with pytest.raises(ValueError, match="below"):
        validate_job_vmid_and_storage(cfg, 400, "local-lvm")


def test_validate_storage_not_allowed() -> None:
    cfg = {
        "proxmox": {
            "safety": {
                "allowed_storage_ids": ["pool-a", "pool-b"],
            }
        }
    }
    validate_job_vmid_and_storage(cfg, 1, "pool-a")
    with pytest.raises(ValueError, match="allowed_storage_ids"):
        validate_job_vmid_and_storage(cfg, 1, "other-pool")


def test_validate_scratch_purge_name_ok() -> None:
    cfg = {"proxmox": {"safety": {"scratch_purge_require_vm_disk_name": True}}}
    validate_scratch_purge_volume(cfg, 3500, "vm-3500-disk-0")


def test_validate_scratch_purge_name_rejected() -> None:
    cfg = {"proxmox": {"safety": {"scratch_purge_require_vm_disk_name": True}}}
    with pytest.raises(ValueError, match="Refusing to purge"):
        validate_scratch_purge_volume(cfg, 3500, "vm-9999-disk-0")


def test_invalid_vmid_range_in_config() -> None:
    cfg = {"proxmox": {"safety": {"allowed_vmid_min": 900, "allowed_vmid_max": 100}}}
    with pytest.raises(ValueError, match="allowed_vmid_min"):
        validate_job_vmid_and_storage(cfg, 500, "local-lvm")
