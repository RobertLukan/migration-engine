"""Redis-backed migration states for VMware → Proxmox Windows VM moves."""

from __future__ import annotations

from enum import Enum


class MigrationState(str, Enum):
    PENDING = "PENDING"
    IMPORTING = "IMPORTING"
    SATA_BOOT = "SATA_BOOT"
    DRIVER_CHECK = "DRIVER_CHECK"
    SCSI_SWAP = "SCSI_SWAP"  # legacy resume only
    HORIZON_CONFIG = "HORIZON_CONFIG"  # legacy Redis jobs only; no longer used by the pipeline
    EFI_REDO = "EFI_REDO"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
