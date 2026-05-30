"""Unit tests for ESXi import concurrency bucket (v2 worker)."""

from worker import (
    apply_esxi_import_permit_bucket_override,
    esxi_import_concurrency_bucket,
    resolve_esxi_import_permit_bucket,
)


def test_bucket_from_pve_esxi_volid() -> None:
    assert (
        esxi_import_concurrency_bucket("sh12:ha-datacenter/DS1/VM/VM.vmdk") == "sh12"
    )


def test_bucket_volid_flat_vmdk_no_slashes() -> None:
    """PVE may surface a volid without path segments; still bucket on storage id."""
    assert esxi_import_concurrency_bucket("sh12:VM-flat.vmdk") == "sh12"


def test_bucket_none_for_simulated() -> None:
    assert esxi_import_concurrency_bucket("simulated:clone") is None


def test_bucket_none_for_host_path() -> None:
    assert esxi_import_concurrency_bucket("/mnt/pve/nfs/vm/disk.vmdk") is None


def test_bucket_none_for_http_url() -> None:
    assert esxi_import_concurrency_bucket("http://example/x/disk.vmdk") is None


def test_bucket_esxi_uri_dc_datastore() -> None:
    assert (
        esxi_import_concurrency_bucket("esxi:ha-datacenter/test2/clone1/clone1.vmdk")
        == "esxi:ha-datacenter/test2"
    )


def test_bucket_esxi_uri_dc_path_normalized_lower() -> None:
    assert (
        esxi_import_concurrency_bucket("esxi:HA-DATacenter/DS1/vm/disk.vmdk")
        == "esxi:ha-datacenter/ds1"
    )


def test_bucket_legacy_esxi_uri_short_path() -> None:
    assert esxi_import_concurrency_bucket("esxi:onlyone.vmdk") == "esxi_legacy_uri"


def test_resolve_bucket_respects_override_map() -> None:
    cfg = {"worker": {"esxi_import_permit_bucket_override": {"sh12": "hostA", "sh13": "hostA"}}}
    assert resolve_esxi_import_permit_bucket(cfg, "sh12:dc/ds/vm/disk.vmdk") == "hostA"
    assert resolve_esxi_import_permit_bucket(cfg, "sh13:dc/ds/vm/disk.vmdk") == "hostA"


def test_resolve_bucket_none_for_simulated_path() -> None:
    assert resolve_esxi_import_permit_bucket({}, "simulated:clone") is None


def test_bucket_override_merge_storage_ids() -> None:
    cfg = {"worker": {"esxi_import_permit_bucket_override": {"sh12": "hostA", "sh13": "hostA"}}}
    assert apply_esxi_import_permit_bucket_override(cfg, "sh12") == "hostA"
    assert apply_esxi_import_permit_bucket_override(cfg, "sh13") == "hostA"
    assert apply_esxi_import_permit_bucket_override(cfg, "sh99") == "sh99"
