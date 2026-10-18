"""
FastAPI API for VMware → Proxmox migration orchestration (Redis state + queue).
Dashboard UI: session password auth, credential editing, connection tests.
"""

from __future__ import annotations

import json
import hashlib
import os
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
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
    qemu_vmids_in_use_cluster,
    qemu_vmids_in_use_on_node,
    resolve_esxi_import_permit_bucket,
)

try:
    from config_validate import validate_loaded_config
except ModuleNotFoundError:
    # Backward-compatibility fallback for old runtime bundles; keep app bootable.
    def validate_loaded_config(data: Any) -> dict[str, Any]:
        if data is None:
            raise ValueError("config file is empty")
        if not isinstance(data, dict):
            raise TypeError(f"config root must be a mapping, got {type(data).__name__}")
        return data

_cfg_path_override = (os.environ.get("MIGRATION_ENGINE_CONFIG") or "").strip()
CONFIG_PATH = (
    Path(_cfg_path_override).expanduser()
    if _cfg_path_override
    else Path(__file__).resolve().parent / "config.yaml"
)
STATIC_DIR = Path(__file__).resolve().parent / "static"

ui_module.CONFIG_PATH = CONFIG_PATH


@lru_cache(maxsize=1)
def _runtime_code_fingerprint() -> str:
    """
    Short SHA-256 fingerprint of key runtime files for operator-visible build identity.
    Helps verify the running code matches expected deployment even when semantic version is unchanged.
    """
    root = Path(__file__).resolve().parent
    files = (
        "main.py",
        "worker.py",
        "ui.py",
        "states.py",
        "config_validate.py",
        "static/index.html",
    )
    h = hashlib.sha256()
    for rel in files:
        p = root / rel
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except Exception:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()[:12]


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raise ValueError(f"config file is empty: {CONFIG_PATH}")
    return validate_loaded_config(raw)


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
    if getattr(vm, "snapshot", None) is not None:
        raise ui_module.SnapshotPresentError(
            f"VM {vm.name!r} still has VMware snapshot(s); consolidate/remove them in vSphere "
            "before migrating (importing now would copy stale, snapshot-time disk data)."
        )
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


def _assert_no_vmware_snapshots_esxi(node: str | None, names: list[str]) -> None:
    """
    Refuse enqueue when a source guest still has VMware snapshots on ESXi-linked
    storage. Importing the base ``<name>.vmdk`` of a snapshotted VM copies the disk
    as of the snapshot (current data lives in the ``-000001.vmdk`` delta chain), so
    the migration would silently import stale data. Detection is best-effort: on any
    listing/permission error the guard is skipped (fails open).
    """
    nod = (node or "").strip()
    if not nod:
        return
    try:
        dmap = ui_module.esxi_snapshot_delta_map(nod)
    except Exception:
        return
    if not dmap:
        return
    blocked: list[str] = []
    seen: set[str] = set()
    for raw in names:
        nm = (raw or "").strip()
        lk = nm.lower()
        if not nm or lk in seen:
            continue
        seen.add(lk)
        deltas = dmap.get(lk) or []
        if deltas:
            shown = ", ".join(d.rsplit("/", 1)[-1] for d in deltas[:3])
            more = "" if len(deltas) <= 3 else f", +{len(deltas) - 3} more"
            blocked.append(f"{nm} ({shown}{more})")
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to import: source VM(s) still have VMware snapshots. Importing now "
                "would copy stale, snapshot-time disk data instead of the current state. "
                "Consolidate/remove all snapshots in vSphere, then retry. Affected: "
                + "; ".join(blocked)
            ),
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
    preimported_disk_mode: bool = Field(
        default=False,
        description=(
            "If true, skip VMware/ESXi import-from. Expects VM shell with this VMID already exists on Proxmox "
            "and OS disk was attached/imported manually (e.g. qm importdisk)."
        ),
    )
    firmware: Literal["uefi", "seabios"] | None = Field(
        default=None,
        description="Guest firmware; None uses import-metadata / config default (uefi).",
    )
    guest_profile: Literal["windows", "linux"] | None = Field(
        default=None,
        description="Guest OS profile for ostype/vTPM defaults; None uses import-metadata / config (windows).",
    )
    enable_vtpm: bool | None = Field(
        default=None,
        description="Attach tpmstate0; None = auto (UEFI+windows only).",
    )
    qemu_cpu_preset: Literal["host_no_nested", "host_nested_vbs"] | None = Field(
        default=None,
        description=(
            "Clears PVE cpu= and sets qemu args to a fixed -cpu host profile (nested/VBS variants). "
            "None keeps the imported/template CPU config as-is (no forced cpu= default)."
        ),
    )
    take_snapshot: bool = Field(
        default=True,
        description="If true, create a pre-boot snapshot before first guest startup checks.",
    )


class VmJobOverride(BaseModel):
    """Per-VM firmware / resources for migrate-selected (keyed by VM name in vm_overrides)."""

    guest_profile: Literal["windows", "linux"] | None = None
    firmware: Literal["uefi", "seabios"] | None = None
    enable_vtpm: bool | None = None
    memory_mb: int | None = Field(default=None, ge=512, le=1048576)
    cores: int | None = Field(default=None, ge=1, le=256)
    vlan: int | str | None = Field(
        default=None,
        description="Optional VLAN tag (1-4094). Empty / null = untagged for this VM.",
    )


# Backward-compatible alias
VmFirmwareOverride = VmJobOverride


class MigrateSelectedRequest(BaseModel):
    """Enqueue one migration job per VM name (must exist on PVE ESXi listing / vCenter)."""

    vm_names: list[str] = Field(..., min_length=1)
    proxmox_vmid_start: int = Field(..., ge=100, le=999999999)
    proxmox_storage: str = Field(..., min_length=1, description="PVE storage id, e.g. local-lvm")
    memory_mb: int = Field(
        ...,
        ge=512,
        le=1048576,
        description="Batch default RAM (MiB); per-VM vm_overrides.memory_mb / import-metadata win when set.",
    )
    cores: int = Field(
        ...,
        ge=1,
        le=256,
        description="Batch default vCPUs; per-VM vm_overrides.cores / import-metadata win when set.",
    )
    vlan: int | str | None = Field(
        default=None,
        description="Batch default VLAN tag (1-4094); per-VM vm_overrides.vlan wins when provided.",
    )
    proxmox_node: str | None = None
    import_mode: Literal["standard", "nfs"] = Field(
        default="standard",
        description=(
            "Disk source mode: 'standard' uses proxmox.vmdk_path_template/auto-esxi behavior, "
            "'nfs' uses nfs_vmdk_path_template from request."
        ),
    )
    nfs_vmdk_path_template: str | None = Field(
        default=None,
        description="Required when import_mode='nfs'; include {vm_name} placeholder.",
    )
    firmware_mode: Literal["importer", "override"] = Field(
        default="importer",
        description=(
            "importer: resolve firmware/profile/vTPM per VM from PVE import-metadata. "
            "override: use vm_overrides firmware cells with importer/config fallbacks for missing cells. "
            "Resource fields in vm_overrides (memory/cores/vlan) apply in both modes."
        ),
    )
    vm_overrides: dict[str, VmJobOverride] | None = Field(
        default=None,
        description="Per-VM resource and/or firmware overrides (keyed by VM name).",
    )
    qemu_cpu_preset: Literal["host_no_nested", "host_nested_vbs"] | None = Field(
        default=None,
        description="Custom qemu -cpu via args=; None keeps the imported/template CPU config as-is.",
    )
    take_snapshot: bool = Field(
        default=True,
        description="If true, create a pre-boot snapshot before first guest startup checks.",
    )


class EfiRedoEnqueueRequest(BaseModel):
    """
    Enqueue EFI-redo workflow for migrated VMs that still carry the pending tag.
    If vm_names is omitted, all matching tagged QEMU VMs in the cluster are targeted.
    """

    vm_names: list[str] | None = Field(default=None, description="Optional subset by VM name")
    proxmox_storage: str | None = Field(
        default=None,
        description="Storage for newly recreated efidisk0; default from proxmox.storage",
    )


class JobRecord(BaseModel):
    job_id: str
    state: str
    vm_name: str
    proxmox_vmid: int
    proxmox_node: str
    vmdk_path_on_node: str
    proxmox_storage: str | None = None
    memory_mb: int | None = None
    cores: int | None = None
    vlan: int | None = None
    guest_ip: str | None = None
    error: str | None = None
    last_failed_stage: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    preimported_disk_mode: bool | None = None
    firmware: str | None = None
    guest_profile: str | None = None
    enable_vtpm: bool | None = None
    qemu_cpu_preset: str | None = None
    take_snapshot: bool | None = None
    import_mode: str | None = None
    vmware_api_host: str | None = None
    import_serialization_bucket: str | None = None
    import_parallelism_summary: str | None = None
    import_parallelism_detail: str | None = None
    import_permit_state: str | None = None
    import_permit_bucket: str | None = None
    import_activity_status: str | None = None
    job_kind: str | None = None


class VCenterEnqueueFilters(BaseModel):
    names: list[str] | None = None
    name_prefix: str | None = None


class VCenterEnqueueRequest(VCenterEnqueueFilters):
    """Each matched VM gets a job; you must supply proxmox_vmid_start (incremented per VM)."""

    proxmox_vmid_start: int = Field(..., ge=100)
    proxmox_node: str | None = None


_cfg_boot = load_config()
_ui = _cfg_boot.get("ui") or {}
_session_secret = (_ui.get("session_secret") or "").strip()
if not _session_secret:
    raise RuntimeError(
        "config.yaml must define ui.session_secret (random string) and ui.password (dashboard password)"
    )

app = FastAPI(title="VMware → Proxmox migration orchestrator", version="2.2.1")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=False,
    max_age=14 * 24 * 3600,
)

api = APIRouter(dependencies=[Depends(require_ui_session)])

_TERMINAL_JOB_STATES = frozenset(
    {
        MigrationState.COMPLETED.value,
        MigrationState.FAILED.value,
        MigrationState.CANCELLED.value,
    }
)


def _iter_job_record_raw(r: redis.Redis, cfg: dict[str, Any]) -> Iterator[dict[str, str]]:
    """Yield raw job hashes (excluding log list keys)."""
    prefix = cfg["redis"]["job_key_prefix"]
    log_suffix = cfg["redis"]["job_log_suffix"]
    for key in r.scan_iter(f"{prefix}*", count=100):
        if key.endswith(log_suffix):
            continue
        data = r.hgetall(key)
        if not data or not data.get("job_id"):
            continue
        yield data


def _job_row_sort_ts(data: dict[str, str]) -> str:
    return (data.get("updated_at") or data.get("created_at") or "").strip()


def _active_migration_index_by_vm_name(
    r: redis.Redis, cfg: dict[str, Any]
) -> dict[str, dict[str, str]]:
    """
    Latest non-terminal job by VM name (lowercased key).
    Used to block concurrent migration of the same VM.
    """
    out: dict[str, dict[str, str]] = {}
    for data in _iter_job_record_raw(r, cfg):
        vm = (data.get("vm_name") or "").strip()
        st = (data.get("state") or "").strip()
        if not vm or not st or st in _TERMINAL_JOB_STATES:
            continue
        lk = vm.lower()
        prev = out.get(lk)
        if prev is None or _job_row_sort_ts(data) >= _job_row_sort_ts(prev):
            out[lk] = data
    return out


def _assert_no_active_migration_for_vm_names(
    names: list[str],
    r: redis.Redis,
    cfg: dict[str, Any],
    active_by_lower: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    idx = active_by_lower if active_by_lower is not None else _active_migration_index_by_vm_name(r, cfg)
    blocked: list[str] = []
    seen: set[str] = set()
    for raw in names:
        nm = (raw or "").strip()
        if not nm:
            continue
        lk = nm.lower()
        if lk in seen:
            continue
        seen.add(lk)
        j = idx.get(lk)
        if not j:
            continue
        jst = (j.get("state") or "").strip() or "UNKNOWN"
        jid = (j.get("job_id") or "").strip() or "?"
        blocked.append(f"{nm} ({jst}, job {jid})")
    if blocked:
        raise HTTPException(
            status_code=409,
            detail=(
                "Migration already in progress for: "
                + ", ".join(blocked)
                + ". Wait until the active job reaches COMPLETED, FAILED, or CANCELLED."
            ),
        )
    return idx


def _normalize_pve_tags(raw: Any) -> list[str]:
    out: list[str] = []
    if raw is None:
        return out
    if isinstance(raw, (list, tuple, set)):
        vals = [str(x or "").strip() for x in raw]
    else:
        vals = [x for x in str(raw).replace(",", ";").replace(" ", ";").split(";")]
    seen: set[str] = set()
    for v in vals:
        s = str(v or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _cluster_tagged_qemu_rows(
    proxmox: Any, *, required_tags_lower: set[str]
) -> list[dict[str, Any]]:
    raw: Any = proxmox.cluster.resources.get(type="vm")
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type") or "").strip().lower()
        if typ and typ != "qemu":
            continue
        vmid_raw = row.get("vmid")
        node = str(row.get("node") or "").strip()
        name = str(row.get("name") or "").strip()
        if vmid_raw is None or not node:
            continue
        try:
            vmid = int(vmid_raw)
        except (TypeError, ValueError):
            continue
        tags = _normalize_pve_tags(row.get("tags"))
        lower = {t.lower() for t in tags}
        if required_tags_lower and not required_tags_lower.issubset(lower):
            continue
        out.append(
            {
                "vmid": vmid,
                "node": node,
                "name": name or f"vm-{vmid}",
                "tags": tags,
            }
        )
    return out


def _merge_migration_history_into_vm_rows(
    rows: list[dict[str, Any]], r: redis.Redis, cfg: dict[str, Any]
) -> None:
    """
    Attach prior / active migration hints per guest name (case-insensitive match to job vm_name).

    Fields on each row (only when applicable):
    - migration_active_state, migration_active_job_id — newest non-terminal job
    - migration_last_terminal_state, migration_last_terminal_at, migration_last_job_id
    - migration_last_error_preview — short text when last terminal was FAILED
    """
    by_lower: dict[str, list[dict[str, str]]] = defaultdict(list)
    for data in _iter_job_record_raw(r, cfg):
        vm = (data.get("vm_name") or "").strip()
        if not vm:
            continue
        by_lower[vm.lower()].append(data)

    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        jobs = by_lower.get(name.lower(), [])
        if not jobs:
            continue
        jobs_sorted = sorted(jobs, key=_job_row_sort_ts, reverse=True)
        for j in jobs_sorted:
            st = (j.get("state") or "").strip()
            if st and st not in _TERMINAL_JOB_STATES:
                row["migration_active_state"] = st
                row["migration_active_job_id"] = j.get("job_id", "")
                break
        for j in jobs_sorted:
            st = (j.get("state") or "").strip()
            if st in _TERMINAL_JOB_STATES:
                row["migration_last_terminal_state"] = st
                row["migration_last_terminal_at"] = (
                    (j.get("updated_at") or j.get("created_at") or "").strip()
                )
                row["migration_last_job_id"] = j.get("job_id", "")
                err = (j.get("error") or "").strip()
                if err:
                    row["migration_last_error_preview"] = (
                        err[:160] + ("…" if len(err) > 160 else "")
                    )
                break


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
                        "esxi_storage_ids": [],
                        "esxi_import_uri": None,
                        "esxi_listing_size_bytes": None,
                    }
        return sorted(by_lower.values(), key=lambda x: x["name"].lower())
    except Exception:
        return sorted(pve_rows, key=lambda x: x["name"].lower())


@api.get("/vms")
def list_vms_for_migrate() -> list[dict[str, Any]]:
    cfg = load_config()
    rows = _vm_inventory_merge(cfg)
    try:
        r = redis_client()
        _merge_migration_history_into_vm_rows(rows, r, cfg)
    except Exception:
        pass
    return rows


def _migration_default_net_mtu(px: dict[str, Any]) -> int:
    try:
        nm = int(px.get("net_mtu", 1500))
    except (TypeError, ValueError):
        return 1500
    return nm if nm > 0 else 1500


def _norm_enqueue_firmware(raw: str | None) -> str:
    k = (raw or "").strip().lower()
    if k in ("uefi", "ovmf", "efi"):
        return "uefi"
    if k in ("seabios", "bios", "legacy"):
        return "seabios"
    return ""


def _norm_enqueue_profile(raw: str | None) -> str:
    k = (raw or "").strip().lower()
    if k in ("windows", "win"):
        return "windows"
    if k in ("linux", "l26", "other"):
        return "linux"
    return ""


def _auto_enable_vtpm(firmware: str, profile: str) -> bool:
    return firmware == "uefi" and profile == "windows"


def _resolve_vm_firmware_fields(
    *,
    cfg: dict[str, Any],
    vm_name: str,
    importer_hint: dict[str, Any] | None,
    override: VmFirmwareOverride | None,
    firmware_mode: str,
    body_firmware: str | None = None,
    body_profile: str | None = None,
    body_enable_vtpm: bool | None = None,
) -> tuple[str, str, bool]:
    """
    Resolve (firmware, guest_profile, enable_vtpm) for one enqueue.
    Priority: explicit override/body → import-metadata hint → config defaults.
    """
    px = cfg.get("proxmox") or {}
    hint = importer_hint or {}
    use_override = (firmware_mode or "importer").strip().lower() == "override"

    firmware = ""
    profile = ""
    vtpm_set: bool | None = None

    if use_override and override is not None:
        firmware = _norm_enqueue_firmware(override.firmware)
        profile = _norm_enqueue_profile(override.guest_profile)
        if override.enable_vtpm is not None:
            vtpm_set = bool(override.enable_vtpm)

    if not firmware:
        firmware = _norm_enqueue_firmware(body_firmware)
    if not profile:
        profile = _norm_enqueue_profile(body_profile)
    if vtpm_set is None and body_enable_vtpm is not None:
        vtpm_set = bool(body_enable_vtpm)

    if not firmware:
        firmware = _norm_enqueue_firmware(str(hint.get("firmware") or ""))
    if not profile:
        profile = _norm_enqueue_profile(str(hint.get("guest_profile") or ""))

    if not firmware:
        firmware = _norm_enqueue_firmware(str(px.get("default_firmware") or "")) or "uefi"
    if not profile:
        profile = _norm_enqueue_profile(str(px.get("default_guest_profile") or "")) or "windows"

    if vtpm_set is None:
        cfg_tpm = px.get("default_enable_vtpm")
        if cfg_tpm is True:
            vtpm_set = True
        elif cfg_tpm is False:
            vtpm_set = False
        elif "enable_vtpm_suggested" in hint and hint.get("enable_vtpm_suggested") is not None:
            vtpm_set = bool(hint.get("enable_vtpm_suggested"))
        else:
            vtpm_set = _auto_enable_vtpm(firmware, profile)

    return firmware, profile, bool(vtpm_set)


def _firmware_fields_to_mapping(firmware: str, profile: str, enable_vtpm: bool) -> dict[str, str]:
    return {
        "firmware": firmware,
        "guest_profile": profile,
        "enable_vtpm": "1" if enable_vtpm else "0",
    }


def _resolve_vm_resources(
    *,
    body_memory_mb: int,
    body_cores: int,
    body_vlan: int | str | None,
    importer_hint: dict[str, Any] | None,
    override: VmJobOverride | None,
) -> tuple[int, int, str | None]:
    """
    Resolve (memory_mb, cores, vlan_str_or_none) for one enqueue.
    Priority: explicit per-VM override → import-metadata hint → batch body defaults.
    VLAN is never taken from import-metadata (not in VMX create-args for our use).
    """
    hint = importer_hint or {}
    memory_mb = int(body_memory_mb)
    cores = int(body_cores)
    if override is not None and override.memory_mb is not None:
        memory_mb = int(override.memory_mb)
    else:
        try:
            hm = int(hint["memory_mb"]) if hint.get("memory_mb") is not None else None
        except (TypeError, ValueError):
            hm = None
        if hm is not None and hm >= 512:
            memory_mb = hm

    if override is not None and override.cores is not None:
        cores = int(override.cores)
    else:
        try:
            hc = int(hint["cores"]) if hint.get("cores") is not None else None
        except (TypeError, ValueError):
            hc = None
        if hc is not None and hc >= 1:
            cores = hc

    vlan_out: str | None = None
    if override is not None and "vlan" in override.model_fields_set:
        raw = override.vlan
        if raw is not None and str(raw).strip() != "":
            vlan_out = str(raw).strip()
    elif body_vlan is not None and str(body_vlan).strip() != "":
        vlan_out = str(body_vlan).strip()

    memory_mb = max(512, min(1048576, memory_mb))
    cores = max(1, min(256, cores))
    return memory_mb, cores, vlan_out


@api.get("/migration-defaults")
def migration_defaults(proxmox_node: str | None = None) -> dict[str, Any]:
    """Defaults for the migrate dialog (from config.yaml proxmox section)."""
    cfg = load_config()
    p = cfg.get("proxmox") or {}
    tmpl = (p.get("vmdk_path_template") or "").strip()
    def_fw = _norm_enqueue_firmware(str(p.get("default_firmware") or "")) or "uefi"
    def_prof = _norm_enqueue_profile(str(p.get("default_guest_profile") or "")) or "windows"
    out: dict[str, Any] = {
        "storage": p.get("storage", "local-lvm"),
        "memory_mb": int(p.get("memory_mb", 16384)),
        "cores": int(p.get("cores", 6)),
        "default_node": p.get("default_node", ""),
        "bridge": p.get("bridge", "vmbr0"),
        "vlan": p.get("vlan", ""),
        "net_mtu": _migration_default_net_mtu(p),
        "vmdk_path_template": tmpl,
        "vmdk_template_auto_esxi": tmpl.lower() in _AUTO_ESXI_TEMPLATES,
        "vmdk_auto_esxi_prefers_pve": True,
        "esxi_import_parent_hint": (p.get("esxi_import_parent_hint") or "").strip(),
        "default_firmware": def_fw,
        "default_guest_profile": def_prof,
        "default_enable_vtpm": p.get("default_enable_vtpm"),
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
            used = qemu_vmids_in_use_cluster(connect_proxmox(cfg))
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
    _assert_no_active_migration_for_vm_names([body.vm_name], r, cfg)
    job_id = str(uuid.uuid4())
    node = body.proxmox_node or cfg["proxmox"]["default_node"]
    if not node or not str(node).strip():
        raise HTTPException(
            status_code=400,
            detail="proxmox.default_node or proxmox_node is required to verify VM ID availability",
        )
    px = cfg.get("proxmox") or {}
    use_preimported = bool(body.preimported_disk_mode)
    if not use_preimported:
        _assert_no_vmware_snapshots_esxi(node, [body.vm_name])
    template = (px.get("vmdk_path_template") or "").strip()
    vmdk = (body.vmdk_path_on_node or "").strip()
    if use_preimported:
        vmdk = "preimported:manual"
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
        px_api = connect_proxmox(cfg)
        used_cluster = qemu_vmids_in_use_cluster(px_api)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot verify Proxmox VM IDs in cluster: {exc}",
        ) from exc
    if use_preimported:
        try:
            used_on_node = qemu_vmids_in_use_on_node(px_api, nod)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Cannot verify existing VMIDs on Proxmox node {nod!r}: {exc}",
            ) from exc
        if int(body.proxmox_vmid) not in used_on_node:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"preimported_disk_mode=true requires an existing VM on node {nod!r}; "
                    f"VMID {body.proxmox_vmid} was not found."
                ),
            )
    else:
        if int(body.proxmox_vmid) in used_cluster:
            alt_ids, _ = allocate_sequential_free_vmids(set(used_cluster), int(body.proxmox_vmid), 1)
            alt = alt_ids[0]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Proxmox VM ID {body.proxmox_vmid} is already in use in this cluster. "
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
    mapping["preimported_disk_mode"] = "1" if use_preimported else "0"
    mapping["take_snapshot"] = "1" if body.take_snapshot else "0"
    hints = ui_module.resolve_guest_import_hints(cfg, str(node).strip(), [body.vm_name])
    fw, prof, vtpm = _resolve_vm_firmware_fields(
        cfg=cfg,
        vm_name=body.vm_name,
        importer_hint=hints.get(body.vm_name),
        override=None,
        firmware_mode="override" if (body.firmware or body.guest_profile or body.enable_vtpm is not None) else "importer",
        body_firmware=body.firmware,
        body_profile=body.guest_profile,
        body_enable_vtpm=body.enable_vtpm,
    )
    mapping.update(_firmware_fields_to_mapping(fw, prof, vtpm))
    if body.qemu_cpu_preset is not None:
        mapping["qemu_cpu_preset"] = body.qemu_cpu_preset
    key = job_key(cfg, job_id)
    r.hset(key, mapping=mapping)
    r.rpush(cfg["redis"]["queue_key"], job_id)
    return _job_record_for_api(cfg, r.hgetall(key))


@api.post("/jobs/migrate-selected")
def migrate_selected(body: MigrateSelectedRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    px = cfg.get("proxmox") or {}
    import_mode = body.import_mode
    template = (px.get("vmdk_path_template") or "").strip()
    nfs_template = (body.nfs_vmdk_path_template or "").strip()
    if import_mode == "nfs":
        if not nfs_template:
            raise HTTPException(
                status_code=400,
                detail="import_mode='nfs' requires nfs_vmdk_path_template",
            )
        if "{vm_name}" not in nfs_template:
            raise HTTPException(
                status_code=400,
                detail="nfs_vmdk_path_template must include '{vm_name}' placeholder",
            )
    elif not template:
        raise HTTPException(
            status_code=400,
            detail="Set proxmox.vmdk_path_template in config.yaml (auto-esxi, or /imports/{vm_name}.vmdk, or esxi:DC/.../{vm_name}/...)",
        )
    node = body.proxmox_node or px.get("default_node")
    if not node:
        raise HTTPException(status_code=400, detail="proxmox.default_node or proxmox_node is required")

    ordered: list[str] = []
    seen: set[str] = set()
    for raw in body.vm_names:
        name = raw.strip()
        lk = name.lower()
        if not name or lk in seen:
            continue
        seen.add(lk)
        ordered.append(name)
    if not ordered:
        raise HTTPException(status_code=400, detail="No valid VM names after deduplication")
    active_by_lower = _assert_no_active_migration_for_vm_names(ordered, r, cfg)

    job_ids: list[str] = []
    storage = body.proxmox_storage.strip()
    node_str = str(node).strip()
    if import_mode == "standard":
        _assert_no_vmware_snapshots_esxi(node_str, ordered)
    proxmox = connect_proxmox(cfg)
    try:
        in_use = qemu_vmids_in_use_cluster(proxmox)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot list QEMU VMIDs in Proxmox cluster: {exc}",
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

    auto_esxi = import_mode == "standard" and template.lower() in _AUTO_ESXI_TEMPLATES
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
    if auto_esxi:
        for n in ordered:
            hit = pve_uri_by_name.get(n.lower()) or ui_module.discover_esxi_import_from_pve(
                node_str, n
            )
            if hit:
                pve_only_paths[n] = hit

    firmware_mode = (body.firmware_mode or "importer").strip().lower()
    if firmware_mode not in ("importer", "override"):
        raise HTTPException(status_code=400, detail="firmware_mode must be 'importer' or 'override'")
    overrides_in = body.vm_overrides or {}
    # Case-insensitive lookup for override keys (resources always; firmware when override mode)
    overrides_by_lower: dict[str, VmJobOverride] = {
        str(k).strip().lower(): v for k, v in overrides_in.items() if str(k).strip()
    }
    importer_hints = ui_module.resolve_guest_import_hints(cfg, node_str, ordered)

    def enqueue_one(name: str, vmdk: str, assigned_vmid: int) -> None:
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        ov = overrides_by_lower.get(name.lower())
        fw_ov = ov if firmware_mode == "override" else None
        hint = importer_hints.get(name)
        fw, prof, vtpm = _resolve_vm_firmware_fields(
            cfg=cfg,
            vm_name=name,
            importer_hint=hint,
            override=fw_ov,
            firmware_mode=firmware_mode,
        )
        mem_mb, n_cores, vlan_s = _resolve_vm_resources(
            body_memory_mb=body.memory_mb,
            body_cores=body.cores,
            body_vlan=body.vlan,
            importer_hint=hint,
            override=ov,
        )
        mapping = {
            "job_id": job_id,
            "state": MigrationState.PENDING.value,
            "vm_name": name,
            "proxmox_vmid": str(assigned_vmid),
            "proxmox_node": node,
            "vmdk_path_on_node": vmdk,
            "proxmox_storage": storage,
            "memory_mb": str(mem_mb),
            "cores": str(n_cores),
            "guest_ip": "",
            "error": "",
            "last_failed_stage": "",
            "created_at": now,
            "updated_at": now,
            "import_mode": import_mode,
            "take_snapshot": "1" if body.take_snapshot else "0",
            **_firmware_fields_to_mapping(fw, prof, vtpm),
        }
        if import_mode == "nfs" and nfs_template:
            mapping["nfs_vmdk_path_template"] = nfs_template
        if vlan_s is not None:
            mapping["vlan"] = vlan_s
        if body.qemu_cpu_preset is not None:
            mapping["qemu_cpu_preset"] = body.qemu_cpu_preset
        key = job_key(cfg, job_id)
        r.hset(key, mapping=mapping)
        r.rpush(cfg["redis"]["queue_key"], job_id)
        active_by_lower[name.lower()] = {
            "job_id": job_id,
            "vm_name": name,
            "state": MigrationState.PENDING.value,
            "created_at": now,
            "updated_at": now,
        }
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

    if import_mode == "nfs":
        for name, vid in zip(ordered, allocated_ids, strict=True):
            try:
                vmdk = nfs_template.format(vm_name=name)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid nfs_vmdk_path_template for {name!r}: {exc}",
                ) from exc
            enqueue_one(name, vmdk, vid)
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


@api.post("/jobs/efi-redo-pending")
def enqueue_efi_redo_pending(body: EfiRedoEnqueueRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    px = cfg.get("proxmox") or {}
    pending_tag = str(px.get("efi_redo_pending_tag") or "efi-redo-pending").strip().lower()
    migrated_tag = str(px.get("efi_redo_migrated_tag") or "migrated").strip().lower()
    required = {t for t in (pending_tag, migrated_tag) if t}
    if pending_tag == "":
        raise HTTPException(status_code=400, detail="proxmox.efi_redo_pending_tag cannot be empty")

    proxmox = connect_proxmox(cfg)
    try:
        tagged = _cluster_tagged_qemu_rows(proxmox, required_tags_lower=required)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot list tagged QEMU guests in Proxmox cluster: {exc}",
        ) from exc

    selected: list[dict[str, Any]] = tagged
    if body.vm_names:
        wanted = {x.strip().lower() for x in body.vm_names if x and x.strip()}
        if not wanted:
            raise HTTPException(status_code=400, detail="vm_names provided but empty after normalization")
        selected = [x for x in tagged if x["name"].lower() in wanted]
        misses = sorted(wanted.difference({x["name"].lower() for x in selected}))
        if misses:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Some vm_names are missing required tags "
                    f"({migrated_tag} + {pending_tag}) or do not exist: {misses}"
                ),
            )
    if not selected:
        return {"enqueued": 0, "job_ids": [], "matched": 0, "required_tags": sorted(required)}

    vm_names = [x["name"] for x in selected]
    active_by_lower = _assert_no_active_migration_for_vm_names(vm_names, r, cfg)
    store = (body.proxmox_storage or px.get("storage") or "").strip() or "local-lvm"
    try:
        for row in selected:
            validate_job_vmid_and_storage(cfg, int(row["vmid"]), store)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_ids: list[str] = []
    for row in selected:
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        mapping: dict[str, str] = {
            "job_id": job_id,
            "job_kind": "efi_redo",
            "state": MigrationState.PENDING.value,
            "vm_name": row["name"],
            "proxmox_vmid": str(int(row["vmid"])),
            "proxmox_node": row["node"],
            "vmdk_path_on_node": "efi-redo:tagged",
            "proxmox_storage": store,
            "guest_ip": "",
            "error": "",
            "last_failed_stage": "",
            "created_at": now,
            "updated_at": now,
            "preimported_disk_mode": "0",
            "take_snapshot": "0",
            "firmware": "uefi",
            "guest_profile": "windows",
            "enable_vtpm": "0",
        }
        key = job_key(cfg, job_id)
        r.hset(key, mapping=mapping)
        r.rpush(cfg["redis"]["queue_key"], job_id)
        active_by_lower[row["name"].lower()] = {
            "job_id": job_id,
            "vm_name": row["name"],
            "state": MigrationState.PENDING.value,
            "created_at": now,
            "updated_at": now,
        }
        job_ids.append(job_id)

    return {
        "enqueued": len(job_ids),
        "job_ids": job_ids,
        "matched": len(selected),
        "required_tags": sorted(required),
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
    active_by_lower = _active_migration_index_by_vm_name(r, cfg)
    names_set = set(body.names) if body.names else None
    prefix = body.name_prefix
    node = body.proxmox_node or cfg["proxmox"]["default_node"]
    template = (cfg["proxmox"].get("vmdk_path_template") or "").strip()
    node_str = str(node).strip()
    proxmox = connect_proxmox(cfg)
    try:
        vcenter_slot = qemu_vmids_in_use_cluster(proxmox)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot list QEMU VMIDs in Proxmox cluster: {exc}",
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
            _assert_no_active_migration_for_vm_names([vm.name], r, cfg, active_by_lower)

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
                "guest_ip": "",
                "error": "",
                "last_failed_stage": "",
                "created_at": now,
                "updated_at": now,
            }
            if not mapping["vmdk_path_on_node"]:
                raise HTTPException(
                    status_code=400,
                    detail="Set proxmox.vmdk_path_template in config.yaml (e.g. /imports/{vm_name}.vmdk or esxi:DC/.../{vm_name}/{vm_name}.vmdk)",
                )
            vc_hints = ui_module.resolve_guest_import_hints(cfg, node_str, [vm.name])
            fw, prof, vtpm = _resolve_vm_firmware_fields(
                cfg=cfg,
                vm_name=vm.name,
                importer_hint=vc_hints.get(vm.name),
                override=None,
                firmware_mode="importer",
            )
            mapping.update(_firmware_fields_to_mapping(fw, prof, vtpm))
            key = job_key(cfg, job_id)
            r.hset(key, mapping=mapping)
            r.rpush(cfg["redis"]["queue_key"], job_id)
            active_by_lower[vm.name.lower()] = {
                "job_id": job_id,
                "vm_name": vm.name,
                "state": MigrationState.PENDING.value,
                "created_at": now,
                "updated_at": now,
            }
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
    return _job_record_for_api(cfg, data)


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
        return _job_record_for_api(cfg, data)

    pipe = r.pipeline(transaction=True)
    pipe.hset(key, "cancel_requested", "1")
    pipe.lrem(cfg["redis"]["queue_key"], 0, job_id)
    results = pipe.execute()
    removed = int(results[-1]) if results else 0

    data = r.hgetall(key)
    if removed > 0 and data.get("state") == MigrationState.PENDING.value:
        mark_job_cancelled_operator(r, cfg, job_id, stage="PENDING")

    return _job_record_for_api(cfg, r.hgetall(key))


@api.get("/jobs")
def list_jobs(state: str | None = None) -> list[JobRecord]:
    cfg = load_config()
    r = redis_client()
    out: list[JobRecord] = []
    for data in _iter_job_record_raw(r, cfg):
        rec = _job_record_for_api(cfg, data)
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


def _job_parallelism_api_fields(
    cfg: dict[str, Any], data: dict[str, str], rec: JobRecord
) -> dict[str, Any]:
    """UI hints: VMware endpoint, import path, and worker import-permit bucket (parallelism)."""
    if (data.get("job_kind") or "").strip().lower() == "efi_redo":
        return {
            "vmware_api_host": None,
            "import_serialization_bucket": None,
            "import_parallelism_summary": "EFI redo workflow",
            "import_parallelism_detail": "Post-migration EFI redo workflow for tagged VMs.",
            "import_permit_state": None,
            "import_permit_bucket": None,
            "import_activity_status": "EFI redo queued/running",
        }
    vmdk = (data.get("vmdk_path_on_node") or "").strip()
    pre = rec.preimported_disk_mode is True
    wcfg = cfg.get("worker") or {}
    try:
        mc = int(wcfg.get("max_concurrent_imports_per_esxi_storage", 1) or 1)
    except (TypeError, ValueError):
        mc = 1
    mc = max(1, mc)
    vmware_h = str((cfg.get("vmware") or {}).get("host") or "").strip() or None

    bucket: str | None = None
    if not pre:
        bucket = resolve_esxi_import_permit_bucket(cfg, vmdk)
    permit_state = (data.get("import_permit_state") or "").strip().upper() or None

    if pre:
        summary = "Preimported disk"
    elif bucket == "esxi_legacy_uri":
        summary = f"VMware URI pool · ≤{mc} concurrent"
    elif bucket and str(bucket).lower().startswith("esxi:"):
        summary = f"ESXi URI bucket {bucket} · ≤{mc} concurrent"
    elif bucket:
        summary = f"PVE ESXi store {bucket} · ≤{mc} concurrent"
    else:
        summary = "No storage queue (parallel)"

    activity: str | None = None
    if rec.state == MigrationState.IMPORTING.value:
        if permit_state == "WAITING" and bucket:
            activity = f"Waiting: bucket {bucket} is busy"
        elif permit_state == "ACQUIRED" and bucket:
            activity = f"Import running: bucket {bucket}"
        elif bucket:
            activity = f"Import stage: bucket {bucket}"
        else:
            activity = "Import running (no bucket serialization)"
    elif permit_state == "RELEASED" and bucket:
        activity = f"Import permit released: bucket {bucket}"

    parts: list[str] = []
    if vmware_h:
        parts.append(f"vCenter/ESXi API host: {vmware_h}")
    pn = (data.get("proxmox_node") or "").strip()
    pvm = (data.get("proxmox_vmid") or "").strip()
    parts.append(f"Proxmox target: node {pn or '—'} · VMID {pvm or '—'}")
    if vmdk:
        vd = vmdk if len(vmdk) <= 220 else vmdk[:217] + "…"
        parts.append(f"Import path: {vd}")
    fw = (data.get("firmware") or "").strip()
    gp = (data.get("guest_profile") or "").strip()
    if fw or gp:
        parts.append(f"firmware={fw or '—'} profile={gp or '—'}")
    im = (data.get("import_mode") or "").strip()
    if im:
        parts.append(f"import_mode {im}")
    if pre:
        parts.append("Preimported: attach existing disk; no VMware import-from permit.")
    elif permit_state == "WAITING" and bucket:
        parts.append(f"Current queue state: waiting for ESXi import permit on bucket {bucket}.")
    elif permit_state == "ACQUIRED" and bucket:
        parts.append(f"Current queue state: import permit acquired on bucket {bucket} (disk import running).")
    elif permit_state == "RELEASED" and bucket:
        parts.append(f"Current queue state: permit released for bucket {bucket} (import step finished).")
    elif bucket == "esxi_legacy_uri":
        parts.append(
            f"Legacy esxi:… URIs share one global permit pool (cap {mc}); only that many "
            "VMware import-from operations at once across all workers. "
            "Permit is held only for the disk pull, not the full migration."
        )
    elif bucket and str(bucket).lower().startswith("esxi:"):
        parts.append(
            f"``esxi:…`` import paths are bucketed by datacenter + datastore segment ({bucket}); "
            f"jobs sharing this bucket are limited to {mc} concurrent import-from operations. "
            "Use PVE volids ``<pve-esxi-storage-id>:…`` if you need one permit pool per linked Proxmox ESXi storage id."
        )
    elif bucket:
        parts.append(
            f"Same PVE ESXi linked-storage id ⇒ same permit bucket (cap {mc}). "
            "Matching bucket names in the jobs list share that limit. "
            "The permit applies only while Proxmox runs import-from (disk pull), not for the whole job — "
            "later stages (boot, guest-agent checks, etc.) can overlap between VMs."
        )
    else:
        parts.append(
            "This path is not gated by the worker’s per–ESXi-storage permit. "
            "Multiple jobs may import in parallel unless VMware or Proxmox limits apply elsewhere. "
            "If you expected a bucket here, check import path shape (needs PVE volid like storageid:…/… or storageid:….vmdk) "
            f"and worker.max_concurrent_imports_per_esxi_storage (currently {mc})."
        )
    detail = " ".join(parts)
    return {
        "vmware_api_host": vmware_h,
        "import_serialization_bucket": bucket,
        "import_parallelism_summary": summary,
        "import_parallelism_detail": detail,
        "import_permit_state": permit_state,
        "import_permit_bucket": bucket,
        "import_activity_status": activity,
    }


def _hash_to_record(data: dict[str, str]) -> JobRecord:
    return JobRecord(
        job_id=data["job_id"],
        state=data.get("state", ""),
        vm_name=data.get("vm_name", ""),
        proxmox_vmid=int(data.get("proxmox_vmid", "0")),
        proxmox_node=data.get("proxmox_node", ""),
        vmdk_path_on_node=data.get("vmdk_path_on_node", ""),
        proxmox_storage=data.get("proxmox_storage") or None,
        memory_mb=_optional_int(data, "memory_mb"),
        cores=_optional_int(data, "cores"),
        vlan=_optional_int(data, "vlan"),
        guest_ip=data.get("guest_ip") or None,
        error=data.get("error") or None,
        last_failed_stage=data.get("last_failed_stage") or None,
        created_at=data.get("created_at") or None,
        updated_at=data.get("updated_at") or None,
        preimported_disk_mode=_parse_job_bool_field(data.get("preimported_disk_mode")),
        firmware=(data.get("firmware") or "").strip() or None,
        guest_profile=(data.get("guest_profile") or "").strip() or None,
        enable_vtpm=_parse_job_bool_field(data.get("enable_vtpm")),
        qemu_cpu_preset=(data.get("qemu_cpu_preset") or "").strip() or None,
        take_snapshot=_parse_job_bool_field(data.get("take_snapshot")),
        import_mode=(data.get("import_mode") or "").strip() or None,
        import_permit_state=(data.get("import_permit_state") or "").strip() or None,
        import_permit_bucket=(data.get("import_permit_bucket") or "").strip() or None,
        job_kind=(data.get("job_kind") or "").strip() or None,
    )


def _job_record_for_api(cfg: dict[str, Any], data: dict[str, str]) -> JobRecord:
    rec = _hash_to_record(data)
    return rec.model_copy(update=_job_parallelism_api_fields(cfg, data, rec))


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
    out: dict[str, str] = {
        "name": app.title,
        "version": app.version,
        "code_fingerprint": _runtime_code_fingerprint(),
    }
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
