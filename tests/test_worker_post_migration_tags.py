from __future__ import annotations

from worker import _normalize_pve_tags, _post_migration_tags


def test_normalize_pve_tags_splits_and_deduplicates() -> None:
    tags = _normalize_pve_tags("migrated;efi-redo-pending, migrated  qa")
    assert tags == ["migrated", "efi-redo-pending", "qa"]


def test_post_migration_tags_defaults_when_unset() -> None:
    cfg = {"proxmox": {}}
    assert _post_migration_tags(cfg) == ["migrated", "efi-redo-pending"]


def test_post_migration_tags_empty_disables() -> None:
    cfg = {"proxmox": {"post_migrate_tags": ""}}
    assert _post_migration_tags(cfg) == []


def test_post_migration_tags_respects_custom_list() -> None:
    cfg = {"proxmox": {"post_migrate_tags": ["migrated", "efi-redo-pending", "wave-1"]}}
    assert _post_migration_tags(cfg) == ["migrated", "efi-redo-pending", "wave-1"]
