from __future__ import annotations

import pytest
from pydantic import ValidationError

from config_validate import validate_loaded_config


def test_validate_minimal_fixture_structure() -> None:
    """Fixture used by pytest must stay valid after schema changes."""
    raw = {
        "ui": {
            "password": "x",
            "session_secret": "1234567890123456",
        },
        "redis": {
            "url": "redis://127.0.0.1:6379/0",
            "queue_key": "q",
            "job_key_prefix": "j:",
            "job_log_suffix": ":log",
        },
        "proxmox": {"host": "127.0.0.1"},
        "vmware": {},
        "worker": {},
    }
    out = validate_loaded_config(raw)
    assert out["redis"]["url"] == "redis://127.0.0.1:6379/0"
    assert "winrm" not in out
    assert "horizon" not in out


def test_validate_rejects_short_session_secret() -> None:
    raw = {
        "ui": {"password": "p", "session_secret": "short"},
        "redis": {"url": "redis://x", "queue_key": "q", "job_key_prefix": "j:", "job_log_suffix": ":log"},
        "proxmox": {"host": "h"},
    }
    with pytest.raises(ValidationError):
        validate_loaded_config(raw)


def test_validate_preserves_unknown_top_level_keys() -> None:
    raw = {
        "ui": {"password": "p", "session_secret": "1234567890123456"},
        "redis": {"url": "redis://x", "queue_key": "q", "job_key_prefix": "j:", "job_log_suffix": ":log"},
        "proxmox": {"host": "h"},
        "future_feature_flag": {"x": 1},
    }
    out = validate_loaded_config(raw)
    assert out.get("future_feature_flag") == {"x": 1}


def test_validate_strips_removed_winrm_horizon_sections() -> None:
    raw = {
        "ui": {"password": "p", "session_secret": "1234567890123456"},
        "redis": {"url": "redis://x", "queue_key": "q", "job_key_prefix": "j:", "job_log_suffix": ":log"},
        "proxmox": {"host": "h"},
        "winrm": {"port": 5985},
        "horizon": {"powershell_script": "Write-Host hi"},
    }
    out = validate_loaded_config(raw)
    assert "winrm" not in out
    assert "horizon" not in out


def test_validate_rejects_empty_proxmox_host() -> None:
    raw = {
        "ui": {"password": "p", "session_secret": "1234567890123456"},
        "redis": {"url": "redis://x", "queue_key": "q", "job_key_prefix": "j:", "job_log_suffix": ":log"},
        "proxmox": {"host": ""},
    }
    with pytest.raises(ValidationError):
        validate_loaded_config(raw)
