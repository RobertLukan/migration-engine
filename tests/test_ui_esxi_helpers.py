"""Regression tests for ESXi / listing helpers in ``ui`` (no Proxmox network)."""

from __future__ import annotations

from typing import Any


def test_esxi_uri_from_volid_storage_prefix(ui_module: Any) -> None:
    u = ui_module._esxi_uri_from_volid("sh12:ha-datacenter/DS1/vm/vm.vmdk")
    assert u == "esxi:ha-datacenter/DS1/vm/vm.vmdk"


def test_esxi_uri_from_volid_finds_embedded_esxi_prefix(ui_module: Any) -> None:
    u = ui_module._esxi_uri_from_volid("junk:esxi:ha-datacenter/DS1/vm/vm.vmdk")
    assert u == "esxi:ha-datacenter/DS1/vm/vm.vmdk"


def test_guest_label_from_path_folder_matches_stem(ui_module: Any) -> None:
    assert ui_module._guest_label_from_esxi_path("esxi:dc/ds/MyVM/MyVM.vmdk") == "MyVM"


def test_guest_label_from_path_stem_when_folder_differs(ui_module: Any) -> None:
    assert ui_module._guest_label_from_esxi_path("esxi:dc/ds/other/disk-flat.vmdk") == "disk-flat"


def test_guess_import_refs_from_vmx_preserves_storage_id(ui_module: Any) -> None:
    refs = ui_module._guess_import_refs_from_vmx(
        "esxi:ha-datacenter/DS1/MyVM/MyVM.vmx",
        "sh12:ha-datacenter/DS1/MyVM/MyVM.vmx",
    )
    assert any(r.startswith("sh12:") and r.endswith("MyVM.vmdk") for r in refs)
    assert any(r.lower().startswith("esxi:") for r in refs)
