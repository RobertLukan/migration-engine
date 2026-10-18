"""
Redis-driven migration worker: Proxmox import directly to SCSI boot disk, first-boot guest-agent
check, optional Horizon via WinRM (see RUN_HORIZON_WINRM_STAGE).
Process **SIGTERM** / **SIGINT**: stop dequeuing, re-queue a popped job if not yet started, then drain the thread pool.

v2.0+: concurrent imports across worker threads / replicas are limited per **Proxmox ESXi storage id**
(``max_concurrent_imports_per_esxi_storage``, Redis token pools) so one ESXi datastore is not overloaded.
Roll back to v1.x images/code for the previous in-process global semaphore behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import redis
import yaml
from proxmoxer import ProxmoxAPI

from pve_safety import validate_job_vmid_and_storage
from states import MigrationState

_cfg_path_override = (os.environ.get("MIGRATION_ENGINE_CONFIG") or "").strip()
CONFIG_PATH = (
    Path(_cfg_path_override).expanduser()
    if _cfg_path_override
    else Path(__file__).resolve().parent / "config.yaml"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("migration-worker")


class MigrationAborted(Exception):
    """Job marked FAILED in Redis; stop pipeline."""


def load_config() -> dict[str, Any]:
    try:
        from config_validate import validate_loaded_config
    except ModuleNotFoundError:
        def validate_loaded_config(data: Any) -> dict[str, Any]:
            if data is None:
                raise ValueError("config file is empty")
            if not isinstance(data, dict):
                raise TypeError(f"config root must be a mapping, got {type(data).__name__}")
            return data

    with CONFIG_PATH.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raise ValueError(f"config file is empty: {CONFIG_PATH}")
    return validate_loaded_config(raw)


def qemu_vmids_in_use_on_node(proxmox: ProxmoxAPI, node: str) -> set[int]:
    """VMIDs that already have a ``/etc/pve/…/{vmid}.conf`` on this node (QEMU guests only)."""
    raw: Any = proxmox.nodes(node).qemu.get()
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        return set()
    out: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        vid = row.get("vmid")
        if vid is None:
            continue
        try:
            out.add(int(vid))
        except (TypeError, ValueError):
            continue
    return out


def qemu_vmids_in_use_cluster(proxmox: ProxmoxAPI) -> set[int]:
    """Cluster-wide VMIDs for QEMU guests (all nodes)."""
    raw: Any = proxmox.cluster.resources.get(type="vm")
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        return set()
    out: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type") or "").strip().lower()
        if typ and typ != "qemu":
            continue
        vid = row.get("vmid")
        if vid is None:
            continue
        try:
            out.add(int(vid))
        except (TypeError, ValueError):
            continue
    return out


def allocate_sequential_free_vmids(
    in_use: set[int], desired_start: int, count: int
) -> tuple[list[int], int]:
    """
    Pick ``count`` free VMIDs starting from ``desired_start``, skipping any present in ``in_use``.
    Mutates ``in_use`` by adding chosen IDs so the same set can be used for further allocations.
    Returns ``(allocated_list, next_suggested_cursor)`` where the cursor is last ID + 1.
    """
    if count < 1:
        return [], int(desired_start)
    out: list[int] = []
    v = max(100, int(desired_start))
    upper = v + max(5000, count * 250)
    while len(out) < int(count) and v <= upper:
        if v not in in_use:
            out.append(v)
            in_use.add(v)
        v += 1
    if len(out) < count:
        raise RuntimeError(
            f"could not allocate {count} free QEMU VMIDs starting near {desired_start} (exhausted scan to {upper})"
        )
    return out, out[-1] + 1


def connect_proxmox(cfg: dict[str, Any]) -> ProxmoxAPI:
    p = cfg["proxmox"]
    try:
        api_timeout = int(p.get("api_timeout_sec", 30))
    except (TypeError, ValueError):
        api_timeout = 30
    api_timeout = max(5, api_timeout)
    token_id = (p.get("api_token_id") or "").strip()
    token_secret = (p.get("api_token_secret") or "").strip()
    if token_id and token_secret:
        user, sep, token_name = token_id.partition("!")
        user = user.strip()
        token_name = token_name.strip()
        if not sep or not user or not token_name:
            raise ValueError(
                "Proxmox: api_token_id must look like 'user@realm!token_name' "
                "(example: root@pam!migration)"
            )
        return ProxmoxAPI(
            host=p["host"],
            user=user,
            token_name=token_name,
            token_value=token_secret,
            port=int(p.get("port", 8006)),
            verify_ssl=bool(p.get("verify_ssl", True)),
            timeout=api_timeout,
        )
    user = (p.get("user") or "").strip()
    password = (p.get("password") or "").strip()
    if not user or not password:
        raise ValueError("Proxmox: set api_token_id/api_token_secret or user/password in config.yaml")
    return ProxmoxAPI(
        host=p["host"],
        user=user,
        password=password,
        port=int(p.get("port", 8006)),
        verify_ssl=bool(p.get("verify_ssl", True)),
        timeout=api_timeout,
    )


def connect_proxmox_args_helper(cfg: dict[str, Any]) -> ProxmoxAPI | None:
    """
    Optional high-privilege client used only for ``qm args=...`` updates.
    Configure under ``proxmox`` in config.yaml:

      - args_helper_token_id + args_helper_token_secret, or
      - args_helper_user + args_helper_password

    If not configured, returns ``None`` and the normal proxmox client is used.
    """
    p = cfg.get("proxmox") or {}
    try:
        api_timeout = int(p.get("api_timeout_sec", 30))
    except (TypeError, ValueError):
        api_timeout = 30
    api_timeout = max(5, api_timeout)
    token_id = (p.get("args_helper_token_id") or "").strip()
    token_secret = (p.get("args_helper_token_secret") or "").strip()
    if token_id and token_secret:
        user, sep, token_name = token_id.partition("!")
        user = user.strip()
        token_name = token_name.strip()
        if not sep or not user or not token_name:
            raise ValueError(
                "Proxmox: args_helper_token_id must look like 'user@realm!token_name' "
                "(example: root@pam!migration-args)"
            )
        return ProxmoxAPI(
            host=p["host"],
            user=user,
            token_name=token_name,
            token_value=token_secret,
            port=int(p.get("port", 8006)),
            verify_ssl=bool(p.get("verify_ssl", True)),
            timeout=api_timeout,
        )

    user = (p.get("args_helper_user") or "").strip()
    password = (p.get("args_helper_password") or "").strip()
    if user and password:
        return ProxmoxAPI(
            host=p["host"],
            user=user,
            password=password,
            port=int(p.get("port", 8006)),
            verify_ssl=bool(p.get("verify_ssl", True)),
            timeout=api_timeout,
        )
    return None


def job_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{cfg['redis']['job_key_prefix']}{job_id}"


def job_log_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{job_key(cfg, job_id)}{cfg['redis']['job_log_suffix']}"


def _sanitize_import_bucket_fragment(s: str, max_len: int = 96) -> str:
    out: list[str] = []
    for ch in s:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    r = "".join(out).strip("._-")
    return (r or "x")[:max_len]


def esxi_import_concurrency_bucket(vmdk_path: str) -> str | None:
    """
    Redis import-permit pools are keyed by this bucket.

    Proxmox ESXi ``import-from`` volids look like ``<pve-storage-id>:dc/.../disk.vmdk``; concurrent
    reads for the same PVE ESXi storage id stress the same ESXi side, so we serialize (or cap) per id.

    Some listings use ``<pve-storage-id>:file.vmdk`` without path segments; we still bucket on the
    storage id when the tail looks like a disk descriptor.

    Host/NFS paths (no ``storageid:…`` pattern) return None — no ESXi-side permit (parallel OK).
    """
    p = (vmdk_path or "").strip()
    if not p:
        return None
    low = p.lower()
    if low.startswith("simulated:") or low.startswith("preimported:"):
        return None
    if low.startswith("http://") or low.startswith("https://"):
        return None
    if low.startswith("esxi:"):
        # Proxmox ``esxi:…`` URIs do not carry the PVE storage-id prefix, so we cannot reuse the
        # ``<pve-storage-id>:…`` bucket scheme. Serialize per datacenter + first path segment (usually
        # datastore / folder root in inventory) so two VMs on the same ESXi-side tree share a pool,
        # while unrelated ``esxi:`` trees can still run in parallel.
        tail = p[5:].strip().strip("/")
        parts = [x for x in tail.split("/") if x]
        if len(parts) >= 2:
            return f"esxi:{parts[0]}/{parts[1]}".lower()
        return "esxi_legacy_uri"
    if "\\" in p:
        return None
    sid, sep, tail = p.partition(":")
    if not sep:
        return None
    sid = sid.strip()
    tail = tail.strip()
    if not sid or not tail:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", sid):
        return None
    tlow = tail.lower()
    path_like = "/" in tail
    vol_like = tlow.endswith(".vmdk") or tlow.endswith(".vmx")
    if not path_like and not vol_like:
        return None
    return sid


def apply_esxi_import_permit_bucket_override(cfg: dict[str, Any], bucket: str | None) -> str | None:
    """
    Map the computed permit bucket onto an operator-defined key (e.g. ESXi host id).

    Use when the real limit is **per ESXi host**, while Proxmox exposes multiple linked storages
    (``sh12:…``, ``sh13:…``) that still funnel through the same host.
    """
    if not bucket:
        return None
    wcfg = cfg.get("worker") or {}
    ov = wcfg.get("esxi_import_permit_bucket_override")
    if not isinstance(ov, dict):
        return bucket
    if bucket in ov:
        v = ov[bucket]
    else:
        lk = bucket.lower()
        v = next((ov[k] for k in ov if isinstance(k, str) and k.lower() == lk), None)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return bucket


def resolve_esxi_import_permit_bucket(cfg: dict[str, Any], vmdk_path: str) -> str | None:
    """Computed import permit bucket after optional ``worker.esxi_import_permit_bucket_override``."""
    return apply_esxi_import_permit_bucket_override(cfg, esxi_import_concurrency_bucket(vmdk_path))


def _import_permit_redis_keys(cfg: dict[str, Any], bucket: str) -> tuple[str, str, str]:
    wcfg = cfg.get("worker") or {}
    base = (wcfg.get("import_permit_prefix") or "migration:import_permit:").strip()
    if not base.endswith(":"):
        base = base + ":"
    safe = _sanitize_import_bucket_fragment(bucket)
    q = f"{base}q:{safe}"
    size_k = f"{base}size:{safe}"
    lock_k = f"{base}lock:{safe}"
    return q, size_k, lock_k


def ensure_esxi_import_permit_pool(
    r: redis.Redis, cfg: dict[str, Any], bucket: str, want: int
) -> str:
    """Create or resize a token list so ``want`` permits exist (best-effort, lock-protected)."""
    q, size_k, lock_k = _import_permit_redis_keys(cfg, bucket)
    want = max(1, int(want))
    for _ in range(240):
        cur = r.get(size_k)
        ln = int(r.llen(q) or 0)
        # ``ln > want`` means extra permit tokens (double release, manual edits, or old pool state)
        # and would allow more than ``want`` concurrent imports — rebuild.
        if cur is not None and int(cur) == want and ln <= want:
            return q
        if r.set(lock_k, "1", nx=True, ex=20):
            try:
                pipe = r.pipeline()
                pipe.delete(q)
                for _i in range(want):
                    pipe.rpush(q, "1")
                pipe.set(size_k, str(want))
                pipe.execute()
            finally:
                r.delete(lock_k)
            return q
        time.sleep(0.5)
    raise RuntimeError(
        f"Timed out initializing ESXi import permit pool for bucket {bucket!r} (key {q!r})"
    )


def acquire_esxi_import_permit(
    r: redis.Redis,
    cfg: dict[str, Any],
    list_key: str,
    job_id: str,
    stage: str,
) -> None:
    wcfg = cfg.get("worker") or {}
    try:
        wait_max = int(wcfg.get("esxi_import_permit_wait_sec", 604800))
    except (TypeError, ValueError):
        wait_max = 604800
    deadline = time.monotonic() + max(60, wait_max)
    while time.monotonic() < deadline:
        check_job_cancelled(r, cfg, job_id, stage)
        popped = r.brpop(list_key, timeout=2)
        if popped:
            return
    raise TimeoutError(
        f"Timed out waiting for ESXi import permit on {list_key!r} after {wait_max}s"
    )


def release_esxi_import_permit(r: redis.Redis, list_key: str) -> None:
    r.rpush(list_key, "1")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_log(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    level: str,
    stage: str,
    message: str,
    exc: BaseException | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ts": utc_now_iso(),
        "level": level,
        "stage": stage,
        "message": message,
    }
    if exc is not None:
        payload["exception"] = repr(exc)
        payload["traceback"] = traceback.format_exc()
    r.rpush(job_log_key(cfg, job_id), json.dumps(payload))


def mark_job_cancelled_operator(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    *,
    stage: str = "",
) -> None:
    """Mark job CANCELLED (operator stop). Idempotent if already terminal."""
    key = job_key(cfg, job_id)
    cur = (r.hget(key, "state") or "").strip()
    if cur in (MigrationState.COMPLETED.value, MigrationState.FAILED.value, MigrationState.CANCELLED.value):
        r.hdel(key, "cancel_requested")
        return
    st = stage or "stop"
    append_log(r, cfg, job_id, "INFO", st, "migration stopped by operator")
    r.hset(
        key,
        mapping={
            "state": MigrationState.CANCELLED.value,
            "error": "Stopped by operator",
            "last_failed_stage": st,
            "updated_at": utc_now_iso(),
        },
    )
    r.hdel(key, "cancel_requested")


def check_job_cancelled(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    stage: str,
) -> None:
    key = job_key(cfg, job_id)
    if (r.hget(key, "cancel_requested") or "").strip() != "1":
        return
    mark_job_cancelled_operator(r, cfg, job_id, stage=stage)
    raise MigrationAborted


def set_state(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    state: MigrationState,
    **extra: str,
) -> None:
    key = job_key(cfg, job_id)
    mapping: dict[str, str] = {"state": state.value, "updated_at": utc_now_iso()}
    mapping.update({k: str(v) for k, v in extra.items()})
    r.hset(key, mapping=mapping)


def set_import_permit_status(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    *,
    bucket: str | None,
    permit_state: str,
) -> None:
    """
    Persist queue/permit hints for UI:
    - WAITING: job is blocked on bucket permit
    - ACQUIRED: job owns permit, import-from is running
    - NONE: import path has no bucket serialization
    - RELEASED: permit was released after import step
    """
    key = job_key(cfg, job_id)
    mapping: dict[str, str] = {
        "import_permit_state": str(permit_state).strip().upper(),
        "import_permit_updated_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    b = (bucket or "").strip()
    if b:
        mapping["import_permit_bucket"] = b
    else:
        # Keep key present but empty so UI knows bucket is intentionally absent.
        mapping["import_permit_bucket"] = ""
    r.hset(key, mapping=mapping)


def fail_job(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    stage: str,
    exc: BaseException,
) -> None:
    msg = f"{type(exc).__name__}: {exc}"
    append_log(r, cfg, job_id, "ERROR", stage, msg, exc)
    r.hset(
        job_key(cfg, job_id),
        mapping={
            "state": MigrationState.FAILED.value,
            "error": msg,
            "last_failed_stage": stage,
            "updated_at": utc_now_iso(),
        },
    )


def hardware_change(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    stage: str,
    label: str,
    fn: Callable[[], Any],
) -> Any:
    """Run a Proxmox hardware/config mutation; on failure log to Redis and mark FAILED."""
    try:
        append_log(r, cfg, job_id, "INFO", stage, f"start: {label}")
        check_job_cancelled(r, cfg, job_id, stage)
        out = fn()
        append_log(r, cfg, job_id, "INFO", stage, f"ok: {label}")
        return out
    except MigrationAborted:
        raise
    except Exception as exc:
        fail_job(r, cfg, job_id, f"{stage}:{label}", exc)
        raise MigrationAborted from exc


def task_upid_from_response(resp: Any) -> str | None:
    if resp is None:
        return None
    if isinstance(resp, str) and resp.startswith("UPID:"):
        return resp
    if isinstance(resp, dict):
        for key in ("data", "upid"):
            d = resp.get(key)
            if isinstance(d, str) and d.startswith("UPID:"):
                return d
            if isinstance(d, dict):
                for inner in ("upid", "UPID", "task_id"):
                    v = d.get(inner)
                    if isinstance(v, str) and v.startswith("UPID:"):
                        return v
    return None


def pve_check_api_response(resp: Any, context: str) -> None:
    """Raise if Proxmox returned errors in JSON (HTTP 200 with errors body)."""
    if not isinstance(resp, dict):
        return
    errs = resp.get("errors")
    if errs:
        raise RuntimeError(f"{context}: Proxmox API errors: {errs}")
    if resp.get("success") is False:
        msg = resp.get("message") or resp.get("detail") or str(resp)
        raise RuntimeError(f"{context}: {msg}")


def wait_for_task(
    proxmox: ProxmoxAPI,
    node: str,
    upid: str,
    poll: float,
    timeout_sec: int,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if cancel_poll is not None:
            cr, ccfg, jid, stg = cancel_poll
            check_job_cancelled(cr, ccfg, jid, stg)
        status = proxmox.nodes(node).tasks(upid).status.get()
        if status.get("status") == "stopped":
            if status.get("exitstatus") != "OK":
                es = str(status.get("exitstatus") or "")
                hint = ""
                low = es.lower()
                if "can't find file" in low or "cannot find" in low or "no such file" in low:
                    hint = (
                        " For a local import-from path, the file must exist on the PVE node; "
                        "for esxi:… URIs, check the linked ESXi storage, datacenter/folder/VM/disk path, "
                        "and proxmox.vmdk_path_template in config.yaml."
                    )
                raise RuntimeError(f"Task {upid} failed: {status}.{hint}")
            return
        time.sleep(poll)
    raise TimeoutError(f"Task {upid} timed out")


def wait_if_async(
    proxmox: ProxmoxAPI,
    cfg: dict[str, Any],
    node: str,
    resp: Any,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None = None,
) -> None:
    pve_check_api_response(resp, "async task")
    upid = task_upid_from_response(resp)
    if upid:
        wait_for_task(
            proxmox,
            node,
            upid,
            float(cfg["worker"].get("task_poll_interval_sec", 2)),
            86400,
            cancel_poll=cancel_poll,
        )


def get_qemu_config(proxmox: ProxmoxAPI, node: str, vmid: int) -> dict[str, Any]:
    return dict(proxmox.nodes(node).qemu(vmid).config.get())


def _qemu_list_rows(proxmox: ProxmoxAPI, node: str) -> list[Any]:
    raw: Any = proxmox.nodes(node).qemu.get()
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("data"), list):
        return raw["data"]
    return []


def _qemu_cluster_lock_hint(proxmox: ProxmoxAPI, node: str, vmid: int) -> str | None:
    for row in _qemu_list_rows(proxmox, node):
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("vmid", 0)) != int(vmid):
                continue
        except (TypeError, ValueError):
            continue
        lock = row.get("lock")
        if lock:
            s = str(lock).strip()
            return s or None
    return None


def wait_proxmox_disk_import_quiescent(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    stage: str,
    proxmox: ProxmoxAPI,
    node: str,
    vmid: int,
    resp: Any,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None,
) -> None:
    """
    ``import-from`` can keep reading from ESXi after ``config.post`` returns. Some PVE builds omit
    a task UPID in the API response, so ``wait_if_async`` returns immediately while the copy still
    runs — which would release our Redis ESXi import permit too early and allow overlapping imports
    from the same bucket. Wait until the VM is not locked and the OS disk line (``scsi0`` in current
    flow; legacy ``sata0`` in older jobs) no longer contains
    ``import-from`` (stable across two polls when no UPID was seen).
    """
    had_upid = bool(task_upid_from_response(resp))
    poll = float((cfg.get("worker") or {}).get("task_poll_interval_sec", 2))
    deadline = time.monotonic() + 86400
    idle_streak = 0
    warned = False
    while time.monotonic() < deadline:
        if cancel_poll is not None:
            cr, ccfg, jid, stg = cancel_poll
            check_job_cancelled(cr, ccfg, jid, stg)
        cfg_now = get_qemu_config(proxmox, node, vmid)
        disk_line = (cfg_now.get("scsi0") or cfg_now.get("sata0") or "").strip()
        if not disk_line:
            idle_streak = 0
            time.sleep(poll)
            continue
        list_lock = _qemu_cluster_lock_hint(proxmox, node, vmid)
        try:
            st = proxmox.nodes(node).qemu(vmid).status.current.get()
            status_lock = (st.get("lock") or "").strip() if isinstance(st, dict) else ""
        except Exception:
            status_lock = ""
        cfg_lock = str(cfg_now.get("lock") or "").strip()
        import_tail = "import-from" in disk_line.lower()
        busy = bool(list_lock or status_lock or cfg_lock or import_tail)
        if not busy:
            idle_streak += 1
        else:
            idle_streak = 0
        need_idle = 1 if had_upid else 2
        if idle_streak >= need_idle:
            return
        if not warned and not had_upid:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                stage,
                "Proxmox import-from had no task UPID in the API response; "
                "waiting until VM locks clear and scsi0/sata0 no longer references import-from "
                "before releasing the ESXi import permit.",
            )
            warned = True
        time.sleep(poll)
    raise TimeoutError(
        f"Timed out waiting for qemu/{vmid} on node {node!r} to finish import-from "
        "(VM lock or import-from still present after 24h)."
    )


PRE_BOOT_SNAPSHOT = "pre_boot"


def _qemu_snapshot_names(proxmox: ProxmoxAPI, node: str, vmid: int) -> set[str]:
    raw: Any = proxmox.nodes(node).qemu(vmid).snapshot.get()
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        return set()
    out: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            n = row.get("name")
            if isinstance(n, str) and n:
                out.add(n)
    return out


def _pre_first_boot_guest_resources_post(
    proxmox: ProxmoxAPI,
    cfg: dict[str, Any],
    node: str,
    vmid: int,
    memory_mb: int,
    cores: int,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None,
) -> None:
    """
    Apply RAM/CPU and memory-related tuning before the guest is powered on for driver check.
    Cloned templates keep source sizing until this runs; ``qm create`` also gets explicit follow-up
    so values always match the job.
    """
    resp = proxmox.nodes(node).qemu(vmid).config.post(
        memory=int(memory_mb),
        cores=int(cores),
        sockets=1,
        balloon=0,
        **{"allow-ksm": 0},
    )
    pve_check_api_response(resp, "pre-first-boot memory/cpu/balloon/ksm")
    wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_poll)


# Job hash key qemu_cpu_preset → qm args= string (-cpu … only).
QEMU_CPU_PRESET_ARGS: dict[str, str] = {
    "host_no_nested": "-cpu host,-vmx",
    "host_nested_vbs": (
        "-cpu host,+vmx,+kvm_pv_eoi,+kvm_pv_unhalt,hv-relaxed,hv-vapic,hv-time,hv-synic,hv-stimer,"
        "hv-vpindex,hv-ipi,hv-runtime,hv-reset,hv-crash,hv-spinlocks=0xfff,hv-tlbflush,hv-tlbflush-ext,"
        "hv-tlbflush-direct,hv-stimer-direct,hv-apicv,hv-avic,hv-evmcs,hv-xmm-input,hv-emsr-bitmap,"
        "host-cache-info=on,l3-cache=on,+invtsc,tsc-frequency=2900000000"
    ),
}


def _qemu_cpu_preset_args_map(cfg: dict[str, Any]) -> dict[str, str]:
    """
    CPU preset args map used by worker.
    Default values live in code; admins can override known presets via:
      proxmox.cpu_presets.<preset_key>: "-cpu ..."
    Unknown keys are ignored for safety.
    """
    out = dict(QEMU_CPU_PRESET_ARGS)
    px = cfg.get("proxmox") or {}
    raw = px.get("cpu_presets")
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        kk = str(k or "").strip()
        if kk not in out:
            continue
        sv = str(v or "").strip()
        if not sv:
            continue
        out[kk] = sv
    return out


def _qemu_cpu_preset_from_job(raw: str | None, cfg: dict[str, Any]) -> str | None:
    k = (raw or "").strip()
    return k if k in _qemu_cpu_preset_args_map(cfg) else None


def _apply_qemu_cpu_preset_to_guest(
    proxmox: ProxmoxAPI,
    cfg: dict[str, Any],
    r: redis.Redis,
    job_id: str,
    node: str,
    vmid: int,
    preset_key: str,
    stage: str,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None,
) -> None:
    """Remove PVE cpu= and set qemu args= to a fixed -cpu host,… profile (not expressible as cpu: alone)."""
    args_map = _qemu_cpu_preset_args_map(cfg)
    args_val = args_map[preset_key]
    if args_val != QEMU_CPU_PRESET_ARGS.get(preset_key):
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            stage,
            f"using custom cpu preset args from config for {preset_key!r}",
        )
    helper = connect_proxmox_args_helper(cfg)
    proxmox_for_args = helper or proxmox
    cfg_now = get_qemu_config(proxmox, node, vmid)
    if (cfg_now.get("cpu") or "").strip():
        resp_del = proxmox.nodes(node).qemu(vmid).config.post(delete="cpu")
        pve_check_api_response(resp_del, "delete cpu for custom qemu args")
        wait_if_async(proxmox, cfg, node, resp_del, cancel_poll=cancel_poll)
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            stage,
            "removed PVE cpu= setting so custom qemu args -cpu … applies",
        )
    if helper is not None:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            stage,
            "using proxmox args-helper credentials for root-only qemu args update",
        )
    try:
        resp = proxmox_for_args.nodes(node).qemu(vmid).config.post(args=args_val)
        pve_check_api_response(resp, f"set qemu args for cpu preset {preset_key!r}")
        wait_if_async(proxmox_for_args, cfg, node, resp, cancel_poll=cancel_poll)
    except Exception as exc:
        msg = str(exc)
        if "only root can set 'args' config" in msg and helper is None:
            raise RuntimeError(
                "Proxmox denied qemu args update (root-only). Configure proxmox.args_helper_user/password "
                "or proxmox.args_helper_token_id/args_helper_token_secret in config.yaml for this step."
            ) from exc
        raise


def _build_proxmox_net0_spec(cfg: dict[str, Any], vlan_override: str | None = None) -> str:
    """
    virtio NIC on the configured bridge, Proxmox firewall off, MTU default 1500, optional VLAN tag.
    Matches ``qm`` net string format (``tag=`` for Linux bridge VLAN).
    """
    px = cfg.get("proxmox") or {}
    bridge = (px.get("bridge") or "vmbr0").strip() or "vmbr0"
    parts = ["virtio", f"bridge={bridge}", "firewall=0"]
    mtu_raw = px.get("net_mtu")
    if mtu_raw is None or str(mtu_raw).strip() == "":
        mtu_i = 1500
    else:
        try:
            mtu_i = int(str(mtu_raw).strip())
        except (TypeError, ValueError):
            mtu_i = 1500
    if mtu_i > 0:
        parts.append(f"mtu={mtu_i}")
    vlan_raw = vlan_override if vlan_override is not None else (px.get("vlan") or px.get("bridge_vlan") or px.get("net_vlan"))
    if vlan_raw is not None and str(vlan_raw).strip() != "":
        try:
            tag = int(str(vlan_raw).strip(), 10)
            if 1 <= tag <= 4094:
                parts.append(f"tag={tag}")
        except ValueError:
            pass
    return ",".join(parts)


def _normalize_pve_tags(raw: Any) -> list[str]:
    """Parse Proxmox tags from string/list and return unique, non-empty tags preserving order."""
    parts: list[str] = []
    if raw is None:
        return parts
    if isinstance(raw, (list, tuple, set)):
        for v in raw:
            s = str(v or "").strip()
            if s:
                parts.append(s)
    else:
        s = str(raw).strip()
        if s:
            parts.extend([x for x in re.split(r"[;,\s]+", s) if x])
    out: list[str] = []
    seen: set[str] = set()
    for t in parts:
        k = t.lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _post_migration_tags(cfg: dict[str, Any], *, firmware: str = "uefi") -> list[str]:
    """
    Tags to apply when migration reaches COMPLETED.
    - proxmox.post_migrate_tags unset -> default tracking tags.
    - proxmox.post_migrate_tags empty string / [] -> disable tagging.
    - SeaBIOS guests never get efi-redo-pending (EFI redo is UEFI-only).
    """
    px = cfg.get("proxmox") or {}
    if "post_migrate_tags" not in px:
        tags = ["migrated", "efi-redo-pending"]
    else:
        tags = _normalize_pve_tags(px.get("post_migrate_tags"))
    if _normalize_firmware(firmware) == "seabios":
        tags = [t for t in tags if t.lower() != "efi-redo-pending"]
    return tags


def _apply_post_migration_tags(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    stage: str,
    *,
    firmware: str = "uefi",
) -> None:
    """Best-effort VM tag update to track post-migration operator actions."""
    wanted = _post_migration_tags(cfg, firmware=firmware)
    if not wanted:
        append_log(r, cfg, job_id, "INFO", stage, "post-migration VM tagging disabled by config")
        return
    cfg_now = get_qemu_config(proxmox, node, vmid)
    existing = _normalize_pve_tags(cfg_now.get("tags"))
    merged = _normalize_pve_tags([*existing, *wanted])
    if [x.lower() for x in merged] == [x.lower() for x in existing]:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            stage,
            f"post-migration VM tags already present: {';'.join(merged)}",
        )
        return
    resp = proxmox.nodes(node).qemu(vmid).config.post(tags=";".join(merged))
    pve_check_api_response(resp, "set post-migration VM tags")
    wait_if_async(proxmox, cfg, node, resp, cancel_poll=None)
    append_log(
        r,
        cfg,
        job_id,
        "INFO",
        stage,
        f"post-migration VM tags applied: {';'.join(merged)}",
    )


def _pve_error_text(exc: BaseException) -> str:
    parts = [str(exc)]
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            txt = resp.text if hasattr(resp, "text") else str(resp.content or b"")
            if isinstance(txt, bytes):
                txt = txt.decode(errors="replace")
            if txt:
                parts.append(txt[:800])
        except Exception:
            pass
    return " ".join(parts).lower()


def is_proxmox_vm_locked_error(exc: BaseException) -> bool:
    """True when PVE rejects an operation because the guest is locked (snapshot, backup, etc.)."""
    m = _pve_error_text(exc)
    if "vm is locked" in m:
        return True
    if "locked (" in m or "locked(" in m:
        return True
    return "is locked" in m and "task" in m


def shutdown_or_stop_vm_retrying(
    proxmox: ProxmoxAPI,
    cfg: dict[str, Any],
    node: str,
    vmid: int,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> None:
    """
    Ask the guest to shut down, falling back to hard stop. Retries when Proxmox returns VM locked
    (e.g. snapshot task still running).
    """
    wcfg = cfg.get("worker") or {}
    max_attempts = max(1, int(wcfg.get("vm_shutdown_max_attempts", 24)))
    delay = float(wcfg.get("vm_shutdown_retry_delay_sec", 5))
    api_timeout = int(wcfg.get("shutdown_api_timeout_sec", 120))

    def log(msg: str) -> None:
        if log_callback:
            log_callback(msg)

    for attempt in range(1, max_attempts + 1):
        if cancel_poll is not None:
            cr, ccfg, jid, stg = cancel_poll
            check_job_cancelled(cr, ccfg, jid, stg)
        st = proxmox.nodes(node).qemu(vmid).status.current.get()
        if st.get("status") == "stopped":
            return

        shut_exc: Exception | None = None
        try:
            proxmox.nodes(node).qemu(vmid).status.shutdown.post(timeout=api_timeout)
            if attempt > 1:
                log(f"shutdown API accepted on try {attempt}/{max_attempts}")
            return
        except Exception as e:
            shut_exc = e

        stop_exc: Exception | None = None
        try:
            proxmox.nodes(node).qemu(vmid).status.stop.post()
            if attempt > 1 or shut_exc is not None:
                log(f"hard stop API accepted on try {attempt}/{max_attempts}")
            return
        except Exception as e:
            stop_exc = e

        shut_l = shut_exc is not None and is_proxmox_vm_locked_error(shut_exc)
        stop_l = stop_exc is not None and is_proxmox_vm_locked_error(stop_exc)

        if shut_exc is not None and not shut_l:
            raise RuntimeError(
                f"Shutdown failed for VM {vmid} ({shut_exc!r}); "
                f"hard stop also failed ({stop_exc!r})"
            ) from (stop_exc or shut_exc)

        if stop_exc is not None and not stop_l:
            raise RuntimeError(f"Hard stop failed for VM {vmid} ({stop_exc!r})") from stop_exc

        if shut_l or stop_l:
            log(
                f"VM {vmid} locked (shutdown={shut_exc!r}; stop={stop_exc!r}), "
                f"retry {attempt}/{max_attempts} in {delay}s"
            )
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"VM {vmid} still locked after {max_attempts} shutdown/stop attempts "
                    f"(wait for snapshot/backup to finish or increase worker.vm_shutdown_*). "
                    f"Last shutdown: {shut_exc!r}; last stop: {stop_exc!r}"
                ) from (stop_exc or shut_exc)
            time.sleep(delay)
            continue

    raise RuntimeError(f"shutdown_or_stop_vm_retrying fell through for VM {vmid}")


def wait_vm_stopped(
    proxmox: ProxmoxAPI,
    node: str,
    vmid: int,
    timeout_sec: int,
    poll: float,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if cancel_poll is not None:
            cr, ccfg, jid, stg = cancel_poll
            check_job_cancelled(cr, ccfg, jid, stg)
        st = proxmox.nodes(node).qemu(vmid).status.current.get()
        if st.get("status") == "stopped":
            return
        time.sleep(poll)
    raise TimeoutError(f"VM {vmid} did not stop within {timeout_sec}s")


def extract_guest_ipv4(agent_payload: Any) -> str | None:
    """Parse qemu-guest-agent network-get-interfaces result."""
    if not agent_payload:
        return None
    data = agent_payload
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    if not isinstance(data, list):
        return None
    for iface in data:
        if not isinstance(iface, dict):
            continue
        addrs = iface.get("ip-addresses") or iface.get("ip_addresses")
        if not isinstance(addrs, list):
            continue
        for addr in addrs:
            if not isinstance(addr, dict):
                continue
            ip_type = addr.get("ip-address-type") or addr.get("ip_address_type")
            if ip_type not in (None, "ipv4"):
                continue
            ip = addr.get("ip-address") or addr.get("ip_address")
            if not ip or ip.startswith("127."):
                continue
            return str(ip)
    return None


def _sleep_with_cancel(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    stage: str,
    total_sec: float,
    *,
    chunk_sec: float = 5.0,
) -> None:
    """Sleep up to ``total_sec`` but wake periodically to honor job cancellation."""
    if total_sec <= 0:
        return
    end = time.monotonic() + total_sec
    while time.monotonic() < end:
        check_job_cancelled(r, cfg, job_id, stage)
        time.sleep(min(chunk_sec, end - time.monotonic()))


def _wait_guest_agent_ipv4(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    stage: str,
    proxmox: ProxmoxAPI,
    node: str,
    vmid: int,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None = None,
    timeout_sec: float | None = None,
) -> str:
    """Poll qemu-guest-agent until a non-loopback IPv4 is visible (same semantics as first boot)."""
    wcfg = cfg["worker"]
    interval = float(wcfg.get("guest_agent_poll_interval_sec", 5))
    if timeout_sec is not None:
        timeout = float(timeout_sec)
    else:
        timeout = float(wcfg.get("guest_agent_ip_timeout_sec", 1800))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel_poll is not None:
            cr, ccfg, jid, stg = cancel_poll
            check_job_cancelled(cr, ccfg, jid, stg)
        else:
            check_job_cancelled(r, cfg, job_id, stage)
        try:
            raw = proxmox.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
            ip = extract_guest_ipv4(raw)
            if ip:
                return ip
        except Exception as exc:
            append_log(
                r,
                cfg,
                job_id,
                "WARNING",
                stage,
                f"guest agent poll: {exc}",
                exc,
            )
        time.sleep(interval)
    raise TimeoutError("No guest IPv4 from qemu-guest-agent before timeout")


def _job_int(data: dict[str, str], key: str, default: int) -> int:
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def _job_bool(data: dict[str, str], key: str, default: bool = False) -> bool:
    raw = str(data.get(key) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _normalize_firmware(raw: str | None) -> str:
    k = (raw or "").strip().lower()
    if k in ("uefi", "ovmf", "efi"):
        return "uefi"
    if k in ("seabios", "bios", "legacy"):
        return "seabios"
    return ""


def _normalize_guest_profile(raw: str | None) -> str:
    k = (raw or "").strip().lower()
    if k in ("windows", "win"):
        return "windows"
    if k in ("linux", "l26", "other"):
        return "linux"
    return ""


def _resolve_job_firmware_profile(data: dict[str, str], cfg: dict[str, Any]) -> tuple[str, str, bool]:
    """
    Returns (firmware, guest_profile, enable_vtpm).
    Defaults: UEFI + windows; vTPM only when UEFI+windows unless job explicitly sets enable_vtpm.
    """
    px = cfg.get("proxmox") or {}
    firmware = _normalize_firmware(data.get("firmware")) or _normalize_firmware(
        str(px.get("default_firmware") or "")
    ) or "uefi"
    profile = _normalize_guest_profile(data.get("guest_profile")) or _normalize_guest_profile(
        str(px.get("default_guest_profile") or "")
    ) or "windows"

    raw_tpm = str(data.get("enable_vtpm") or "").strip().lower()
    if raw_tpm in ("1", "true", "yes", "on"):
        enable_vtpm = True
    elif raw_tpm in ("0", "false", "no", "off"):
        enable_vtpm = False
    else:
        cfg_tpm = px.get("default_enable_vtpm")
        if cfg_tpm is True:
            enable_vtpm = True
        elif cfg_tpm is False:
            enable_vtpm = False
        else:
            enable_vtpm = firmware == "uefi" and profile == "windows"
    return firmware, profile, enable_vtpm


def _ostype_for_profile(cfg: dict[str, Any], profile: str) -> str:
    px = cfg.get("proxmox") or {}
    if profile == "linux":
        return str(px.get("linux_ostype") or "l26").strip() or "l26"
    return str(px.get("ostype") or "win10").strip() or "win10"


def _import_disk_format(p: dict[str, Any], vmdk_path: str) -> str:
    """
    PVE import-from format must match the source file.
    format=raw with a .vmdk often fails silently or never creates scsi0 — use vmdk or qcow2.
    """
    fmt = (p.get("import_disk_format") or "auto").strip().lower()
    if fmt == "auto":
        lp = vmdk_path.lower()
        if lp.endswith(".vmdk"):
            return "vmdk"
        if lp.endswith(".qcow2"):
            return "qcow2"
        return "raw"
    if fmt == "raw" and vmdk_path.lower().endswith(".vmdk"):
        log.warning(
            "import_disk_format is 'raw' but source is .vmdk — using 'vmdk' "
            "(set proxmox.import_disk_format to vmdk/qcow2/auto in config.yaml)"
        )
        return "vmdk"
    return fmt


def _disk_line_merge_options(disk_line: str, updates: dict[str, str]) -> str:
    """Merge/override ``key=value`` options on a qm disk string; preserve first volume segment."""
    dl = (disk_line or "").strip()
    if not dl:
        return dl
    parts = [x.strip() for x in dl.split(",") if x.strip()]
    if not parts:
        return dl
    head = parts[0]
    order: list[str] = []
    seen: set[str] = set()
    kv: dict[str, str] = {}
    for seg in parts[1:]:
        if "=" in seg:
            k, _, v = seg.partition("=")
            kl = k.strip().lower()
            val = v.strip()
        else:
            kl = seg.lower()
            val = ""
        kv[kl] = val
        if kl not in seen:
            seen.add(kl)
            order.append(kl)
    for nk, nv in updates.items():
        kl = nk.lower()
        if kl not in seen:
            seen.add(kl)
            order.append(kl)
        kv[kl] = nv
    tail = ",".join(f"{k}={kv[k]}" if kv[k] != "" else k for k in order)
    return f"{head},{tail}" if tail else head


def _efi_tpm_disk_format(px: dict[str, Any]) -> str:
    """Format for newly allocated efidisk0 / tpmstate0 volumes (must match storage capabilities)."""
    f = (px.get("efi_tpm_disk_format") or "raw").strip().lower()
    if f in ("", "auto"):
        return "raw"
    return f


def ensure_firmware_devices(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    storage: str,
    *,
    firmware: str,
    enable_vtpm: bool,
) -> None:
    """
    Attach EFI vars disk and/or vTPM before first boot, based on firmware profile.
    - SeaBIOS: neither device (unless enable_vtpm is forced — unusual).
    - UEFI: efidisk0 always; tpmstate0 only when enable_vtpm is True.
    Skips each device if already present.
    """
    px = cfg.get("proxmox") or {}
    fmt = _efi_tpm_disk_format(px)
    cancel_imp = (r, cfg, job_id, MigrationState.IMPORTING.value)
    fw = _normalize_firmware(firmware) or "uefi"
    want_efi = fw == "uefi"
    want_tpm = bool(enable_vtpm)
    cfg_now = get_qemu_config(proxmox, node, vmid)

    if want_efi:
        if not (cfg_now.get("efidisk0") or "").strip():
            efispec = f"{storage}:1,format={fmt},efitype=4m,pre-enrolled-keys=1"

            def add_efi() -> None:
                append_log(
                    r,
                    cfg,
                    job_id,
                    "INFO",
                    MigrationState.IMPORTING.value,
                    f"efidisk0 (pre-enrolled keys): {efispec}",
                )
                resp = proxmox.nodes(node).qemu(vmid).config.post(efidisk0=efispec)
                pve_check_api_response(resp, "efidisk0 preboot")
                wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_imp)

            hardware_change(
                r,
                cfg,
                job_id,
                MigrationState.IMPORTING.value,
                "attach_efidisk0_preboot",
                add_efi,
            )
        else:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.IMPORTING.value,
                "efidisk0 already present; skip automatic EFI disk",
            )
    else:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.IMPORTING.value,
            "firmware=seabios: skipping efidisk0",
        )

    cfg_now = get_qemu_config(proxmox, node, vmid)
    if want_tpm:
        if not (cfg_now.get("tpmstate0") or "").strip():
            tpmspec = f"{storage}:1,format={fmt},version=v2.0"

            def add_tpm() -> None:
                append_log(
                    r,
                    cfg,
                    job_id,
                    "INFO",
                    MigrationState.IMPORTING.value,
                    f"tpmstate0 (vTPM): {tpmspec}",
                )
                resp = proxmox.nodes(node).qemu(vmid).config.post(tpmstate0=tpmspec)
                pve_check_api_response(resp, "tpmstate0 preboot")
                wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_imp)

            hardware_change(
                r,
                cfg,
                job_id,
                MigrationState.IMPORTING.value,
                "attach_tpmstate0_preboot",
                add_tpm,
            )
        else:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.IMPORTING.value,
                "tpmstate0 already present; skip automatic vTPM",
            )
    else:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.IMPORTING.value,
            "vTPM not enabled for this job; skipping tpmstate0",
        )


# Backward-compatible alias for older call sites / tests.
def ensure_ovmf_efidisk_and_vtpm(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    storage: str,
) -> None:
    ensure_firmware_devices(
        r,
        cfg,
        proxmox,
        job_id,
        node,
        vmid,
        storage,
        firmware="uefi",
        enable_vtpm=True,
    )


def stage_import_vmdk_scsi0(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    vm_name: str,
    vmdk_path: str,
    storage: str,
    memory_mb: int,
    cores: int,
    *,
    firmware: str = "uefi",
    guest_profile: str = "windows",
    enable_vtpm: bool = True,
) -> None:
    p = cfg["proxmox"]
    fw = _normalize_firmware(firmware) or "uefi"
    profile = _normalize_guest_profile(guest_profile) or "windows"
    ostype = _ostype_for_profile(cfg, profile)
    bios = "ovmf" if fw == "uefi" else "seabios"
    disk_fmt = _import_disk_format(p, vmdk_path)
    cancel_imp = (r, cfg, job_id, MigrationState.IMPORTING.value)

    def create_shell() -> None:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.IMPORTING.value,
            f"create VM shell: bios={bios} ostype={ostype} profile={profile} vtpm={enable_vtpm}",
        )
        resp = proxmox.nodes(node).qemu.post(
            vmid=vmid,
            name=vm_name,
            memory=int(memory_mb),
            cores=int(cores),
            sockets=1,
            ostype=ostype,
            bios=bios,
            machine="q35",
            scsihw="virtio-scsi-single",
            ide2="none,media=cdrom",
            agent="enabled=1",
            balloon=0,
            **{"allow-ksm": 0},
        )
        pve_check_api_response(resp, "qemu create")

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.IMPORTING.value,
        f"create_vm_q35_{bios}",
        create_shell,
    )

    scsi_spec = f"{storage}:0,import-from={vmdk_path},format={disk_fmt}"

    def attach_import() -> None:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.IMPORTING.value,
            f"import_vmdk_to_scsi0 spec: scsi0={scsi_spec}",
        )
        resp = proxmox.nodes(node).qemu(vmid).config.post(scsi0=scsi_spec, boot="order=scsi0")
        log.info("PVE config.post(scsi0=...) response type=%s value=%r", type(resp).__name__, resp)
        pve_check_api_response(resp, "import vmdk to scsi0")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_imp)
        cfg_now = get_qemu_config(proxmox, node, vmid)
        if not (cfg_now.get("scsi0") or "").strip():
            src_hint = (
                "ESXi URI wrong or ESXi storage not linked in PVE"
                if str(vmdk_path).lower().startswith("esxi:")
                else f"file missing on the PVE node at {vmdk_path!r}"
            )
            raise RuntimeError(
                "scsi0 is still empty after import. Common causes: (1) "
                + src_hint
                + "; (2) import_disk_format incompatible "
                f"(effective format={disk_fmt!r}; for .vmdk use vmdk or qcow2); "
                "(3) target storage does not support this import-from — check "
                "Datacenter → Tasks on the node; (4) async import failed — see Redis job log."
            )
        wait_proxmox_disk_import_quiescent(
            r,
            cfg,
            job_id,
            MigrationState.IMPORTING.value,
            proxmox,
            node,
            vmid,
            resp,
            cancel_poll=cancel_imp,
        )

    hardware_change(r, cfg, job_id, MigrationState.IMPORTING.value, "import_vmdk_to_scsi0", attach_import)
    ensure_firmware_devices(
        r,
        cfg,
        proxmox,
        job_id,
        node,
        vmid,
        storage,
        firmware=fw,
        enable_vtpm=enable_vtpm,
    )


def stage_preimported_disk_to_scsi0(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    storage: str,
    *,
    firmware: str = "uefi",
    enable_vtpm: bool = True,
) -> None:
    """
    Pre-imported mode: VM shell + disk were prepared manually by operator (e.g. qm importdisk).
    Normalize boot disk to scsi0 so downstream pipeline matches regular import flow.
    """
    stage = MigrationState.IMPORTING.value
    cancel_imp = (r, cfg, job_id, stage)
    fw = _normalize_firmware(firmware) or "uefi"

    def ensure_vm_stopped() -> None:
        st = proxmox.nodes(node).qemu(vmid).status.current.get()
        if st.get("status") == "running":
            shutdown_or_stop_vm_retrying(
                proxmox,
                cfg,
                node,
                vmid,
                cancel_poll=cancel_imp,
                log_callback=lambda m: append_log(r, cfg, job_id, "INFO", stage, m),
            )

    hardware_change(r, cfg, job_id, stage, "preimported_ensure_stopped", ensure_vm_stopped)

    def normalize_disk_to_scsi0() -> None:
        cfg_now = get_qemu_config(proxmox, node, vmid)
        scsi0 = (cfg_now.get("scsi0") or "").strip()
        if scsi0:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                stage,
                "preimported mode: scsi0 already present; enforcing boot order scsi0",
            )
            resp = proxmox.nodes(node).qemu(vmid).config.post(boot="order=scsi0")
            pve_check_api_response(resp, "preimported set boot order")
            wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_imp)
            ensure_firmware_devices(
                r,
                cfg,
                proxmox,
                job_id,
                node,
                vmid,
                storage,
                firmware=fw,
                enable_vtpm=enable_vtpm,
            )
            return

        src_key = ""
        src_val = ""
        for key in ("virtio0", "ide0", "sata0"):
            v = (cfg_now.get(key) or "").strip()
            if v:
                src_key, src_val = key, v
                break
        if not src_key:
            for key in sorted(k for k in cfg_now if isinstance(k, str) and re.fullmatch(r"unused\d+", k)):
                v = (cfg_now.get(key) or "").strip()
                if v:
                    src_key, src_val = key, v
                    break
        if not src_key:
            raise RuntimeError(
                "preimported mode: no disk found on scsi0/sata0/virtio0/ide0/unusedN. "
                "Import and attach disk first (e.g. qm importdisk + attach), then retry."
            )

        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            stage,
            f"preimported mode: moving {src_key} -> scsi0 and setting boot order",
        )
        resp = proxmox.nodes(node).qemu(vmid).config.post(delete=src_key, scsi0=src_val, boot="order=scsi0")
        pve_check_api_response(resp, "preimported disk -> scsi0")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_imp)
        verify = get_qemu_config(proxmox, node, vmid)
        if not (verify.get("scsi0") or "").strip():
            raise RuntimeError("preimported mode: scsi0 missing after move from source disk slot")
        ensure_firmware_devices(
            r,
            cfg,
            proxmox,
            job_id,
            node,
            vmid,
            storage,
            firmware=fw,
            enable_vtpm=enable_vtpm,
        )

    hardware_change(
        r,
        cfg,
        job_id,
        stage,
        "preimported_disk_to_scsi0",
        normalize_disk_to_scsi0,
    )


def stage_sata_boot_extras(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    storage: str,
    memory_mb: int,
    cores: int,
    qemu_cpu_preset: str | None,
    vlan_override: str | None,
    take_snapshot: bool,
) -> None:
    cancel_sata = (r, cfg, job_id, MigrationState.SATA_BOOT.value)
    net0_spec = _build_proxmox_net0_spec(cfg, vlan_override=vlan_override)

    def snapshot_before_boot() -> None:
        if PRE_BOOT_SNAPSHOT in _qemu_snapshot_names(proxmox, node, vmid):
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.SATA_BOOT.value,
                f"snapshot {PRE_BOOT_SNAPSHOT!r} already present; skipping create",
            )
            return
        resp = proxmox.nodes(node).qemu(vmid).snapshot.post(
            snapname=PRE_BOOT_SNAPSHOT,
            description="Automatic snapshot before first boot checks.",
        )
        pve_check_api_response(resp, "snapshot pre first boot")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_sata)

    if take_snapshot:
        hardware_change(
            r,
            cfg,
            job_id,
            MigrationState.SATA_BOOT.value,
            "snapshot_pre_first_boot",
            snapshot_before_boot,
        )
    else:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.SATA_BOOT.value,
            "snapshot_pre_first_boot: skipped by job option",
        )

    def pre_first_boot_guest_resources() -> None:
        _pre_first_boot_guest_resources_post(
            proxmox,
            cfg,
            node,
            vmid,
            memory_mb,
            cores,
            cancel_poll=cancel_sata,
        )

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.SATA_BOOT.value,
        "pre_first_boot_guest_resources",
        pre_first_boot_guest_resources,
    )

    def enforce_scsi0_performance_flags() -> None:
        cfg_live = get_qemu_config(proxmox, node, vmid)
        scsi0 = (cfg_live.get("scsi0") or "").strip()
        if not scsi0:
            raise RuntimeError("scsi0 missing in VM config before performance option tuning")
        tuned = _disk_line_merge_options(
            scsi0,
            {
                "ssd": "1",
                "iothread": "1",
                "discard": "on",
                "aio": "native",
            },
        )
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.SATA_BOOT.value,
            "scsi0 performance flags: ssd=1, iothread=1, discard=on, aio=native",
        )
        resp = proxmox.nodes(node).qemu(vmid).config.post(scsi0=tuned, boot="order=scsi0")
        pve_check_api_response(resp, "apply scsi0 performance flags")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_sata)

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.SATA_BOOT.value,
        "enforce_scsi0_perf_flags",
        enforce_scsi0_performance_flags,
    )

    preset_key = _qemu_cpu_preset_from_job(qemu_cpu_preset, cfg)
    if preset_key:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.SATA_BOOT.value,
            f"cpu preset from job request: {preset_key}",
        )

        def qemu_custom_cpu_args() -> None:
            _apply_qemu_cpu_preset_to_guest(
                proxmox,
                cfg,
                r,
                job_id,
                node,
                vmid,
                preset_key,
                MigrationState.SATA_BOOT.value,
                cancel_poll=cancel_sata,
            )

        hardware_change(
            r,
            cfg,
            job_id,
            MigrationState.SATA_BOOT.value,
            "qemu_custom_cpu_args",
            qemu_custom_cpu_args,
        )
    else:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.SATA_BOOT.value,
            "no CPU preset selected: keeping imported/template CPU config as-is "
            "(no forced cpu= default)",
        )

    def apply_net_and_boot_order() -> None:
        # Drop any NICs from import/clone so we apply a single known-good virtio net (firewall off,
        # MTU, optional VLAN) before first boot.
        cfg_live = get_qemu_config(proxmox, node, vmid)
        for key in sorted(k for k in cfg_live if isinstance(k, str) and re.fullmatch(r"net\d+", k)):
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.SATA_BOOT.value,
                f"removing {key} before applying standard net0",
            )
            resp_del = proxmox.nodes(node).qemu(vmid).config.post(delete=key)
            wait_if_async(proxmox, cfg, node, resp_del, cancel_poll=cancel_sata)
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.SATA_BOOT.value,
            f"net0: {net0_spec!r}",
        )
        resp = proxmox.nodes(node).qemu(vmid).config.post(
            net0=net0_spec,
            boot="order=scsi0",
        )
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_sata)

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.SATA_BOOT.value,
        "replace_net_virtio_set_boot_scsi0",
        apply_net_and_boot_order,
    )


def stage_driver_check_agent_ip(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
) -> str:
    wcfg = cfg["worker"]
    cancel_dc = (r, cfg, job_id, MigrationState.DRIVER_CHECK.value)

    def power_on() -> None:
        st = proxmox.nodes(node).qemu(vmid).status.current.get()
        if st.get("status") == "running":
            return
        proxmox.nodes(node).qemu(vmid).status.start.post()

    hardware_change(r, cfg, job_id, MigrationState.DRIVER_CHECK.value, "power_on", power_on)

    try:
        ip = _wait_guest_agent_ipv4(
            r,
            cfg,
            job_id,
            MigrationState.DRIVER_CHECK.value,
            proxmox,
            node,
            vmid,
            cancel_poll=cancel_dc,
        )
    except TimeoutError as err:
        fail_job(r, cfg, job_id, MigrationState.DRIVER_CHECK.value, err)
        raise MigrationAborted from err

    r.hset(
        job_key(cfg, job_id),
        mapping={"guest_ip": ip, "updated_at": utc_now_iso()},
    )
    append_log(r, cfg, job_id, "INFO", MigrationState.DRIVER_CHECK.value, f"guest ip: {ip}")

    grace = float(wcfg.get("post_first_boot_agent_grace_sec", 120))
    append_log(
        r,
        cfg,
        job_id,
        "INFO",
        MigrationState.DRIVER_CHECK.value,
        f"guest agent is up; waiting {grace}s for driver/services stabilization",
    )
    _sleep_with_cancel(r, cfg, job_id, MigrationState.DRIVER_CHECK.value, grace)
    return ip


def _run_winrm_powershell(
    *,
    cfg: dict[str, Any],
    ip: str,
    user: str,
    password: str,
    script: str,
) -> None:
    import winrm  # lazy: pywinrm import can hang on some hosts at process start

    w = cfg["winrm"]
    port = int(w.get("port", 5985))
    scheme = "https" if port == 5986 else "http"
    endpoint = f"{scheme}://{ip}:{port}/wsman"
    session = winrm.Session(
        endpoint,
        auth=(user, password),
        transport=w.get("transport", "ntlm"),
        server_cert_validation=w.get("server_cert_validation", "ignore"),
        operation_timeout_sec=int(w.get("operation_timeout_sec", 300)),
        read_timeout_sec=int(w.get("read_timeout_sec", 600)),
    )
    result = session.run_ps(script)
    if result.status_code != 0:
        stderr = result.std_err.decode(errors="replace") if result.std_err else ""
        raise RuntimeError(f"WinRM exit {result.status_code}: {stderr}")


def stage_efi_redo_tagged_vm(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    storage: str,
    *,
    run_guest_script: bool,
    win_user: str,
    win_pass: str,
    guest_script_override: str,
) -> None:
    stage = MigrationState.EFI_REDO.value
    cancel = (r, cfg, job_id, stage)
    px = cfg.get("proxmox") or {}
    pending_tag = str(px.get("efi_redo_pending_tag") or "efi-redo-pending").strip()
    migrated_tag = str(px.get("efi_redo_migrated_tag") or "migrated").strip()
    done_tag = str(px.get("efi_redo_done_tag") or "efi-redo-done").strip()

    cfg_now = get_qemu_config(proxmox, node, vmid)
    current_tags = _normalize_pve_tags(cfg_now.get("tags"))
    lower = {t.lower() for t in current_tags}
    need = [t for t in (migrated_tag, pending_tag) if t]
    missing = [t for t in need if t.lower() not in lower]
    if missing:
        raise RuntimeError(
            "EFI redo workflow requires VM tags: "
            + ", ".join(need)
            + f" (missing: {', '.join(missing)})"
        )

    if run_guest_script:
        script = (guest_script_override or "").strip() or str(px.get("efi_redo_guest_script") or "").strip()
        if not script:
            raise RuntimeError(
                "run_guest_script=true but no script provided; set proxmox.efi_redo_guest_script "
                "or send guest_script_override"
            )
        if not win_user.strip():
            raise RuntimeError("run_guest_script=true requires winrm_user")

        def power_on() -> None:
            st = proxmox.nodes(node).qemu(vmid).status.current.get()
            if st.get("status") == "running":
                return
            proxmox.nodes(node).qemu(vmid).status.start.post()

        hardware_change(r, cfg, job_id, stage, "power_on_for_guest_script", power_on)
        ip = _wait_guest_agent_ipv4(
            r,
            cfg,
            job_id,
            stage,
            proxmox,
            node,
            vmid,
            cancel_poll=cancel,
        )
        r.hset(job_key(cfg, job_id), mapping={"guest_ip": ip, "updated_at": utc_now_iso()})
        append_log(r, cfg, job_id, "INFO", stage, f"guest ip for EFI redo script: {ip}")
        append_log(r, cfg, job_id, "INFO", stage, "winrm: run EFI redo guest script")
        _run_winrm_powershell(cfg=cfg, ip=ip, user=win_user, password=win_pass, script=script)
        append_log(r, cfg, job_id, "INFO", stage, "winrm: EFI redo guest script ok")

    def ensure_stopped() -> None:
        shutdown_or_stop_vm_retrying(
            proxmox,
            cfg,
            node,
            vmid,
            cancel_poll=cancel,
            log_callback=lambda m: append_log(r, cfg, job_id, "INFO", stage, m),
        )
        wait_vm_stopped(proxmox, node, vmid, timeout_sec=600, poll=2, cancel_poll=cancel)

    hardware_change(r, cfg, job_id, stage, "ensure_vm_stopped", ensure_stopped)

    def recreate_efidisk() -> None:
        cfg_live = get_qemu_config(proxmox, node, vmid)
        cur = (cfg_live.get("efidisk0") or "").strip()
        if cur:
            append_log(r, cfg, job_id, "INFO", stage, f"remove existing efidisk0: {cur}")
            resp_del = proxmox.nodes(node).qemu(vmid).config.post(delete="efidisk0")
            pve_check_api_response(resp_del, "delete efidisk0")
            wait_if_async(proxmox, cfg, node, resp_del, cancel_poll=cancel)
        fmt = _efi_tpm_disk_format(px)
        efispec = f"{storage}:1,format={fmt},efitype=4m,pre-enrolled-keys=1"
        append_log(r, cfg, job_id, "INFO", stage, f"attach new efidisk0: {efispec}")
        resp_add = proxmox.nodes(node).qemu(vmid).config.post(efidisk0=efispec)
        pve_check_api_response(resp_add, "attach replacement efidisk0")
        wait_if_async(proxmox, cfg, node, resp_add, cancel_poll=cancel)

    hardware_change(r, cfg, job_id, stage, "recreate_efidisk0", recreate_efidisk)

    def update_tags() -> None:
        cfg_live = get_qemu_config(proxmox, node, vmid)
        tags = _normalize_pve_tags(cfg_live.get("tags"))
        kept = [t for t in tags if t.lower() != pending_tag.lower()]
        if done_tag and all(t.lower() != done_tag.lower() for t in kept):
            kept.append(done_tag)
        if kept:
            resp = proxmox.nodes(node).qemu(vmid).config.post(tags=";".join(kept))
        else:
            resp = proxmox.nodes(node).qemu(vmid).config.post(delete="tags")
        pve_check_api_response(resp, "update EFI redo tags")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel)
        append_log(r, cfg, job_id, "INFO", stage, f"updated VM tags after EFI redo: {';'.join(kept)}")

    hardware_change(r, cfg, job_id, stage, "update_efi_redo_tags", update_tags)


# Horizon / Omnissa guest work via WinRM (``horizon.powershell_script``) is OFF while post-migrate steps
# use Microsoft SCCM. Set True to restore optional guest WinRM script execution.
RUN_HORIZON_WINRM_STAGE = False


def stage_horizon_winrm(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    proxmox: ProxmoxAPI,
    node: str,
    vmid: int,
    ip: str,
    user: str,
    password: str,
) -> None:
    cancel_hz = (r, cfg, job_id, MigrationState.HORIZON_CONFIG.value)

    def power_on_after_scsi_swap() -> None:
        st = proxmox.nodes(node).qemu(vmid).status.current.get()
        if st.get("status") == "running":
            return
        proxmox.nodes(node).qemu(vmid).status.start.post()

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.HORIZON_CONFIG.value,
        "power_on_after_scsi_swap",
        power_on_after_scsi_swap,
    )

    def wait_guest_agent_after_scsi_boot() -> None:
        wcfg = cfg.get("worker") or {}
        raw_t = wcfg.get("post_scsi_swap_guest_agent_timeout_sec")
        t_ovr: float | None = None
        if raw_t not in (None, ""):
            try:
                t_ovr = float(raw_t)
            except (TypeError, ValueError):
                t_ovr = None
        try:
            ip2 = _wait_guest_agent_ipv4(
                r,
                cfg,
                job_id,
                MigrationState.HORIZON_CONFIG.value,
                proxmox,
                node,
                vmid,
                cancel_poll=cancel_hz,
                timeout_sec=t_ovr,
            )
        except TimeoutError as err:
            fail_job(r, cfg, job_id, MigrationState.HORIZON_CONFIG.value, err)
            raise MigrationAborted from err
        r.hset(
            job_key(cfg, job_id),
            mapping={"guest_ip": ip2, "updated_at": utc_now_iso()},
        )
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.HORIZON_CONFIG.value,
            f"guest agent up after SCSI boot; ip: {ip2}",
        )

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.HORIZON_CONFIG.value,
        "guest_agent_after_scsi_boot",
        wait_guest_agent_after_scsi_boot,
    )

    ip = (r.hget(job_key(cfg, job_id), "guest_ip") or "").strip() or ip.strip()
    if not ip:
        err = RuntimeError("guest_ip missing after SCSI reboot guest-agent wait")
        fail_job(r, cfg, job_id, MigrationState.HORIZON_CONFIG.value, err)
        raise MigrationAborted from err

    w = cfg["winrm"]
    port = int(w.get("port", 5985))
    scheme = "https" if port == 5986 else "http"
    endpoint = f"{scheme}://{ip}:{port}/wsman"
    script = cfg.get("horizon", {}).get("powershell_script", "").strip()
    if not script:
        err = ValueError("horizon.powershell_script empty in config.yaml")
        fail_job(r, cfg, job_id, MigrationState.HORIZON_CONFIG.value, err)
        raise MigrationAborted from err

    try:
        import winrm  # lazy: pywinrm import can hang on some hosts at process start

        append_log(r, cfg, job_id, "INFO", MigrationState.HORIZON_CONFIG.value, "winrm: run horizon script")
        session = winrm.Session(
            endpoint,
            auth=(user, password),
            transport=w.get("transport", "ntlm"),
            server_cert_validation=w.get("server_cert_validation", "ignore"),
            operation_timeout_sec=int(w.get("operation_timeout_sec", 300)),
            read_timeout_sec=int(w.get("read_timeout_sec", 600)),
        )
        result = session.run_ps(script)
        if result.status_code != 0:
            stderr = result.std_err.decode(errors="replace") if result.std_err else ""
            raise RuntimeError(f"WinRM exit {result.status_code}: {stderr}")
        append_log(r, cfg, job_id, "INFO", MigrationState.HORIZON_CONFIG.value, "winrm: horizon script ok")
    except Exception as exc:
        fail_job(r, cfg, job_id, MigrationState.HORIZON_CONFIG.value, exc)
        raise MigrationAborted from exc


def process_job(
    r: redis.Redis,
    job_id: str,
) -> None:
    cfg = load_config()
    key = job_key(cfg, job_id)
    data = r.hgetall(key)
    if not data:
        log.warning("Missing job hash for %s", job_id)
        return

    state = MigrationState(data.get("state", MigrationState.PENDING.value))
    if state in (MigrationState.COMPLETED, MigrationState.FAILED, MigrationState.CANCELLED):
        return

    check_job_cancelled(r, cfg, job_id, state.value)

    proxmox = connect_proxmox(cfg)
    vm_name = data["vm_name"]
    node = data["proxmox_node"]
    vmid = int(data["proxmox_vmid"])
    vmdk_path = data["vmdk_path_on_node"]
    px_cfg = cfg.get("proxmox") or {}
    raw_storage = (data.get("proxmox_storage") or "").strip()
    storage = raw_storage or (px_cfg.get("storage") or "").strip() or "local-lvm"
    if not raw_storage:
        append_log(
            r,
            cfg,
            job_id,
            "WARNING",
            state.value,
            f"No proxmox_storage on job hash; using config default {storage!r} "
            "(set storage when enqueueing, e.g. migrate-selected or POST /jobs)",
        )
    memory_mb = _job_int(data, "memory_mb", int(px_cfg.get("memory_mb", 16384)))
    cores = _job_int(data, "cores", int(px_cfg.get("cores", 6)))
    qemu_cpu_preset_raw = (data.get("qemu_cpu_preset") or "").strip()
    qemu_cpu_preset = qemu_cpu_preset_raw or None
    vlan_override_raw = (data.get("vlan") or "").strip()
    vlan_override = vlan_override_raw if vlan_override_raw else None
    win_user = data.get("winrm_user") or "Administrator"
    win_pass = data.get("winrm_password") or ""
    job_kind = (data.get("job_kind") or "").strip().lower()
    run_guest_script = str(data.get("run_guest_script") or "").strip().lower() in ("1", "true", "yes", "on")
    guest_script_override = (data.get("efi_redo_guest_script_override") or "").strip()
    preimported_mode = str(data.get("preimported_disk_mode") or "").strip().lower() in ("1", "true", "yes", "on")
    take_snapshot_raw = str(data.get("take_snapshot") or "").strip().lower()
    take_snapshot = take_snapshot_raw not in ("0", "false", "no", "off")
    firmware, guest_profile, enable_vtpm = _resolve_job_firmware_profile(data, cfg)

    try:
        validate_job_vmid_and_storage(cfg, vmid, storage)
    except ValueError as exc:
        fail_job(r, cfg, job_id, state.value, exc)
        raise MigrationAborted from exc

    try:
        if job_kind == "efi_redo":
            if state == MigrationState.PENDING:
                set_state(r, cfg, job_id, MigrationState.EFI_REDO)
            data = r.hgetall(key)
            state = MigrationState(data.get("state", MigrationState.EFI_REDO.value))
            if state in (MigrationState.CANCELLED, MigrationState.FAILED):
                return
            if state == MigrationState.EFI_REDO:
                stage_efi_redo_tagged_vm(
                    r,
                    cfg,
                    proxmox,
                    job_id,
                    node,
                    vmid,
                    storage,
                    run_guest_script=run_guest_script,
                    win_user=win_user,
                    win_pass=win_pass,
                    guest_script_override=guest_script_override,
                )
                set_state(r, cfg, job_id, MigrationState.COMPLETED)
                append_log(r, cfg, job_id, "INFO", MigrationState.COMPLETED.value, "efi redo workflow finished")
            return

        if state in (MigrationState.PENDING, MigrationState.IMPORTING):
            if state == MigrationState.PENDING:
                set_state(r, cfg, job_id, MigrationState.IMPORTING)
            if preimported_mode:
                set_import_permit_status(
                    r,
                    cfg,
                    job_id,
                    bucket=None,
                    permit_state="NONE",
                )
                stage_preimported_disk_to_scsi0(
                    r,
                    cfg,
                    proxmox,
                    job_id,
                    node,
                    vmid,
                    storage,
                    firmware=firmware,
                    enable_vtpm=enable_vtpm,
                )
            else:
                bucket = resolve_esxi_import_permit_bucket(cfg, vmdk_path)
                permit_list: str | None = None
                if bucket:
                    set_import_permit_status(
                        r,
                        cfg,
                        job_id,
                        bucket=bucket,
                        permit_state="WAITING",
                    )
                    wcfg = cfg.get("worker") or {}
                    try:
                        per_esxi = int(
                            wcfg.get("max_concurrent_imports_per_esxi_storage", 1)
                        )
                    except (TypeError, ValueError):
                        per_esxi = 1
                    per_esxi = max(1, per_esxi)
                    permit_list = ensure_esxi_import_permit_pool(
                        r, cfg, bucket, per_esxi
                    )
                    append_log(
                        r,
                        cfg,
                        job_id,
                        "INFO",
                        MigrationState.IMPORTING.value,
                        "v2 parallel imports: jobs on different ESXi storage ids (buckets) can run at the "
                        "same time; jobs sharing the same bucket are limited to "
                        f"max_concurrent_imports_per_esxi_storage={per_esxi} (shared across all worker replicas via Redis).",
                    )
                    append_log(
                        r,
                        cfg,
                        job_id,
                        "INFO",
                        MigrationState.IMPORTING.value,
                        f"ESXi import permit pool: bucket={bucket!r} redis_list={permit_list!r}",
                    )
                    append_log(
                        r,
                        cfg,
                        job_id,
                        "INFO",
                        MigrationState.IMPORTING.value,
                        "waiting for a free ESXi import permit (another job may be using this bucket)",
                    )
                    acquire_esxi_import_permit(
                        r,
                        cfg,
                        permit_list,
                        job_id,
                        MigrationState.IMPORTING.value,
                    )
                    append_log(
                        r,
                        cfg,
                        job_id,
                        "INFO",
                        MigrationState.IMPORTING.value,
                        f"acquired ESXi import permit for bucket={bucket!r}; starting import-from",
                    )
                    set_import_permit_status(
                        r,
                        cfg,
                        job_id,
                        bucket=bucket,
                        permit_state="ACQUIRED",
                    )
                else:
                    set_import_permit_status(
                        r,
                        cfg,
                        job_id,
                        bucket=None,
                        permit_state="NONE",
                    )
                    append_log(
                        r,
                        cfg,
                        job_id,
                        "INFO",
                        MigrationState.IMPORTING.value,
                        "v2: no ESXi per-storage permit for this import path (not storageid:…/path.vmdk); "
                        "this job is not serialized against others by ESXi storage bucket. "
                        "Different buckets still run in parallel when paths use distinct storage ids.",
                    )
                try:
                    stage_import_vmdk_scsi0(
                        r,
                        cfg,
                        proxmox,
                        job_id,
                        node,
                        vmid,
                        vm_name,
                        vmdk_path,
                        storage,
                        memory_mb,
                        cores,
                        firmware=firmware,
                        guest_profile=guest_profile,
                        enable_vtpm=enable_vtpm,
                    )
                finally:
                    if permit_list:
                        release_esxi_import_permit(r, permit_list)
                        set_import_permit_status(
                            r,
                            cfg,
                            job_id,
                            bucket=bucket,
                            permit_state="RELEASED",
                        )
                        append_log(
                            r,
                            cfg,
                            job_id,
                            "INFO",
                            MigrationState.IMPORTING.value,
                            "released ESXi import permit (slot available for another job on this bucket)",
                        )
            set_state(r, cfg, job_id, MigrationState.SATA_BOOT)

        data = r.hgetall(key)
        state = MigrationState(data.get("state", MigrationState.SATA_BOOT.value))
        if state in (MigrationState.CANCELLED, MigrationState.FAILED):
            return

        if state == MigrationState.SATA_BOOT:
            stage_sata_boot_extras(
                r,
                cfg,
                proxmox,
                job_id,
                node,
                vmid,
                storage,
                memory_mb,
                cores,
                qemu_cpu_preset,
                vlan_override,
                take_snapshot,
            )
            set_state(r, cfg, job_id, MigrationState.DRIVER_CHECK)

        data = r.hgetall(key)
        state = MigrationState(data.get("state", MigrationState.DRIVER_CHECK.value))
        if state in (MigrationState.CANCELLED, MigrationState.FAILED):
            return

        if state == MigrationState.DRIVER_CHECK:
            ip = stage_driver_check_agent_ip(r, cfg, proxmox, job_id, node, vmid)
            set_state(r, cfg, job_id, MigrationState.HORIZON_CONFIG)
        else:
            ip = data.get("guest_ip") or ""

        data = r.hgetall(key)
        state = MigrationState(data.get("state", MigrationState.SCSI_SWAP.value))
        if state in (MigrationState.CANCELLED, MigrationState.FAILED):
            return

        if state == MigrationState.SCSI_SWAP:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.SCSI_SWAP.value,
                "Legacy SCSI_SWAP state detected from an older job definition; "
                "direct-SCSI boot flow skips disk swap and continues.",
            )
            set_state(r, cfg, job_id, MigrationState.HORIZON_CONFIG)

        data = r.hgetall(key)
        state = MigrationState(data.get("state", MigrationState.HORIZON_CONFIG.value))
        if state in (MigrationState.CANCELLED, MigrationState.FAILED):
            return

        if state == MigrationState.HORIZON_CONFIG:
            if not RUN_HORIZON_WINRM_STAGE:
                append_log(
                    r,
                    cfg,
                    job_id,
                    "INFO",
                    MigrationState.HORIZON_CONFIG.value,
                    "HORIZON_CONFIG skipped (RUN_HORIZON_WINRM_STAGE=False); marking COMPLETED.",
                )
                try:
                    _apply_post_migration_tags(
                        r,
                        cfg,
                        proxmox,
                        job_id,
                        node,
                        vmid,
                        MigrationState.HORIZON_CONFIG.value,
                        firmware=firmware,
                    )
                except Exception as tag_exc:
                    append_log(
                        r,
                        cfg,
                        job_id,
                        "WARNING",
                        MigrationState.HORIZON_CONFIG.value,
                        f"post-migration VM tags failed (continuing): {tag_exc}",
                    )
                set_state(r, cfg, job_id, MigrationState.COMPLETED)
                append_log(
                    r,
                    cfg,
                    job_id,
                    "INFO",
                    MigrationState.COMPLETED.value,
                    "migration finished",
                )
            else:
                # --- Original Horizon / WinRM path (re-enable with RUN_HORIZON_WINRM_STAGE = True) ---
                ip = (r.hget(key, "guest_ip") or "").strip() or ip
                if not ip:
                    ip = data.get("guest_ip") or ""
                if not ip:
                    err = RuntimeError("guest_ip missing before WinRM")
                    fail_job(r, cfg, job_id, MigrationState.HORIZON_CONFIG.value, err)
                    raise MigrationAborted from err
                stage_horizon_winrm(
                    r, cfg, job_id, proxmox, node, vmid, ip, win_user, win_pass
                )
                try:
                    _apply_post_migration_tags(
                        r,
                        cfg,
                        proxmox,
                        job_id,
                        node,
                        vmid,
                        MigrationState.HORIZON_CONFIG.value,
                        firmware=firmware,
                    )
                except Exception as tag_exc:
                    append_log(
                        r,
                        cfg,
                        job_id,
                        "WARNING",
                        MigrationState.HORIZON_CONFIG.value,
                        f"post-migration VM tags failed (continuing): {tag_exc}",
                    )
                set_state(r, cfg, job_id, MigrationState.COMPLETED)
                append_log(
                    r, cfg, job_id, "INFO", MigrationState.COMPLETED.value, "migration finished"
                )

    except MigrationAborted:
        log.info("Job %s stopped or aborted after a failed stage", job_id)
    except Exception as exc:
        fail_job(r, cfg, job_id, "unhandled", exc)
        log.exception("Unhandled failure for job %s", job_id)


def worker_loop(cfg: dict[str, Any]) -> None:
    # socket_timeout must exceed BLPOP's block time. redis-py applies the command
    # timeout to the socket; when the queue is empty, a 30s BLPOP + 30s socket
    # timeout races and raises TimeoutError, which used to crash the process.
    r = redis.from_url(
        cfg["redis"]["url"],
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=60,
        retry_on_timeout=True,
    )
    q = cfg["redis"]["queue_key"]
    wcfg = cfg.get("worker") or {}
    try:
        per_esxi = int(wcfg.get("max_concurrent_imports_per_esxi_storage", 1))
    except (TypeError, ValueError):
        per_esxi = 1
    per_esxi = max(1, per_esxi)
    try:
        pool_workers = int(wcfg.get("job_thread_pool_size", max(16, per_esxi * 8)))
    except (TypeError, ValueError):
        pool_workers = max(16, per_esxi * 8)
    pool_workers = max(4, pool_workers)

    shutdown = threading.Event()
    instance = f"{socket.gethostname()} pid={os.getpid()} config={CONFIG_PATH}"

    def handle_sig(signum: int, _frame: object | None) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = str(signum)
        log.info(
            "Received %s — %s will stop dequeuing new jobs and drain in-flight migrations",
            sig_name,
            instance,
        )
        shutdown.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    log.info(
        "Worker up (v2): instance=%s queue=%s max_concurrent_imports_per_esxi_storage=%s job_thread_pool_size=%s",
        instance,
        q,
        per_esxi,
        pool_workers,
    )

    pending: set[Future[Any]] = set()

    def prune_done() -> None:
        done = {f for f in pending if f.done()}
        pending.difference_update(done)

    def run(job_id: str) -> None:
        try:
            process_job(r, job_id)
        except Exception:
            log.exception("Worker thread crashed for job %s", job_id)

    last_drain_log = 0.0
    with ThreadPoolExecutor(max_workers=pool_workers) as executor:
        while True:
            prune_done()
            if shutdown.is_set():
                if not pending:
                    log.info("Worker shutdown complete (%s)", instance)
                    break
                now = time.monotonic()
                if now - last_drain_log >= 60.0:
                    log.info(
                        "Shutdown: waiting for %d in-flight migration(s) (%s)",
                        len(pending),
                        instance,
                    )
                    last_drain_log = now
                wait(pending, timeout=10.0, return_when=FIRST_COMPLETED)
                continue

            try:
                item = r.blpop(q, timeout=25)
            except (redis.exceptions.TimeoutError, TimeoutError, redis.exceptions.ConnectionError) as exc:
                log.warning("Redis queue poll failed (%s); retrying", exc)
                time.sleep(1)
                continue
            if item is None:
                continue
            _, job_id = item
            if shutdown.is_set():
                try:
                    r.lpush(q, job_id)
                except Exception:
                    log.exception(
                        "Failed to re-queue job %s during shutdown — re-enqueue manually if needed",
                        job_id,
                    )
                else:
                    log.info("Re-queued job %s (shutdown before worker thread started)", job_id)
                continue
            fut = executor.submit(run, job_id)
            pending.add(fut)


def main() -> None:
    cfg = load_config()
    worker_loop(cfg)


if __name__ == "__main__":
    main()
