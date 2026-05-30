"""``_vm_inventory_merge`` behaviour (Proxmox rows + optional vCenter enrichment)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest


@contextmanager
def _fake_vc(_cfg: dict[str, Any]) -> Iterator[Any]:
    yield object()


def test_inventory_merge_pve_only_when_vmware_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    pve = [
        {
            "name": "Alpha",
            "vmware_moref": "",
            "uuid": None,
            "power_state": None,
            "inventory_source": "pve_esxi",
            "esxi_import_uri": "sh12:dc/ds/a/a.vmdk",
            "esxi_storage_ids": ["sh12"],
            "esxi_listing_size_bytes": 1024,
        }
    ]
    monkeypatch.setattr(main.ui_module, "enumerate_guests_from_pve_esxi", lambda cfg, node: list(pve))
    monkeypatch.setattr(main.ui_module, "vmware_credentials_configured", lambda cfg: False)
    cfg = {"proxmox": {"default_node": "pve"}}
    rows = main._vm_inventory_merge(cfg)
    assert len(rows) == 1
    assert rows[0]["name"] == "Alpha"
    assert rows[0]["inventory_source"] == "pve_esxi"
    assert rows[0]["esxi_listing_size_bytes"] == 1024


def test_inventory_merge_enriches_matching_guest_from_vcenter(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    pve = [
        {
            "name": "Beta",
            "vmware_moref": "",
            "uuid": None,
            "power_state": None,
            "inventory_source": "pve_esxi",
            "esxi_import_uri": "sh12:x/x.vmdk",
            "esxi_storage_ids": ["sh12"],
            "esxi_listing_size_bytes": None,
        }
    ]
    monkeypatch.setattr(main.ui_module, "enumerate_guests_from_pve_esxi", lambda cfg, node: list(pve))
    monkeypatch.setattr(main.ui_module, "vmware_credentials_configured", lambda cfg: True)
    monkeypatch.setattr(main, "vcenter_connection", _fake_vc)

    class _FakeVm:
        name = "Beta"

    monkeypatch.setattr(main, "iter_virtual_machines", lambda si: [_FakeVm()])
    monkeypatch.setattr(
        main,
        "vm_to_summary",
        lambda vm: {
            "name": vm.name,
            "vmware_moref": "vm-99",
            "uuid": "421c-a-b-c",
            "power_state": "POWERED_ON",
        },
    )
    cfg = {"proxmox": {"default_node": "pve"}, "vmware": {"host": "h", "user": "u", "password": "p"}}
    rows = main._vm_inventory_merge(cfg)
    assert len(rows) == 1
    r0 = rows[0]
    assert r0["inventory_source"] == "pve_esxi+vcenter"
    assert r0["vmware_moref"] == "vm-99"
    assert r0["uuid"] == "421c-a-b-c"
    assert r0["power_state"] == "POWERED_ON"
    assert r0["esxi_import_uri"] == "sh12:x/x.vmdk"


def test_inventory_merge_falls_back_to_pve_on_vcenter_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    pve = [
        {
            "name": "Gamma",
            "vmware_moref": "",
            "uuid": None,
            "power_state": None,
            "inventory_source": "pve_esxi",
            "esxi_import_uri": None,
            "esxi_storage_ids": [],
            "esxi_listing_size_bytes": None,
        }
    ]
    monkeypatch.setattr(main.ui_module, "enumerate_guests_from_pve_esxi", lambda cfg, node: list(pve))
    monkeypatch.setattr(main.ui_module, "vmware_credentials_configured", lambda cfg: True)

    @contextmanager
    def _boom(_cfg: dict[str, Any]) -> Iterator[Any]:
        raise OSError("vCenter unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(main, "vcenter_connection", _boom)
    cfg = {"proxmox": {"default_node": "pve"}, "vmware": {"host": "h", "user": "u", "password": "p"}}
    rows = main._vm_inventory_merge(cfg)
    assert len(rows) == 1
    assert rows[0]["name"] == "Gamma"
    assert rows[0]["inventory_source"] == "pve_esxi"
