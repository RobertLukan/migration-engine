from __future__ import annotations

from ui import (
    SnapshotPresentError,
    _bucket_snapshot_deltas,
    esxi_guest_snapshot_delta_uris,
)


def test_snapshot_present_error_is_value_error() -> None:
    # Existing `except ValueError` handlers must surface it as a 400.
    assert issubclass(SnapshotPresentError, ValueError)


def test_bucket_ignores_base_and_aux_files() -> None:
    uris = [
        "esxi:DC/ds1/win11/win11.vmdk",
        "esxi:DC/ds1/win11/win11-flat.vmdk",
        "esxi:DC/ds1/win11/win11-ctk.vmdk",
        "esxi:DC/ds1/win11/win11-sesparse.vmdk",
        "esxi:DC/ds1/win11/win11_1.vmdk",
    ]
    assert _bucket_snapshot_deltas(iter(uris)) == {}


def test_bucket_detects_delta_by_folder_and_stem() -> None:
    uris = [
        "esxi:DC/ds1/win11/win11.vmdk",
        "esxi:DC/ds1/win11/win11-000001.vmdk",
        "esxi:DC/ds1/win11/win11-000002.vmdk",
    ]
    m = _bucket_snapshot_deltas(iter(uris))
    # Keyed by both the base file stem and the parent folder (same here).
    assert set(m.keys()) == {"win11"}
    assert m["win11"] == [
        "esxi:DC/ds1/win11/win11-000001.vmdk",
        "esxi:DC/ds1/win11/win11-000002.vmdk",
    ]


def test_bucket_flat_layout_keyed_by_stem() -> None:
    # Datastore-root layout: parent folder is the datastore, base stem is the guest.
    uris = ["esxi:DC/ds1/appvm-000001.vmdk"]
    m = _bucket_snapshot_deltas(iter(uris))
    assert "appvm" in m
    assert "ds1" in m  # parent folder also indexed (over-detect is safe)


def test_bucket_second_disk_snapshot_keyed_by_folder() -> None:
    # Snapshot on a secondary disk: base stem differs, but the per-VM folder matches.
    uris = ["esxi:DC/ds1/dbvm/dbvm_1-000001.vmdk"]
    m = _bucket_snapshot_deltas(iter(uris))
    assert "dbvm" in m  # folder name resolves the guest
    assert m["dbvm"] == ["esxi:DC/ds1/dbvm/dbvm_1-000001.vmdk"]


def test_guest_snapshot_uris_empty_for_blank_name() -> None:
    assert esxi_guest_snapshot_delta_uris("node1", "") == []
    assert esxi_guest_snapshot_delta_uris("node1", "   ") == []
