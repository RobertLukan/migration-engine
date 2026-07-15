from __future__ import annotations

from worker import _disk_line_merge_options


def test_disk_line_merge_options_adds_perf_flags() -> None:
    line = "ceph-vm:vm-3500-disk-0,format=raw"
    out = _disk_line_merge_options(
        line,
        {"ssd": "1", "iothread": "1", "discard": "on", "aio": "native"},
    )
    assert out.startswith("ceph-vm:vm-3500-disk-0,")
    assert "ssd=1" in out
    assert "iothread=1" in out
    assert "discard=on" in out
    assert "aio=native" in out


def test_disk_line_merge_options_overrides_existing_values() -> None:
    line = "local-lvm:vm-100-disk-0,ssd=0,iothread=0,discard=off,aio=threads"
    out = _disk_line_merge_options(
        line,
        {"ssd": "1", "iothread": "1", "discard": "on", "aio": "native"},
    )
    assert "ssd=1" in out and "ssd=0" not in out
    assert "iothread=1" in out and "iothread=0" not in out
    assert "discard=on" in out and "discard=off" not in out
    assert "aio=native" in out and "aio=threads" not in out
