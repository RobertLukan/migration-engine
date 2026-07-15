from __future__ import annotations

from worker import _qemu_cpu_preset_from_job

_CFG: dict = {"proxmox": {}}


def test_qemu_cpu_preset_from_job_none_when_unset() -> None:
    assert _qemu_cpu_preset_from_job(None, _CFG) is None
    assert _qemu_cpu_preset_from_job("", _CFG) is None


def test_qemu_cpu_preset_from_job_keeps_known_values() -> None:
    assert _qemu_cpu_preset_from_job("host_no_nested", _CFG) == "host_no_nested"
    assert _qemu_cpu_preset_from_job("host_nested_vbs", _CFG) == "host_nested_vbs"


def test_qemu_cpu_preset_from_job_invalid_returns_none() -> None:
    assert _qemu_cpu_preset_from_job("invalid", _CFG) is None
