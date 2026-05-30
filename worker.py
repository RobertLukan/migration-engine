"""
Redis-driven migration worker: Proxmox import + SATA first-boot (with a small SCSI scratch disk),
grace shutdown, SATA→SCSI disk move, second-boot guest-agent wait. Horizon/WinRM guest automation
is optional and commonly kept as a placeholder hook for future workflows.
Max concurrent VMDK imports enforced with a semaphore; hardware steps wrapped with Redis error logging.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import redis
import winrm
import yaml
from proxmoxer import ProxmoxAPI

from pve_safety import validate_job_vmid_and_storage, validate_scratch_purge_volume
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
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


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
        )
    return None


def job_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{cfg['redis']['job_key_prefix']}{job_id}"


def job_log_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{job_key(cfg, job_id)}{cfg['redis']['job_log_suffix']}"


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


PRE_SCSI_SWAP_SNAPSHOT = "pre_scsi_swap"


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


def _volume_indices_on_storage(cfg_qemu: dict[str, Any], storage_id: str) -> set[int]:
    """
    Collect numeric volume indices already used on ``storage_id`` (``STORAGE:N`` or
    ``STORAGE:vm-*-disk-N`` in any disk/efidisk/tpm field). Used to avoid reusing ``:0`` after
    ``sata0=data15_nvme:0,import-from=…`` which breaks LVM-thin with ``virtualsize may not be zero``.
    """
    sid = (storage_id or "").strip()
    if not sid:
        return set()
    found: set[int] = set()
    digit_prefix = re.compile(re.escape(sid) + r":(\d+)(?:,|$)", re.IGNORECASE)
    vm_disk = re.compile(re.escape(sid) + r":vm-\d+-disk-(\d+)(?:,|$)", re.IGNORECASE)
    for val in cfg_qemu.values():
        if not isinstance(val, str):
            continue
        first = val.strip().split(",")[0].strip()
        m = digit_prefix.match(first)
        if m:
            found.add(int(m.group(1)))
            continue
        m = vm_disk.match(first)
        if m:
            found.add(int(m.group(1)))
            continue
    return found


def _next_free_pve_storage_volume_index(cfg_qemu: dict[str, Any], storage_id: str) -> int:
    used = _volume_indices_on_storage(cfg_qemu, storage_id)
    idx = 0
    while idx in used:
        idx += 1
    return idx


def _pve_disk_storage_volume_ref(disk_line: str) -> tuple[str, str] | None:
    """
    Parse ``STORAGE:volumeId`` from the first segment of a qm disk option string
    (e.g. ``data15_nvme:vm-500-disk-3,cache=none,size=2G`` → ``(data15_nvme, vm-500-disk-3)``).
    """
    root = (disk_line or "").strip().split(",")[0].strip()
    if ":" not in root:
        return None
    sid, vol = root.split(":", 1)
    sid = sid.strip()
    vol = vol.strip()
    if sid and vol:
        return (sid, vol)
    return None


def _disk_line_merge_options(disk_line: str, updates: dict[str, str]) -> str:
    """Merge ``key=value`` options into a qm disk string; first comma segment is the volume reference."""
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


def _qemu_cpu_preset_from_job(raw: str | None) -> str | None:
    k = (raw or "").strip()
    return k if k in QEMU_CPU_PRESET_ARGS else None


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
    args_val = QEMU_CPU_PRESET_ARGS[preset_key]
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


def _purge_pve_storage_volume(
    proxmox: ProxmoxAPI,
    cfg: dict[str, Any],
    node: str,
    storage_id: str,
    volume_id: str,
    vmid: int,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None,
) -> None:
    """Remove a disk image from a storage pool (``DELETE …/storage/{id}/content/{vol}``)."""
    validate_scratch_purge_volume(cfg, vmid, volume_id)
    resp = proxmox.nodes(node).storage(storage_id).content(volume_id).delete()
    pve_check_api_response(resp, f"purge storage volume {storage_id}:{volume_id}")
    wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_poll)


def _delete_qemu_unused_slots_for_volume(
    proxmox: ProxmoxAPI,
    cfg: dict[str, Any],
    node: str,
    vmid: int,
    storage_id: str,
    volume_id: str,
    *,
    cancel_poll: tuple[redis.Redis, dict[str, Any], str, str] | None,
) -> None:
    """
    Remove ``unusedN`` entries that still point at ``storage_id:volume_id`` (e.g. after ``delete=scsi0``
    Proxmox may park the disk on ``unused0``; after purging the LV the slot can remain until deleted).
    """
    want = (storage_id, volume_id)
    cfg_live = get_qemu_config(proxmox, node, vmid)
    for key in sorted(k for k in cfg_live if isinstance(k, str) and re.fullmatch(r"unused\d+", k)):
        line = (cfg_live.get(key) or "").strip()
        if _pve_disk_storage_volume_ref(line) != want:
            continue
        resp = proxmox.nodes(node).qemu(vmid).config.post(delete=key)
        pve_check_api_response(resp, f"delete {key} (scratch volume reference)")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_poll)


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


def _job_simulated_import(data: dict[str, str], cfg: dict[str, Any]) -> bool:
    raw = (data.get("simulated_import") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool((cfg.get("proxmox") or {}).get("simulated_import"))


def _import_disk_format(p: dict[str, Any], vmdk_path: str) -> str:
    """
    PVE import-from format must match the source file.
    format=raw with a .vmdk often fails silently or never creates sata0 — use vmdk or qcow2.
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


def _efi_tpm_disk_format(px: dict[str, Any]) -> str:
    """Format for newly allocated efidisk0 / tpmstate0 volumes (must match storage capabilities)."""
    f = (px.get("efi_tpm_disk_format") or "raw").strip().lower()
    if f in ("", "auto"):
        return "raw"
    return f


def ensure_ovmf_efidisk_and_vtpm(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
    storage: str,
) -> None:
    """
    Add EFI vars disk (4m, pre-enrolled Secure Boot keys) and TPM state (v2.0) before the guest
    is powered on. Skips each device if already present (e.g. cloned template).
    """
    px = cfg.get("proxmox") or {}
    fmt = _efi_tpm_disk_format(px)
    cancel_imp = (r, cfg, job_id, MigrationState.IMPORTING.value)
    cfg_now = get_qemu_config(proxmox, node, vmid)

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

    cfg_now = get_qemu_config(proxmox, node, vmid)
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


def stage_import_vmdk_sata0(
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
) -> None:
    p = cfg["proxmox"]
    ostype = p.get("ostype", "win10")
    disk_fmt = _import_disk_format(p, vmdk_path)
    cancel_imp = (r, cfg, job_id, MigrationState.IMPORTING.value)

    def create_shell() -> None:
        resp = proxmox.nodes(node).qemu.post(
            vmid=vmid,
            name=vm_name,
            memory=int(memory_mb),
            cores=int(cores),
            sockets=1,
            ostype=ostype,
            bios="ovmf",
            machine="q35",
            scsihw="virtio-scsi-single",
            agent="enabled=1",
            balloon=0,
            **{"allow-ksm": 0},
        )
        pve_check_api_response(resp, "qemu create")

    hardware_change(r, cfg, job_id, MigrationState.IMPORTING.value, "create_vm_q35_ovmf", create_shell)

    sata_spec = f"{storage}:0,import-from={vmdk_path},format={disk_fmt}"

    def attach_import() -> None:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.IMPORTING.value,
            f"import_vmdk_to_sata0 spec: sata0={sata_spec}",
        )
        resp = proxmox.nodes(node).qemu(vmid).config.post(sata0=sata_spec)
        log.info("PVE config.post(sata0=...) response type=%s value=%r", type(resp).__name__, resp)
        pve_check_api_response(resp, "import vmdk to sata0")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_imp)
        cfg_now = get_qemu_config(proxmox, node, vmid)
        if not (cfg_now.get("sata0") or "").strip():
            src_hint = (
                "ESXi URI wrong or ESXi storage not linked in PVE"
                if str(vmdk_path).lower().startswith("esxi:")
                else f"file missing on the PVE node at {vmdk_path!r}"
            )
            raise RuntimeError(
                "sata0 is still empty after import. Common causes: (1) "
                + src_hint
                + "; (2) import_disk_format incompatible "
                f"(effective format={disk_fmt!r}; for .vmdk use vmdk or qcow2); "
                "(3) target storage does not support this import-from — check "
                "Datacenter → Tasks on the node; (4) async import failed — see Redis job log."
            )

    hardware_change(r, cfg, job_id, MigrationState.IMPORTING.value, "import_vmdk_to_sata0", attach_import)
    ensure_ovmf_efidisk_and_vtpm(r, cfg, proxmox, job_id, node, vmid, storage)


def stage_simulated_clone(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    target_node: str,
    new_vmid: int,
    vm_name: str,
    storage: str,
    source_vmid_override: int | None = None,
) -> None:
    """Full clone of a template VM on Proxmox instead of VMware import-from (testing / dry pipeline)."""
    px = cfg.get("proxmox") or {}
    source_vmid = int(
        source_vmid_override
        if source_vmid_override is not None
        else px.get("simulated_clone_source_vmid", 147)
    )
    source_node = (px.get("simulated_clone_source_node") or "").strip() or target_node.strip()
    cancel_imp = (r, cfg, job_id, MigrationState.IMPORTING.value)

    try:
        src_cfg = get_qemu_config(proxmox, source_node, source_vmid)
        src_vmstate_storage = (src_cfg.get("vmstatestorage") or "").strip()
        if src_vmstate_storage:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.IMPORTING.value,
                f"simulated clone preflight: source vmstatestorage={src_vmstate_storage!r}, target_storage={storage!r}",
            )
            if src_vmstate_storage != storage:
                append_log(
                    r,
                    cfg,
                    job_id,
                    "WARNING",
                    MigrationState.IMPORTING.value,
                    "simulated clone preflight: source vmstatestorage differs from selected target storage; "
                    "Proxmox may still require Datastore permissions on the source vmstatestorage.",
                )
    except Exception as exc:
        append_log(
            r,
            cfg,
            job_id,
            "WARNING",
            MigrationState.IMPORTING.value,
            f"simulated clone preflight: could not inspect source VM config ({exc!r})",
        )

    def clone_vm() -> None:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            MigrationState.IMPORTING.value,
            "simulated import: "
            f"qm clone from node={source_node!r} vmid={source_vmid} "
            f"-> newid={new_vmid} name={vm_name!r} target_storage={storage!r}",
        )
        params: dict[str, Any] = {
            "newid": new_vmid,
            "name": vm_name,
            "full": 1,
            "storage": storage,
        }
        if target_node.strip() and target_node.strip() != source_node:
            params["target"] = target_node.strip()
        resp = proxmox.nodes(source_node).qemu(source_vmid).clone.post(**params)
        pve_check_api_response(resp, "qm clone")
        wait_if_async(proxmox, cfg, source_node, resp, cancel_poll=cancel_imp)

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.IMPORTING.value,
        "simulated_clone",
        clone_vm,
    )


def stage_simulated_scsi_to_sata_prep(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
) -> None:
    """
    After cloning a SCSI-boot template, move the OS volume from scsi0 to sata0 so the rest of the
    pipeline matches a real VMware import (SATA boot → driver check → SCSI swap back to scsi0).
    """
    wcfg = cfg["worker"]
    poll = float(wcfg.get("task_poll_interval_sec", 2))
    shutdown_timeout = int(wcfg.get("shutdown_timeout_sec", 300))
    cancel_imp = (r, cfg, job_id, MigrationState.IMPORTING.value)

    def shutdown_guest() -> None:
        def logm(msg: str) -> None:
            append_log(r, cfg, job_id, "INFO", MigrationState.IMPORTING.value, msg)

        shutdown_or_stop_vm_retrying(
            proxmox,
            cfg,
            node,
            vmid,
            cancel_poll=cancel_imp,
            log_callback=logm,
        )

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.IMPORTING.value,
        "sim_prep_shutdown",
        shutdown_guest,
    )

    def wait_stopped() -> None:
        wait_vm_stopped(
            proxmox,
            node,
            vmid,
            shutdown_timeout,
            poll,
            cancel_poll=cancel_imp,
        )

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.IMPORTING.value,
        "sim_prep_wait_stopped",
        wait_stopped,
    )

    def move_scsi0_to_sata0() -> None:
        cfg_now = get_qemu_config(proxmox, node, vmid)
        scsi0 = (cfg_now.get("scsi0") or "").strip()
        if not scsi0:
            raise RuntimeError(
                "simulated prep: scsi0 missing; template VM should have the OS disk on virtio-scsi scsi0"
            )
        sata_existing = (cfg_now.get("sata0") or "").strip()
        if sata_existing:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.IMPORTING.value,
                "simulated prep: removing existing sata0 before attaching OS disk from scsi0",
            )
            resp0 = proxmox.nodes(node).qemu(vmid).config.post(delete="sata0")
            pve_check_api_response(resp0, "sim prep delete sata0")
            wait_if_async(proxmox, cfg, node, resp0, cancel_poll=cancel_imp)
        resp = proxmox.nodes(node).qemu(vmid).config.post(
            delete="scsi0",
            sata0=scsi0,
            boot="order=sata0",
        )
        pve_check_api_response(resp, "sim prep scsi0->sata0")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_imp)
        verify = get_qemu_config(proxmox, node, vmid)
        if not (verify.get("sata0") or "").strip():
            raise RuntimeError("simulated prep: sata0 missing after scsi0->sata0 move")

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.IMPORTING.value,
        "sim_prep_scsi0_to_sata0",
        move_scsi0_to_sata0,
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
) -> None:
    cancel_sata = (r, cfg, job_id, MigrationState.SATA_BOOT.value)
    wcfg = cfg.get("worker") or {}
    scratch = str(wcfg.get("pre_scsi_swap_scratch_disk_size", "2G")).strip() or "2G"
    cfg_now = get_qemu_config(proxmox, node, vmid)
    raw_idx = wcfg.get("pre_scsi_swap_scratch_disk_index")
    if raw_idx not in (None, ""):
        try:
            vol_idx = int(raw_idx)
        except (TypeError, ValueError):
            vol_idx = _next_free_pve_storage_volume_index(cfg_now, storage)
    else:
        vol_idx = _next_free_pve_storage_volume_index(cfg_now, storage)
    px = cfg.get("proxmox") or {}
    scratch_fmt = _efi_tpm_disk_format(px)
    scsi_spec = f"{storage}:{vol_idx},size={scratch},format={scratch_fmt}"
    net0_spec = _build_proxmox_net0_spec(cfg, vlan_override=vlan_override)

    def snapshot_before_scratch_disk() -> None:
        # Must run before scsi0 scratch exists so reverting the snapshot does not keep the scratch
        # volume attached (which would block deleting it without removing the snapshot first).
        if PRE_SCSI_SWAP_SNAPSHOT in _qemu_snapshot_names(proxmox, node, vmid):
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                MigrationState.SATA_BOOT.value,
                f"snapshot {PRE_SCSI_SWAP_SNAPSHOT!r} already present; skipping create",
            )
            return
        resp = proxmox.nodes(node).qemu(vmid).snapshot.post(
            snapname=PRE_SCSI_SWAP_SNAPSHOT,
            description=(
                "Automatic snapshot before virtio net + SCSI scratch disk "
                "(SATA first-boot / pre–SCSI-swap checkpoint)"
            ),
        )
        pve_check_api_response(resp, "snapshot pre scratch disk")
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_sata)

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.SATA_BOOT.value,
        "snapshot_pre_scsi_swap",
        snapshot_before_scratch_disk,
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

    preset_key = _qemu_cpu_preset_from_job(qemu_cpu_preset)
    if preset_key:

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

    def attach_net_and_scsi_scratch() -> None:
        # Drop any NICs from import/clone so we apply a single known-good virtio net (firewall off,
        # MTU, optional VLAN) before first boot. Small empty SCSI disk on the same pool as the OS.
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
            f"scsi scratch: index={vol_idx} spec={scsi_spec!r} (must not reuse sata0 import index)",
        )
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
            scsi0=scsi_spec,
            boot="order=sata0",
        )
        wait_if_async(proxmox, cfg, node, resp, cancel_poll=cancel_sata)

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.SATA_BOOT.value,
        "replace_net_virtio_and_scsi_scratch",
        attach_net_and_scsi_scratch,
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

    grace = float(wcfg.get("post_first_boot_agent_grace_sec", 60))
    append_log(
        r,
        cfg,
        job_id,
        "INFO",
        MigrationState.DRIVER_CHECK.value,
        f"guest agent is up; waiting {grace}s before shutdown for SCSI swap",
    )
    _sleep_with_cancel(r, cfg, job_id, MigrationState.DRIVER_CHECK.value, grace)

    def shutdown_after_first_boot() -> None:
        def logm(msg: str) -> None:
            append_log(r, cfg, job_id, "INFO", MigrationState.DRIVER_CHECK.value, msg)

        shutdown_or_stop_vm_retrying(
            proxmox,
            cfg,
            node,
            vmid,
            cancel_poll=cancel_dc,
            log_callback=logm,
        )

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.DRIVER_CHECK.value,
        "shutdown_after_first_boot",
        shutdown_after_first_boot,
    )

    poll = float(wcfg.get("task_poll_interval_sec", 2))
    shutdown_timeout = int(wcfg.get("shutdown_timeout_sec", 300))

    def wait_stopped_after_first_boot() -> None:
        wait_vm_stopped(
            proxmox,
            node,
            vmid,
            shutdown_timeout,
            poll,
            cancel_poll=cancel_dc,
        )

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.DRIVER_CHECK.value,
        "wait_stopped_after_first_boot",
        wait_stopped_after_first_boot,
    )
    return ip


def stage_scsi_swap(
    r: redis.Redis,
    cfg: dict[str, Any],
    proxmox: ProxmoxAPI,
    job_id: str,
    node: str,
    vmid: int,
) -> None:
    wcfg = cfg["worker"]
    poll = float(wcfg.get("task_poll_interval_sec", 2))
    shutdown_timeout = int(wcfg.get("shutdown_timeout_sec", 300))
    cancel_scsi = (r, cfg, job_id, MigrationState.SCSI_SWAP.value)

    def shutdown_guest() -> None:
        def logm(msg: str) -> None:
            append_log(r, cfg, job_id, "INFO", MigrationState.SCSI_SWAP.value, msg)

        shutdown_or_stop_vm_retrying(
            proxmox,
            cfg,
            node,
            vmid,
            cancel_poll=cancel_scsi,
            log_callback=logm,
        )

    hardware_change(r, cfg, job_id, MigrationState.SCSI_SWAP.value, "shutdown_vm", shutdown_guest)

    def wait_stopped() -> None:
        wait_vm_stopped(
            proxmox,
            node,
            vmid,
            shutdown_timeout,
            poll,
            cancel_poll=cancel_scsi,
        )

    hardware_change(r, cfg, job_id, MigrationState.SCSI_SWAP.value, "wait_stopped", wait_stopped)

    cfg_qemu = get_qemu_config(proxmox, node, vmid)
    sata0 = cfg_qemu.get("sata0")
    if not sata0:
        err = RuntimeError("sata0 missing in VM config before SCSI swap")
        fail_job(r, cfg, job_id, MigrationState.SCSI_SWAP.value, err)
        raise MigrationAborted from err

    scsi0_before = (cfg_qemu.get("scsi0") or "").strip()

    def move_os_disk_to_scsi0() -> None:
        # Detach scratch scsi0, delete its backing volume from storage (not just unused), then move OS SATA→scsi0.
        if scsi0_before:
            scratch_ref = _pve_disk_storage_volume_ref(scsi0_before)
            resp1 = proxmox.nodes(node).qemu(vmid).config.post(delete="scsi0")
            wait_if_async(proxmox, cfg, node, resp1, cancel_poll=cancel_scsi)
            if not scratch_ref:
                append_log(
                    r,
                    cfg,
                    job_id,
                    "WARNING",
                    MigrationState.SCSI_SWAP.value,
                    "could not parse storage:volume from scsi0 scratch line; backing volume was not purged from storage",
                )
            else:
                sid, vid = scratch_ref
                try:
                    _purge_pve_storage_volume(
                        proxmox,
                        cfg,
                        node,
                        sid,
                        vid,
                        vmid,
                        cancel_poll=cancel_scsi,
                    )
                except Exception as exc:
                    low = _pve_error_text(exc)
                    if (
                        "does not exist" in low
                        or "no such file" in low
                        or "not found" in low
                        or "404" in low
                    ):
                        append_log(
                            r,
                            cfg,
                            job_id,
                            "INFO",
                            MigrationState.SCSI_SWAP.value,
                            f"scratch volume {sid}:{vid} already absent on storage after detach ({exc!r})",
                        )
                    else:
                        raise
                _delete_qemu_unused_slots_for_volume(
                    proxmox,
                    cfg,
                    node,
                    vmid,
                    sid,
                    vid,
                    cancel_poll=cancel_scsi,
                )
        scsi0_os = _disk_line_merge_options(
            str(sata0).strip(),
            {"ssd": "1", "discard": "on", "aio": "native"},
        )
        resp2 = proxmox.nodes(node).qemu(vmid).config.post(
            delete="sata0",
            scsi0=scsi0_os,
            boot="order=scsi0",
        )
        wait_if_async(proxmox, cfg, node, resp2, cancel_poll=cancel_scsi)

    hardware_change(
        r,
        cfg,
        job_id,
        MigrationState.SCSI_SWAP.value,
        "detach_sata0_attach_scsi0_set_boot",
        move_os_disk_to_scsi0,
    )


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
    import_slots: threading.BoundedSemaphore,
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
    memory_mb = _job_int(data, "memory_mb", int(px_cfg.get("memory_mb", 8192)))
    cores = _job_int(data, "cores", int(px_cfg.get("cores", 4)))
    qemu_cpu_preset_raw = (data.get("qemu_cpu_preset") or "").strip()
    qemu_cpu_preset = qemu_cpu_preset_raw or None
    vlan_override_raw = (data.get("vlan") or "").strip()
    vlan_override = vlan_override_raw if vlan_override_raw else None
    win_user = data.get("winrm_user") or "Administrator"
    win_pass = data.get("winrm_password") or ""
    simulated = _job_simulated_import(data, cfg)
    simulated_source_vmid = _job_int(
        data,
        "simulated_clone_source_vmid",
        int(px_cfg.get("simulated_clone_source_vmid", 147)),
    )

    try:
        validate_job_vmid_and_storage(cfg, vmid, storage)
    except ValueError as exc:
        fail_job(r, cfg, job_id, state.value, exc)
        raise MigrationAborted from exc

    try:
        if state in (MigrationState.PENDING, MigrationState.IMPORTING):
            if state == MigrationState.PENDING:
                set_state(r, cfg, job_id, MigrationState.IMPORTING)
            with import_slots:
                if simulated:
                    stage_simulated_clone(
                        r,
                        cfg,
                        proxmox,
                        job_id,
                        node,
                        vmid,
                        vm_name,
                        storage,
                        simulated_source_vmid,
                    )
                    stage_simulated_scsi_to_sata_prep(
                        r, cfg, proxmox, job_id, node, vmid
                    )
                    ensure_ovmf_efidisk_and_vtpm(
                        r, cfg, proxmox, job_id, node, vmid, storage
                    )
                else:
                    stage_import_vmdk_sata0(
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
            )
            set_state(r, cfg, job_id, MigrationState.DRIVER_CHECK)

        data = r.hgetall(key)
        state = MigrationState(data.get("state", MigrationState.DRIVER_CHECK.value))
        if state in (MigrationState.CANCELLED, MigrationState.FAILED):
            return

        if state == MigrationState.DRIVER_CHECK:
            ip = stage_driver_check_agent_ip(r, cfg, proxmox, job_id, node, vmid)
            set_state(r, cfg, job_id, MigrationState.SCSI_SWAP)
        else:
            ip = data.get("guest_ip") or ""

        data = r.hgetall(key)
        state = MigrationState(data.get("state", MigrationState.SCSI_SWAP.value))
        if state in (MigrationState.CANCELLED, MigrationState.FAILED):
            return

        if state == MigrationState.SCSI_SWAP:
            stage_scsi_swap(r, cfg, proxmox, job_id, node, vmid)
            set_state(r, cfg, job_id, MigrationState.HORIZON_CONFIG)

        data = r.hgetall(key)
        state = MigrationState(data.get("state", MigrationState.HORIZON_CONFIG.value))
        if state in (MigrationState.CANCELLED, MigrationState.FAILED):
            return

        if state == MigrationState.HORIZON_CONFIG:
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
            set_state(r, cfg, job_id, MigrationState.COMPLETED)
            append_log(r, cfg, job_id, "INFO", MigrationState.COMPLETED.value, "migration finished")

    except MigrationAborted:
        log.info("Job %s stopped or aborted after a failed stage", job_id)
    except Exception as exc:
        fail_job(r, cfg, job_id, "unhandled", exc)
        log.exception("Unhandled failure for job %s", job_id)


def worker_loop(cfg: dict[str, Any]) -> None:
    r = redis.from_url(cfg["redis"]["url"], decode_responses=True)
    q = cfg["redis"]["queue_key"]
    max_imports = int(cfg.get("worker", {}).get("max_concurrent_imports", 3))
    import_slots = threading.BoundedSemaphore(max_imports)
    pool_workers = max(8, max_imports * 2)

    log.info("Worker up: queue=%s max_concurrent_imports=%s pool=%s", q, max_imports, pool_workers)

    def run(job_id: str) -> None:
        try:
            process_job(r, import_slots, job_id)
        except Exception:
            log.exception("Worker thread crashed for job %s", job_id)

    with ThreadPoolExecutor(max_workers=pool_workers) as executor:
        while True:
            item = r.blpop(q, timeout=30)
            if item is None:
                continue
            _, job_id = item
            executor.submit(run, job_id)


def main() -> None:
    cfg = load_config()
    worker_loop(cfg)


if __name__ == "__main__":
    main()
