"""
Proxmox safety envelope: optional limits so the orchestrator only touches VMIDs and storage pools
you explicitly allow, and only purges scratch disks whose volume id matches this VM.
"""

from __future__ import annotations

import re
from typing import Any


def proxmox_safety(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = (cfg.get("proxmox") or {}).get("safety")
    return raw if isinstance(raw, dict) else {}


def _opt_int(v: Any) -> int | None:
    if v is None or v is False:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def validate_job_vmid_and_storage(cfg: dict[str, Any], vmid: int, storage_id: str) -> None:
    """
    Enforce ``proxmox.safety`` VMID range and storage allowlist.
    Raises ``ValueError`` with an operator-facing message if the job is out of policy.
    """
    s = proxmox_safety(cfg)
    lo = _opt_int(s.get("allowed_vmid_min"))
    hi = _opt_int(s.get("allowed_vmid_max"))
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(
            f"Invalid proxmox.safety: allowed_vmid_min ({lo}) > allowed_vmid_max ({hi}) in config.yaml"
        )
    if lo is not None and vmid < lo:
        raise ValueError(
            f"Proxmox VM ID {vmid} is below proxmox.safety.allowed_vmid_min ({lo}); refusing to enqueue or run."
        )
    if hi is not None and vmid > hi:
        raise ValueError(
            f"Proxmox VM ID {vmid} is above proxmox.safety.allowed_vmid_max ({hi}); refusing to enqueue or run."
        )

    allowed = s.get("allowed_storage_ids")
    if isinstance(allowed, list) and len(allowed) > 0:
        sid = (storage_id or "").strip()
        allowed_set = {str(x).strip() for x in allowed if str(x).strip()}
        if sid not in allowed_set:
            raise ValueError(
                f"Proxmox storage {sid!r} is not in proxmox.safety.allowed_storage_ids {sorted(allowed_set)!r}; refusing."
            )


def validate_scratch_purge_volume(cfg: dict[str, Any], vmid: int, volume_id: str) -> None:
    """
    Before ``DELETE …/storage/…/content/{volume}``, optionally require ``vm-{vmid}-disk-N`` naming
    so a bug or stale job cannot drop an unrelated LV.
    """
    s = proxmox_safety(cfg)
    if not bool(s.get("scratch_purge_require_vm_disk_name", True)):
        return
    vid = (volume_id or "").strip()
    if re.fullmatch(rf"vm-{int(vmid)}-disk-\d+", vid, flags=re.IGNORECASE):
        return
    raise ValueError(
        f"Refusing to purge storage volume {volume_id!r}: expected name like "
        f"vm-{vmid}-disk-<n> for this job (proxmox.safety.scratch_purge_require_vm_disk_name). "
        "If your pool uses numeric volume ids only, set scratch_purge_require_vm_disk_name: false "
        "(weakens protection)."
    )
