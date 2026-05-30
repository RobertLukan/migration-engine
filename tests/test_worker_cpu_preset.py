from __future__ import annotations

from worker import _qemu_cpu_preset_from_job


def test_qemu_cpu_preset_from_job_none_when_unset() -> None:
    assert _qemu_cpu_preset_from_job(None) is None
    assert _qemu_cpu_preset_from_job("") is None


def test_qemu_cpu_preset_from_job_keeps_known_values() -> None:
    assert _qemu_cpu_preset_from_job("host_no_nested") == "host_no_nested"
    assert _qemu_cpu_preset_from_job("host_nested_vbs") == "host_nested_vbs"


def test_qemu_cpu_preset_from_job_invalid_returns_none() -> None:
    assert _qemu_cpu_preset_from_job("invalid") is None
