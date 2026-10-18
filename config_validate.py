"""
Validate ``config.yaml`` after YAML load so typos and missing required keys fail at startup.

Used by the API process, the worker, and the dashboard credential loader. Unknown top-level keys
are ignored; unknown keys under ``proxmox``, ``vmware``, and ``worker`` are preserved.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UiConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    password: str = Field(min_length=1, description="Dashboard login password")
    session_secret: str = Field(
        min_length=16,
        max_length=4096,
        description="Secret for signed browser sessions (use a long random string)",
    )


class RedisConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=1)
    queue_key: str = Field(min_length=1)
    job_key_prefix: str = Field(min_length=1)
    job_log_suffix: str = Field(min_length=1, default=":log")


class VmwareConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    host: str = ""
    port: int = 443
    user: str = ""
    password: str = ""
    insecure: bool = False


class ProxmoxConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    host: str = Field(min_length=1)
    port: int = Field(default=8006, ge=1, le=65535)
    api_timeout_sec: int = Field(default=30, ge=5, le=600)
    verify_ssl: bool = False
    default_node: str = ""
    api_token_id: str = ""
    api_token_secret: str = ""
    user: str = ""
    password: str = ""


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_concurrent_imports_per_esxi_storage: int = Field(default=1, ge=1, le=256)
    job_thread_pool_size: int = Field(default=24, ge=1, le=4096)


class MigrationEngineConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    ui: UiConfig
    redis: RedisConfig
    proxmox: ProxmoxConfig
    vmware: VmwareConfig = Field(default_factory=VmwareConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)


def validate_loaded_config(data: Any) -> dict[str, Any]:
    """
    Parse and validate a config mapping loaded from YAML.

    :raises pydantic.ValidationError: on invalid structure or out-of-range values
    :returns: A plain dict suitable for existing code paths (nested dicts, extra proxmox/worker keys)
    """
    if not isinstance(data, dict):
        raise TypeError(f"config root must be a mapping, got {type(data).__name__}")
    merged: dict[str, Any] = dict(data)
    # Drop removed top-level sections if present in older config.yaml files.
    merged.pop("winrm", None)
    merged.pop("horizon", None)
    if "worker" not in merged or merged["worker"] is None:
        merged["worker"] = {}
    if "vmware" not in merged or merged["vmware"] is None:
        merged["vmware"] = {}
    model = MigrationEngineConfig.model_validate(merged)
    return model.model_dump(mode="python")
