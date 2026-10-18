"""Unit tests for firmware / guest-profile / vTPM resolution helpers."""

from __future__ import annotations

from typing import Any

import worker


def test_resolve_job_firmware_defaults_uefi_windows_vtpm() -> None:
    fw, prof, vtpm = worker._resolve_job_firmware_profile({}, {"proxmox": {}})
    assert fw == "uefi"
    assert prof == "windows"
    assert vtpm is True


def test_resolve_job_firmware_linux_uefi_no_vtpm() -> None:
    fw, prof, vtpm = worker._resolve_job_firmware_profile(
        {"firmware": "uefi", "guest_profile": "linux"},
        {"proxmox": {}},
    )
    assert fw == "uefi"
    assert prof == "linux"
    assert vtpm is False


def test_resolve_job_firmware_seabios_no_vtpm() -> None:
    fw, prof, vtpm = worker._resolve_job_firmware_profile(
        {"firmware": "seabios", "guest_profile": "windows"},
        {"proxmox": {}},
    )
    assert fw == "seabios"
    assert vtpm is False


def test_resolve_job_firmware_explicit_vtpm_opt_in_linux() -> None:
    fw, prof, vtpm = worker._resolve_job_firmware_profile(
        {"firmware": "uefi", "guest_profile": "linux", "enable_vtpm": "1"},
        {"proxmox": {}},
    )
    assert fw == "uefi"
    assert prof == "linux"
    assert vtpm is True


def test_post_migration_tags_strip_efi_redo_for_seabios() -> None:
    cfg: dict[str, Any] = {"proxmox": {}}
    tags = worker._post_migration_tags(cfg, firmware="seabios")
    assert "efi-redo-pending" not in [t.lower() for t in tags]


def test_ostype_for_profile() -> None:
    cfg = {"proxmox": {"ostype": "win11", "linux_ostype": "l26"}}
    assert worker._ostype_for_profile(cfg, "windows") == "win11"
    assert worker._ostype_for_profile(cfg, "linux") == "l26"


def test_hints_from_import_metadata_maps_ovmf_and_win() -> None:
    import ui as ui_module

    meta = {
        "create-args": {
            "bios": "ovmf",
            "ostype": "win11",
            "memory": 8192,
            "cores": 4,
            "sockets": 2,
        },
        "warnings": ["efi-state-lost"],
    }
    hint = ui_module._hints_from_import_metadata(meta, {"proxmox": {}})
    assert hint["firmware"] == "uefi"
    assert hint["guest_profile"] == "windows"
    assert hint["enable_vtpm_suggested"] is True
    assert hint["memory_mb"] == 8192
    assert hint["cores"] == 8  # cores × sockets
    assert "efi-state-lost" in hint["warnings"]


def test_hints_from_import_metadata_maps_seabios_linux() -> None:
    import ui as ui_module

    meta = {"create-args": {"bios": "seabios", "ostype": "l26", "memory": 2048, "cores": 2}}
    hint = ui_module._hints_from_import_metadata(meta, {"proxmox": {}})
    assert hint["firmware"] == "seabios"
    assert hint["guest_profile"] == "linux"
    assert hint["enable_vtpm_suggested"] is False
    assert hint["memory_mb"] == 2048
    assert hint["cores"] == 2


def test_import_metadata_volume_arg_strips_storage_prefix() -> None:
    import ui as ui_module

    assert (
        ui_module._import_metadata_volume_arg("esxi", "esxi:ha-datacenter/test2/clone1/clone1.vmx")
        == "ha-datacenter/test2/clone1/clone1.vmx"
    )
    assert (
        ui_module._import_metadata_volume_arg("esxi", "ha-datacenter/test2/clone1/clone1.vmx")
        == "ha-datacenter/test2/clone1/clone1.vmx"
    )


def test_hints_from_import_metadata_string_memory_and_sockets_only() -> None:
    """PVE often returns memory as a string and omits cores when cores-per-socket is 1."""
    import ui as ui_module

    meta = {
        "create-args": {
            "bios": "seabios",
            "ostype": "l26",
            "memory": "4096",
            "sockets": 2,
        }
    }
    hint = ui_module._hints_from_import_metadata(meta, {"proxmox": {}})
    assert hint["memory_mb"] == 4096
    assert hint["cores"] == 2
    assert hint["firmware"] == "seabios"
    assert hint["guest_profile"] == "linux"
