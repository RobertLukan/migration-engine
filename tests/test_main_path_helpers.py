"""Regression tests for pure path / string helpers in ``main``."""

from __future__ import annotations

from main import _deepest_rel_dir_for_esxi, _job_row_sort_ts, _split_bracketed_datastore_path


def test_split_bracketed_datastore_path_vmware_style() -> None:
    ds, parent = _split_bracketed_datastore_path(r"[test2] clone1/clone1.vmx")
    assert ds == "test2"
    assert parent.replace("\\", "/") == "clone1"


def test_split_bracketed_datastore_path_empty_when_no_bracket() -> None:
    assert _split_bracketed_datastore_path("no-bracket") == ("", "")


def test_deepest_rel_dir_prefers_deepest_name_match() -> None:
    candidates = ["test2/clone1", "test2", "other/vm"]
    assert _deepest_rel_dir_for_esxi(candidates, "clone1") == "test2/clone1"


def test_deepest_rel_dir_empty_when_no_match() -> None:
    assert _deepest_rel_dir_for_esxi(["a/b"], "missing") == ""


def test_job_row_sort_ts_prefers_updated_over_created() -> None:
    assert (
        _job_row_sort_ts({"updated_at": "2026-01-02", "created_at": "2026-01-01"}) == "2026-01-02"
    )
    assert _job_row_sort_ts({"created_at": "2026-01-01"}) == "2026-01-01"
    assert _job_row_sort_ts({}) == ""
