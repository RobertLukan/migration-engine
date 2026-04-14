"""
FastAPI API for VMware → Proxmox migration orchestration (Redis state + queue).
Dashboard UI: session password auth, credential editing, connection tests.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

import redis
import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from starlette.middleware.sessions import SessionMiddleware

import ui as ui_module
from pve_safety import validate_job_vmid_and_storage
from states import MigrationState
from ui import require_ui_session, router as ui_router
from worker import (
    allocate_sequential_free_vmids,
    connect_proxmox,
    mark_job_cancelled_operator,
    qemu_vmids_in_use_on_node,
)

_cfg_path_override = (os.environ.get("MIGRATION_ENGINE_CONFIG") or "").strip()
CONFIG_PATH = (
    Path(_cfg_path_override).expanduser()
    if _cfg_path_override
    else Path(__file__).resolve().parent / "config.yaml"
)
STATIC_DIR = Path(__file__).resolve().parent / "static"

ui_module.CONFIG_PATH = CONFIG_PATH


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def redis_client() -> redis.Redis:
    cfg = load_config()
    return redis.from_url(cfg["redis"]["url"], decode_responses=True)


def job_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{cfg['redis']['job_key_prefix']}{job_id}"


@contextmanager
def vcenter_connection(cfg: dict[str, Any]) -> Iterator[Any]:
    vc = cfg["vmware"]
    si = SmartConnect(
        host=vc["host"],
        user=vc["user"],
        pwd=vc["password"],
        port=int(vc.get("port", 443)),
        disableSslCertValidation=bool(vc.get("insecure", False)),
    )
    try:
        yield si
    finally:
        Disconnect(si)


def iter_virtual_machines(si: Any) -> Iterator[vim.VirtualMachine]:
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm in container.view:
            if isinstance(vm, vim.VirtualMachine):
                yield vm
    finally:
        container.Destroy()


def vm_to_summary(vm: vim.VirtualMachine) -> dict[str, Any]:
    return {
        "vmware_moref": str(vm._moId),
        "name": vm.name,
        "uuid": vm.config.uuid if vm.config else None,
        "power_state": str(vm.runtime.powerState) if vm.runtime else None,
    }


_SKIP_VSPHERE_FOLDER_NAMES = frozenset(
    {"vm", "host", "datastore", "network", "root"}
)  # "root" is a common default folder under DC/vm; PVE esxi: paths usually follow datastore dirs instead
_AUTO_ESXI_TEMPLATES = frozenset({"auto-esxi", "auto"})


def _datacenter_for_vm(vm: vim.VirtualMachine) -> vim.Datacenter | None:
    cur: Any = vm.parent
    while cur:
        if isinstance(cur, vim.Datacenter):
            return cur
        cur = getattr(cur, "parent", None)
    return None


def _inventory_folder_segments(vm: vim.VirtualMachine) -> tuple[vim.Datacenter, list[str]]:
    """Folder names from DC's vm subtree up to (but excluding) the VM object."""
    dc = _datacenter_for_vm(vm)
    if not dc:
        raise ValueError(f"VM {vm.name!r} has no datacenter in its parent chain")
    segments: list[str] = []
    cur: Any = vm.parent
    while cur and cur is not dc:
        if isinstance(cur, vim.Folder) and cur.name and cur.name not in _SKIP_VSPHERE_FOLDER_NAMES:
            segments.insert(0, cur.name)
        cur = getattr(cur, "parent", None)
    return dc, segments


def _split_bracketed_datastore_path(file_path: str) -> tuple[str, str]:
    """``[ds] a/b/c.ext`` → (``ds``, ``a/b``)."""
    fp = str(file_path or "").strip()
    if "]" not in fp:
        return "", ""
    left, rest = fp.split("]", 1)
    ds = left.strip().lstrip("[").strip()
    rest = rest.strip().replace("\\", "/")
    if not rest:
        return ds, ""
    parent = str(Path(rest).parent).strip("/").replace("\\", "/")
    return ds, parent


def _vmx_directory_relative(vm: vim.VirtualMachine) -> str:
    """
    Path under the VM's datastore from config.files.vmPathName.
    Example: ``[test2] clone1/clone1.vmx`` -> ``test2/clone1``.
    """
    if not vm.config or not vm.config.files:
        return ""
    ds, parent = _split_bracketed_datastore_path(str(vm.config.files.vmPathName or ""))
    if not ds and not parent:
        return ""
    return "/".join([p for p in (ds, parent) if p]).strip("/")


def _backing_and_vmx_relative_dirs(vm: vim.VirtualMachine) -> list[str]:
    """Datastore-relative directories from vmx and each disk ``fileName`` (deepest usually wins)."""
    seen: set[str] = set()
    out: list[str] = []
    if vm.config and vm.config.files and vm.config.files.vmPathName:
        ds, parent = _split_bracketed_datastore_path(str(vm.config.files.vmPathName))
        d = "/".join([p for p in (ds, parent) if p]).strip("/")
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    if vm.config and vm.config.hardware:
        for dev in vm.config.hardware.device or []:
            if not isinstance(dev, vim.vm.device.VirtualDisk):
                continue
            fn = getattr(dev.backing, "fileName", None) or ""
            ds, parent = _split_bracketed_datastore_path(str(fn))
            d = "/".join([p for p in (ds, parent) if p]).strip("/")
            if d and d not in seen:
                seen.add(d)
                out.append(d)
    return out


def _deepest_rel_dir_for_esxi(candidates: list[str], vm_name: str) -> str:
    n = vm_name.lower()
    best = ""
    best_depth = -1
    for d in candidates:
        if n not in d.lower():
            continue
        depth = len([p for p in d.replace("\\", "/").split("/") if p])
        if depth > best_depth:
            best_depth = depth
            best = d.replace("\\", "/").strip("/")
    return best


def _primary_vmdk_basename(vm: vim.VirtualMachine) -> str:
    """Basename of the first base disk VMDK from VM config (best effort)."""
    for dev in vm.config.hardware.device if vm.config else []:
        if not isinstance(dev, vim.vm.device.VirtualDisk):
            continue
        fn = getattr(dev.backing, "fileName", None) or ""
        if not fn:
            continue
        base = os.path.basename(str(fn).strip())
        if base.lower().endswith(".vmdk"):
            return base
    return f"{vm.name}.vmdk"


def infer_esxi_import_uri(vm: vim.VirtualMachine) -> str:
    """
    Build a Proxmox import-from ESXi URI for linked ESXi storage in PVE.

    Picks the **deepest** datastore-relative directory from vmx + disk ``fileName``
    paths (often ``test2/clone1`` even when vmx path omits ``test2``), then merges
    moRef inventory folders. Optional ``proxmox.esxi_import_parent_hint`` prepends
    one folder when the guest still resolves to ``DC/vm/vm.vmdk``.
    """
    dc = _datacenter_for_vm(vm)
    if not dc:
        raise ValueError(f"VM {vm.name!r} has no datacenter in its parent chain")
    vmdk = _primary_vmdk_basename(vm)
    _, inv_folders = _inventory_folder_segments(vm)
    cfg = load_config()
    hint = ((cfg.get("proxmox") or {}).get("esxi_import_parent_hint") or "").strip().strip("/")

    rel = _deepest_rel_dir_for_esxi(_backing_and_vmx_relative_dirs(vm), vm.name)
    if not rel:
        rel = _vmx_directory_relative(vm)
    if hint:
        rp = [p for p in rel.replace("\\", "/").split("/") if p] if rel else []
        hlow = hint.lower()
        if not rel:
            rel = f"{hint}/{vm.name}"
        elif len(rp) == 1 and rp[0] == vm.name and not rel.lower().startswith(hlow + "/"):
            rel = f"{hint}/{vm.name}"

    body: list[str] = []
    if rel:
        rel_parts = [p for p in rel.replace("\\", "/").split("/") if p]
        inv_join = "/".join(inv_folders)
        rel_join = "/".join(rel_parts)
        if inv_folders:
            if rel_join.lower().startswith(inv_join.lower() + "/") or rel_join.lower() == inv_join.lower():
                body = rel_parts
            elif len(rel_parts) == 1 and rel_parts[0] == vm.name:
                body = [*inv_folders, *rel_parts]
            else:
                body = [*inv_folders, *rel_parts]
        else:
            body = rel_parts
    else:
        body = [*inv_folders, vm.name]

    mid = "/".join([dc.name, *body, vmdk]).replace("//", "/")
    return f"esxi:{mid}"


def find_first_vm_named(si: Any, name: str) -> vim.VirtualMachine | None:
    for vm in iter_virtual_machines(si):
        if vm.name == name:
            return vm
    return None


def resolve_vmdk_import_path(
    cfg: dict[str, Any],
    si: Any | None,
    template: str,
    vm_name: str,
    *,
    proxmox_node: str | None = None,
) -> str:
    t = (template or "").strip()
    if t.lower() not in _AUTO_ESXI_TEMPLATES:
        return t.format(vm_name=vm_name)
    nod = (proxmox_node or (cfg.get("proxmox") or {}).get("default_node") or "").strip()
    p = ui_module.discover_esxi_import_from_pve(nod, vm_name) if nod else None
    if p:
        return p
    if si is not None:
        vm = find_first_vm_named(si, vm_name)
        if vm is None:
            raise ValueError(f"VM {vm_name!r} not found in vCenter inventory")
        return infer_esxi_import_uri(vm)
    raise ValueError(
        f"auto-esxi: no esxi:… disk in Proxmox ESXi storage listings for {vm_name!r} on node {nod!r}; "
        "link ESXi in PVE, check Datastore.Audit permissions, or save VMware credentials for vCenter path fallback."
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateJobRequest(BaseModel):
    vm_name: str = Field(..., description="Guest name on Proxmox / VMware")
    proxmox_vmid: int = Field(..., ge=100, le=999999999)
    proxmox_node: str | None = Field(default=None, description="Defaults to proxmox.default_node")
    vmdk_path_on_node: str | None = Field(
        default=None,
        description="import-from source: absolute path on the PVE node, or esxi:… URI; default uses proxmox.vmdk_path_template",
    )
    proxmox_storage: str | None = Field(
        default=None,
        description="Proxmox storage id for import / disks; default from config",
    )
    memory_mb: int | None = Field(default=None, ge=512, le=1048576)
    cores: int | None = Field(default=None, ge=1, le=256)
    vlan: int | str | None = Field(default=None, description="Optional VLAN tag (1-4094) for guest net0")
    winrm_user: str = Field(default="Administrator")
    winrm_password: str = Field(default="")
    simulated_import: bool | None = Field(
        default=None,
        description="If true, clone proxmox.simulated_clone_source_vmid instead of VMware import; None uses config default",
    )
    simulated_clone_source_vmid: int | None = Field(
        default=None,
        ge=100,
        description=(
            "Only for simulated_import=true: source Proxmox template VMID to clone. "
            "If unset, proxmox.simulated_clone_source_vmid from config is used."
        ),
    )
    qemu_cpu_preset: Literal["host_no_nested", "host_nested_vbs"] | None = Field(
        default=None,
        description=(
            "Clears PVE cpu= and sets qemu args to a fixed -cpu host profile (nested/VBS variants). "
            "None leaves CPU as after import/clone."
        ),
    )


class MigrateSelectedRequest(BaseModel):
    """Enqueue one migration job per VM name (must exist in vCenter inventory)."""

    vm_names: list[str] = Field(..., min_length=1)
    proxmox_vmid_start: int = Field(..., ge=100, le=999999999)
    proxmox_storage: str = Field(..., min_length=1, description="PVE storage id, e.g. local-lvm")
    memory_mb: int = Field(..., ge=512, le=1048576)
    cores: int = Field(..., ge=1, le=256)
    vlan: int | str | None = Field(default=None, description="Optional VLAN tag (1-4094) for guest net0")
    proxmox_node: str | None = None
    winrm_user: str = "Administrator"
    winrm_password: str = ""
    simulated_import: bool | None = Field(
        default=None,
        description="If true, clone template VM on Proxmox instead of VMware disk import; None uses config default",
    )
    simulated_clone_source_vmid: int | None = Field(
        default=None,
        ge=100,
        description=(
            "Only for simulated_import=true: source Proxmox template VMID to clone. "
            "If unset, proxmox.simulated_clone_source_vmid from config is used."
        ),
    )
    qemu_cpu_preset: Literal["host_no_nested", "host_nested_vbs"] | None = Field(
        default=None,
        description="Custom qemu -cpu via args=; None leaves template/import CPU settings.",
    )


class JobRecord(BaseModel):
    job_id: str
    state: str
    vm_name: str
    proxmox_vmid: int
    proxmox_node: str
    vmdk_path_on_node: str
    winrm_user: str
    proxmox_storage: str | None = None
    memory_mb: int | None = None
    cores: int | None = None
    vlan: int | None = None
    guest_ip: str | None = None
    error: str | None = None
    last_failed_stage: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    simulated_import: bool | None = None
    simulated_clone_source_vmid: int | None = None
    qemu_cpu_preset: str | None = None


class VCenterEnqueueFilters(BaseModel):
    names: list[str] | None = None
    name_prefix: str | None = None


class VCenterEnqueueRequest(VCenterEnqueueFilters):
    """Each matched VM gets a job; you must supply proxmox_vmid_start (incremented per VM)."""

    proxmox_vmid_start: int = Field(..., ge=100)
    proxmox_node: str | None = None
    winrm_user: str = "Administrator"
    winrm_password: str = ""


_cfg_boot = load_config()
_ui = _cfg_boot.get("ui") or {}
_session_secret = (_ui.get("session_secret") or "").strip()
if not _session_secret:
    raise RuntimeError(
        "config.yaml must define ui.session_secret (random string) and ui.password (dashboard password)"
    )

app = FastAPI(title="VMware → Proxmox migration orchestrator", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=False,
    max_age=14 * 24 * 3600,
)

api = APIRouter(dependencies=[Depends(require_ui_session)])


def _vm_inventory_merge(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Guests from Proxmox ESXi storage; vCenter enriches matching names when configured."""
    node = str((cfg.get("proxmox") or {}).get("default_node") or "").strip()
    pve_rows = ui_module.enumerate_guests_from_pve_esxi(cfg, node)
    by_lower: dict[str, dict[str, Any]] = {}
    for r in pve_rows:
        row = dict(r)
        row.setdefault("inventory_source", "pve_esxi")
        by_lower[row["name"].lower()] = row

    if not ui_module.vmware_credentials_configured(cfg):
        return sorted(by_lower.values(), key=lambda x: x["name"].lower())

    try:
        with vcenter_connection(cfg) as si:
            for vm in iter_virtual_machines(si):
                s = vm_to_summary(vm)
                lk = s["name"].lower()
                if lk in by_lower:
                    by_lower[lk].update(
                        {
                            "vmware_moref": s["vmware_moref"],
                            "uuid": s["uuid"],
                            "power_state": s["power_state"],
                            "inventory_source": "pve_esxi+vcenter",
                        }
                    )
                else:
                    by_lower[lk] = {
                        **s,
                        "inventory_source": "vcenter",
                    }
        return sorted(by_lower.values(), key=lambda x: x["name"].lower())
    except Exception:
        return sorted(pve_rows, key=lambda x: x["name"].lower())


@api.get("/vms")
def list_vms_for_migrate() -> list[dict[str, Any]]:
    cfg = load_config()
    return _vm_inventory_merge(cfg)


def _migration_default_net_mtu(px: dict[str, Any]) -> int:
    try:
        nm = int(px.get("net_mtu", 1500))
    except (TypeError, ValueError):
        return 1500
    return nm if nm > 0 else 1500


@api.get("/migration-defaults")
def migration_defaults(proxmox_node: str | None = None) -> dict[str, Any]:
    """Defaults for the migrate dialog (from config.yaml proxmox section)."""
    cfg = load_config()
    p = cfg.get("proxmox") or {}
    tmpl = (p.get("vmdk_path_template") or "").strip()
    out: dict[str, Any] = {
        "storage": p.get("storage", "local-lvm"),
        "memory_mb": int(p.get("memory_mb", 8192)),
        "cores": int(p.get("cores", 4)),
        "default_node": p.get("default_node", ""),
        "bridge": p.get("bridge", "vmbr0"),
        "vlan": p.get("vlan", ""),
        "net_mtu": _migration_default_net_mtu(p),
        "vmdk_path_template": tmpl,
        "vmdk_template_auto_esxi": tmpl.lower() in _AUTO_ESXI_TEMPLATES,
        "vmdk_auto_esxi_prefers_pve": True,
        "esxi_import_parent_hint": (p.get("esxi_import_parent_hint") or "").strip(),
        "simulated_import_default": bool(p.get("simulated_import", False)),
        "simulated_clone_source_vmid": int(p.get("simulated_clone_source_vmid", 147) or 147),
        "simulated_clone_source_node": (p.get("simulated_clone_source_node") or "").strip(),
    }
    safety = p.get("safety")
    if isinstance(safety, dict):
        try:
            if safety.get("allowed_vmid_min") is not None:
                out["migration_vmid_min"] = int(safety["allowed_vmid_min"])
        except (TypeError, ValueError):
            pass
        try:
            if safety.get("allowed_vmid_max") is not None:
                out["migration_vmid_max"] = int(safety["allowed_vmid_max"])
        except (TypeError, ValueError):
            pass
    nod = (proxmox_node or p.get("default_node") or "").strip()
    if nod:
        try:
            used = qemu_vmids_in_use_on_node(connect_proxmox(cfg), nod)
            vmin = out.get("migration_vmid_min")
            vmax = out.get("migration_vmid_max")
            if used:
                nxt = max(max(used) + 1, 100)
            else:
                nxt = int(vmin) if vmin is not None else 100
            if vmin is not None:
                nxt = max(nxt, int(vmin))
            if vmax is not None and nxt > int(vmax):
                pass
            else:
                out["next_free_proxmox_vmid"] = nxt
        except Exception:
            pass
    return out


@api.post("/jobs", response_model=JobRecord)
def create_job(body: CreateJobRequest) -> JobRecord:
    cfg = load_config()
    r = redis_client()
    job_id = str(uuid.uuid4())
    node = body.proxmox_node or cfg["proxmox"]["default_node"]
    if not node or not str(node).strip():
        raise HTTPException(
            status_code=400,
            detail="proxmox.default_node or proxmox_node is required to verify VM ID availability",
        )
    px = cfg.get("proxmox") or {}
    use_simulated = body.simulated_import
    if use_simulated is None:
        use_simulated = bool(px.get("simulated_import", False))
    template = (px.get("vmdk_path_template") or "").strip()
    vmdk = (body.vmdk_path_on_node or "").strip()
    if use_simulated:
        vmdk = vmdk or "simulated:clone"
    elif not vmdk:
        if not template:
            raise HTTPException(status_code=400, detail="vmdk_path_on_node or vmdk_path_template required")
        if template.lower() in _AUTO_ESXI_TEMPLATES:
            nod = (body.proxmox_node or cfg["proxmox"].get("default_node") or "").strip()
            vmdk = ui_module.discover_esxi_import_from_pve(nod, body.vm_name) if nod else ""
            if not vmdk:
                with vcenter_connection(cfg) as si:
                    try:
                        vmdk = resolve_vmdk_import_path(
                            cfg, si, template, body.vm_name, proxmox_node=nod or None
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            vmdk = template.format(vm_name=body.vm_name)

    store_job = (body.proxmox_storage or px.get("storage") or "").strip() or "local-lvm"
    try:
        validate_job_vmid_and_storage(cfg, int(body.proxmox_vmid), store_job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    nod = str(node).strip()
    try:
        used_now = qemu_vmids_in_use_on_node(connect_proxmox(cfg), nod)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot verify Proxmox VM ID on node {nod!r}: {exc}",
        ) from exc
    if int(body.proxmox_vmid) in used_now:
        alt_ids, _ = allocate_sequential_free_vmids(set(used_now), int(body.proxmox_vmid), 1)
        alt = alt_ids[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"Proxmox VM ID {body.proxmox_vmid} is already in use on node {nod!r}. "
                f"Retry with proxmox_vmid={alt} (next free from your requested ID)."
            ),
        )

    now = utc_now_iso()
    mapping: dict[str, str] = {
        "job_id": job_id,
        "state": MigrationState.PENDING.value,
        "vm_name": body.vm_name,
        "proxmox_vmid": str(body.proxmox_vmid),
        "proxmox_node": node,
        "vmdk_path_on_node": vmdk,
        "winrm_user": body.winrm_user,
        "winrm_password": body.winrm_password,
        "guest_ip": "",
        "error": "",
        "last_failed_stage": "",
        "created_at": now,
        "updated_at": now,
    }
    if body.proxmox_storage:
        mapping["proxmox_storage"] = body.proxmox_storage.strip()
    if body.memory_mb is not None:
        mapping["memory_mb"] = str(body.memory_mb)
    if body.cores is not None:
        mapping["cores"] = str(body.cores)
    if body.vlan is not None and str(body.vlan).strip() != "":
        mapping["vlan"] = str(body.vlan).strip()
    mapping["simulated_import"] = "1" if use_simulated else "0"
    if use_simulated and body.simulated_clone_source_vmid is not None:
        mapping["simulated_clone_source_vmid"] = str(int(body.simulated_clone_source_vmid))
    if body.qemu_cpu_preset is not None:
        mapping["qemu_cpu_preset"] = body.qemu_cpu_preset
    key = job_key(cfg, job_id)
    r.hset(key, mapping=mapping)
    r.rpush(cfg["redis"]["queue_key"], job_id)
    return _hash_to_record(r.hgetall(key))


@api.post("/jobs/migrate-selected")
def migrate_selected(body: MigrateSelectedRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    px = cfg.get("proxmox") or {}
    use_simulated = body.simulated_import
    if use_simulated is None:
        use_simulated = bool(px.get("simulated_import", False))
    template = (px.get("vmdk_path_template") or "").strip()
    if not use_simulated and not template:
        raise HTTPException(
            status_code=400,
            detail="Set proxmox.vmdk_path_template in config.yaml (auto-esxi, or /imports/{vm_name}.vmdk, or esxi:DC/.../{vm_name}/...), or enable simulated import",
        )
    node = body.proxmox_node or px.get("default_node")
    if not node:
        raise HTTPException(status_code=400, detail="proxmox.default_node or proxmox_node is required")

    ordered: list[str] = []
    seen: set[str] = set()
    for raw in body.vm_names:
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    if not ordered:
        raise HTTPException(status_code=400, detail="No valid VM names after deduplication")

    job_ids: list[str] = []
    storage = body.proxmox_storage.strip()
    node_str = str(node).strip()
    proxmox = connect_proxmox(cfg)
    try:
        in_use = qemu_vmids_in_use_on_node(proxmox, node_str)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot list QEMU guests on Proxmox node {node_str!r}: {exc}",
        ) from exc
    slot = set(in_use)
    try:
        allocated_ids, next_proxmox_vmid = allocate_sequential_free_vmids(
            slot, body.proxmox_vmid_start, len(ordered)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    vmid_auto_adjusted = allocated_ids[0] != body.proxmox_vmid_start
    _ms_extra = {
        "proxmox_vmids_assigned": allocated_ids,
        "proxmox_vmid_start_requested": body.proxmox_vmid_start,
        "proxmox_vmid_auto_adjusted": vmid_auto_adjusted,
    }
    try:
        for vid in allocated_ids:
            validate_job_vmid_and_storage(cfg, int(vid), storage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    auto_esxi = template.lower() in _AUTO_ESXI_TEMPLATES
    pve_rows_enum = ui_module.enumerate_guests_from_pve_esxi(cfg, node_str)
    pve_name_keys = {r["name"].lower() for r in pve_rows_enum}
    pve_uri_by_name: dict[str, str] = {}
    for row in pve_rows_enum:
        u = (row.get("esxi_import_uri") or "").strip()
        if not u:
            continue
        nm = str(row.get("name") or "").strip()
        if not nm:
            continue
        pve_uri_by_name[nm.lower()] = u

    pve_only_paths: dict[str, str] = {}
    if not use_simulated and auto_esxi:
        for n in ordered:
            hit = pve_uri_by_name.get(n.lower()) or ui_module.discover_esxi_import_from_pve(
                node_str, n
            )
            if hit:
                pve_only_paths[n] = hit

    def enqueue_one(name: str, vmdk: str, assigned_vmid: int) -> None:
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        mapping = {
            "job_id": job_id,
            "state": MigrationState.PENDING.value,
            "vm_name": name,
            "proxmox_vmid": str(assigned_vmid),
            "proxmox_node": node,
            "vmdk_path_on_node": vmdk,
            "proxmox_storage": storage,
            "memory_mb": str(body.memory_mb),
            "cores": str(body.cores),
            "winrm_user": body.winrm_user,
            "winrm_password": body.winrm_password,
            "guest_ip": "",
            "error": "",
            "last_failed_stage": "",
            "created_at": now,
            "updated_at": now,
            "simulated_import": "1" if use_simulated else "0",
        }
        if body.vlan is not None and str(body.vlan).strip() != "":
            mapping["vlan"] = str(body.vlan).strip()
        if use_simulated and body.simulated_clone_source_vmid is not None:
            mapping["simulated_clone_source_vmid"] = str(int(body.simulated_clone_source_vmid))
        if body.qemu_cpu_preset is not None:
            mapping["qemu_cpu_preset"] = body.qemu_cpu_preset
        key = job_key(cfg, job_id)
        r.hset(key, mapping=mapping)
        r.rpush(cfg["redis"]["queue_key"], job_id)
        job_ids.append(job_id)

    def _validate_names_against_pve_esxi() -> None:
        miss = [n for n in ordered if n.strip().lower() not in pve_name_keys]
        if miss:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"VMs not found on Proxmox ESXi storage for node {node_str!r}: {miss}. "
                    "Names must match guests listed from linked ESXi storage in PVE."
                ),
            )

    if use_simulated:
        if ui_module.vmware_credentials_configured(cfg):
            try:
                with vcenter_connection(cfg) as si:
                    by_name = {vm.name: vm for vm in iter_virtual_machines(si)}
                    if all(n in by_name for n in ordered):
                        for name, vid in zip(ordered, allocated_ids, strict=True):
                            enqueue_one(name, "simulated:clone", vid)
                        return {
                            "enqueued": len(job_ids),
                            "job_ids": job_ids,
                            "next_proxmox_vmid": next_proxmox_vmid,
                            **_ms_extra,
                        }
            except HTTPException:
                raise
            except Exception:
                pass
        _validate_names_against_pve_esxi()
        for name, vid in zip(ordered, allocated_ids, strict=True):
            enqueue_one(name, "simulated:clone", vid)
        return {
            "enqueued": len(job_ids),
            "job_ids": job_ids,
            "next_proxmox_vmid": next_proxmox_vmid,
            **_ms_extra,
        }

    if auto_esxi and len(pve_only_paths) == len(ordered):
        for name, vid in zip(ordered, allocated_ids, strict=True):
            enqueue_one(name, pve_only_paths[name], vid)
        return {
            "enqueued": len(job_ids),
            "job_ids": job_ids,
            "next_proxmox_vmid": next_proxmox_vmid,
            **_ms_extra,
        }

    if ui_module.vmware_credentials_configured(cfg):
        try:
            with vcenter_connection(cfg) as si:
                by_name: dict[str, vim.VirtualMachine] = {}
                for vm in iter_virtual_machines(si):
                    if vm.name not in by_name:
                        by_name[vm.name] = vm
                missing = [n for n in ordered if n not in by_name]
                if missing:
                    raise HTTPException(
                        status_code=400,
                        detail=f"VMs not found in vCenter: {missing}",
                    )
                for name, vid in zip(ordered, allocated_ids, strict=True):
                    try:
                        vmdk = (
                            pve_only_paths.get(name)
                            or pve_uri_by_name.get(name.lower())
                            or resolve_vmdk_import_path(
                                cfg, si, template, name, proxmox_node=node_str
                            )
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    enqueue_one(name, vmdk, vid)
                return {
                    "enqueued": len(job_ids),
                    "job_ids": job_ids,
                    "next_proxmox_vmid": next_proxmox_vmid,
                    **_ms_extra,
                }
        except HTTPException:
            raise
        except Exception:
            pass

    _validate_names_against_pve_esxi()
    for name, vid in zip(ordered, allocated_ids, strict=True):
        vmdk = (
            pve_only_paths.get(name)
            or pve_uri_by_name.get(name.lower())
            or ui_module.discover_esxi_import_from_pve(node_str, name)
        )
        if not vmdk:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No ESXi import-from path for {name!r} on Proxmox node {node_str!r}. "
                    "The guest must appear on linked ESXi storage on that node (see GET /vms and esxi_import_uri). "
                    "Check proxmox.vmdk_path_template (auto-esxi), default_node, and that a ``.vmdk`` "
                    "under ``esxi:…`` is listed for this VM on that node."
                ),
            )
        enqueue_one(name, vmdk, vid)

    return {
        "enqueued": len(job_ids),
        "job_ids": job_ids,
        "next_proxmox_vmid": next_proxmox_vmid,
        **_ms_extra,
    }


@api.post("/jobs/enqueue-from-vcenter")
def enqueue_from_vcenter(body: VCenterEnqueueRequest) -> dict[str, Any]:
    cfg = load_config()
    if not ui_module.vmware_credentials_configured(cfg):
        raise HTTPException(
            status_code=400,
            detail="enqueue-from-vcenter requires VMware (vCenter) credentials in config.yaml",
        )
    r = redis_client()
    names_set = set(body.names) if body.names else None
    prefix = body.name_prefix
    node = body.proxmox_node or cfg["proxmox"]["default_node"]
    template = (cfg["proxmox"].get("vmdk_path_template") or "").strip()
    node_str = str(node).strip()
    proxmox = connect_proxmox(cfg)
    try:
        vcenter_slot = qemu_vmids_in_use_on_node(proxmox, node_str)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot list QEMU guests on Proxmox node {node_str!r}: {exc}",
        ) from exc
    cursor = int(body.proxmox_vmid_start)
    px_vc = cfg.get("proxmox") or {}
    default_storage_vc = str(px_vc.get("storage") or "local-lvm").strip() or "local-lvm"

    job_ids: list[str] = []
    with vcenter_connection(cfg) as si:
        for vm in iter_virtual_machines(si):
            if names_set is not None and vm.name not in names_set:
                continue
            if prefix is not None and not vm.name.startswith(prefix):
                continue

            ids, cursor = allocate_sequential_free_vmids(vcenter_slot, cursor, 1)
            vmid = ids[0]
            try:
                validate_job_vmid_and_storage(cfg, int(vmid), default_storage_vc)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            job_id = str(uuid.uuid4())
            if not template:
                raise HTTPException(
                    status_code=400,
                    detail="Set proxmox.vmdk_path_template in config.yaml (auto-esxi, or /imports/{vm_name}.vmdk, or esxi:DC/.../{vm_name}/...)",
                )
            try:
                vmdk = resolve_vmdk_import_path(
                    cfg, si, template, vm.name, proxmox_node=(str(node).strip() if node else None)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            now = utc_now_iso()
            mapping = {
                "job_id": job_id,
                "state": MigrationState.PENDING.value,
                "vm_name": vm.name,
                "proxmox_vmid": str(vmid),
                "proxmox_node": node,
                "vmdk_path_on_node": vmdk,
                "winrm_user": body.winrm_user,
                "winrm_password": body.winrm_password,
                "guest_ip": "",
                "error": "",
                "last_failed_stage": "",
                "created_at": now,
                "updated_at": now,
                "simulated_import": "0",
            }
            if not mapping["vmdk_path_on_node"]:
                raise HTTPException(
                    status_code=400,
                    detail="Set proxmox.vmdk_path_template in config.yaml (e.g. /imports/{vm_name}.vmdk or esxi:DC/.../{vm_name}/{vm_name}.vmdk)",
                )
            key = job_key(cfg, job_id)
            r.hset(key, mapping=mapping)
            r.rpush(cfg["redis"]["queue_key"], job_id)
            job_ids.append(job_id)

    return {"enqueued": len(job_ids), "job_ids": job_ids}


@api.get("/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str) -> JobRecord:
    cfg = load_config()
    r = redis_client()
    key = job_key(cfg, job_id)
    data = r.hgetall(key)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return _hash_to_record(data)


@api.post("/jobs/{job_id}/stop", response_model=JobRecord)
def stop_job(job_id: str) -> JobRecord:
    """Request worker cancellation; queued PENDING jobs may be finalised immediately."""
    cfg = load_config()
    r = redis_client()
    key = job_key(cfg, job_id)
    data = r.hgetall(key)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        state = MigrationState(data.get("state", MigrationState.PENDING.value))
    except ValueError:
        state = MigrationState.PENDING
    if state in (MigrationState.COMPLETED, MigrationState.FAILED, MigrationState.CANCELLED):
        r.hdel(key, "cancel_requested")
        return _hash_to_record(data)

    pipe = r.pipeline(transaction=True)
    pipe.hset(key, "cancel_requested", "1")
    pipe.lrem(cfg["redis"]["queue_key"], 0, job_id)
    results = pipe.execute()
    removed = int(results[-1]) if results else 0

    data = r.hgetall(key)
    if removed > 0 and data.get("state") == MigrationState.PENDING.value:
        mark_job_cancelled_operator(r, cfg, job_id, stage="PENDING")

    return _hash_to_record(r.hgetall(key))


@api.get("/jobs")
def list_jobs(state: str | None = None) -> list[JobRecord]:
    cfg = load_config()
    r = redis_client()
    prefix = cfg["redis"]["job_key_prefix"]
    out: list[JobRecord] = []
    for key in r.scan_iter(f"{prefix}*", count=100):
        if key.endswith(cfg["redis"]["job_log_suffix"]):
            continue
        data = r.hgetall(key)
        if not data:
            continue
        rec = _hash_to_record(data)
        if state is None or rec.state == state:
            out.append(rec)
    return sorted(out, key=lambda j: j.created_at or "")


@api.get("/jobs/{job_id}/log")
def get_job_log(job_id: str, limit: int = 200) -> list[dict[str, Any]]:
    cfg = load_config()
    r = redis_client()
    log_key = f"{job_key(cfg, job_id)}{cfg['redis']['job_log_suffix']}"
    lines = r.lrange(log_key, -limit, -1)
    parsed: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"raw": line})
    return parsed


def _optional_int(data: dict[str, str], key: str) -> int | None:
    v = data.get(key)
    if v is None or str(v).strip() == "":
        return None
    return int(v)


def _parse_job_bool_field(raw: str | None) -> bool | None:
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _hash_to_record(data: dict[str, str]) -> JobRecord:
    return JobRecord(
        job_id=data["job_id"],
        state=data.get("state", ""),
        vm_name=data.get("vm_name", ""),
        proxmox_vmid=int(data.get("proxmox_vmid", "0")),
        proxmox_node=data.get("proxmox_node", ""),
        vmdk_path_on_node=data.get("vmdk_path_on_node", ""),
        winrm_user=data.get("winrm_user", "Administrator"),
        proxmox_storage=data.get("proxmox_storage") or None,
        memory_mb=_optional_int(data, "memory_mb"),
        cores=_optional_int(data, "cores"),
        vlan=_optional_int(data, "vlan"),
        guest_ip=data.get("guest_ip") or None,
        error=data.get("error") or None,
        last_failed_stage=data.get("last_failed_stage") or None,
        created_at=data.get("created_at") or None,
        updated_at=data.get("updated_at") or None,
        simulated_import=_parse_job_bool_field(data.get("simulated_import")),
        simulated_clone_source_vmid=_optional_int(data, "simulated_clone_source_vmid"),
        qemu_cpu_preset=(data.get("qemu_cpu_preset") or "").strip() or None,
    )


@app.get("/health")
def health() -> JSONResponse:
    """
    Liveness / dependency check for the UI, Docker HEALTHCHECK, and load balancers.
    Returns **503** if config, Redis, or **Proxmox** is unhealthy.
    **VMware** is optional: reported in ``components`` but never causes 503.
    """
    components: dict[str, dict[str, Any]] = {}
    ok_all = True
    cfg: dict[str, Any] | None = None

    try:
        cfg = load_config()
        components["config"] = {"ok": True, "detail": "yaml loaded"}
    except Exception as exc:
        ok_all = False
        components["config"] = {"ok": False, "detail": str(exc)}

    if components.get("config", {}).get("ok"):
        try:
            r = redis_client()
            r.ping()
            components["redis"] = {"ok": True, "detail": "PING"}
        except Exception as exc:
            ok_all = False
            components["redis"] = {"ok": False, "detail": str(exc)}
    else:
        components["redis"] = {"ok": False, "detail": "skipped (config not loaded)"}
        ok_all = False

    if cfg is not None:
        px = ui_module.health_proxmox_component(cfg)
        components["proxmox"] = px
        if not px.get("ok"):
            ok_all = False
        components["vmware"] = ui_module.health_vmware_component(cfg)
    else:
        components["proxmox"] = {"ok": False, "detail": "skipped (config not loaded)"}
        components["vmware"] = {
            "ok": True,
            "detail": "skipped (config not loaded)",
            "optional": True,
        }

    body: dict[str, Any] = {
        "status": "ok" if ok_all else "degraded",
        "components": components,
    }
    return JSONResponse(status_code=200 if ok_all else 503, content=body)


@app.get("/version")
def app_version() -> dict[str, str]:
    """
    Public build metadata for the dashboard Help panel and operators.
    Optional: set ``MIGRATION_ENGINE_GIT_REVISION`` in the environment (e.g. CI / Docker build arg).
    """
    out: dict[str, str] = {"name": app.title, "version": app.version}
    rev = (os.environ.get("MIGRATION_ENGINE_GIT_REVISION") or "").strip()
    if rev:
        out["revision"] = rev
    return out


@app.get("/")
def dashboard() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Dashboard not found (static/index.html missing)")
    return FileResponse(index)


app.include_router(api)
app.include_router(ui_router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
