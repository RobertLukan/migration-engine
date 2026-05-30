from __future__ import annotations

from typing import Any

from main import resolve_vmdk_import_path


def test_resolve_vmdk_literal_template_formats_vm_name() -> None:
    cfg: dict[str, Any] = {"proxmox": {}}
    out = resolve_vmdk_import_path(cfg, None, "/imports/{vm_name}.vmdk", "Web01")
    assert out == "/imports/Web01.vmdk"


def test_resolve_vmdk_braced_path_with_hyphenated_name() -> None:
    cfg: dict[str, Any] = {"proxmox": {}}
    out = resolve_vmdk_import_path(cfg, None, "/mnt/imports/{vm_name}-flat.vmdk", "SRV-DB-01")
    assert out == "/mnt/imports/SRV-DB-01-flat.vmdk"
