from __future__ import annotations

from worker import (
    QEMU_CPU_PRESET_ARGS,
    _qemu_cpu_preset_args_map,
    _qemu_cpu_preset_from_job,
)


def test_qemu_cpu_preset_args_map_uses_builtins_by_default() -> None:
    cfg = {"proxmox": {}}
    m = _qemu_cpu_preset_args_map(cfg)
    assert m == QEMU_CPU_PRESET_ARGS


def test_qemu_cpu_preset_args_map_overrides_known_keys_only() -> None:
    cfg = {
        "proxmox": {
            "cpu_presets": {
                "host_no_nested": "-cpu host,-vmx,-svm",
                "unknown_key": "-cpu host,+foo",
            }
        }
    }
    m = _qemu_cpu_preset_args_map(cfg)
    assert m["host_no_nested"] == "-cpu host,-vmx,-svm"
    assert "unknown_key" not in m
    assert m["host_nested_vbs"] == QEMU_CPU_PRESET_ARGS["host_nested_vbs"]


def test_qemu_cpu_preset_from_job_uses_configurable_map() -> None:
    cfg = {
        "proxmox": {
            "cpu_presets": {
                "host_no_nested": "-cpu host,-vmx",
            }
        }
    }
    assert _qemu_cpu_preset_from_job("host_no_nested", cfg) == "host_no_nested"
    assert _qemu_cpu_preset_from_job("not-a-preset", cfg) is None
