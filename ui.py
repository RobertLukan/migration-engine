"""
Dashboard auth, credential editing (config.yaml), and connectivity tests.
"""

from __future__ import annotations

import copy
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from pyVim.connect import Disconnect, SmartConnect
from proxmoxer import ProxmoxAPI

# CONFIG_PATH set by main after import to avoid circular import
CONFIG_PATH: Path | None = None


def _config_path() -> Path:
    if CONFIG_PATH is None:
        raise RuntimeError("ui.CONFIG_PATH not set")
    return CONFIG_PATH


def load_yaml() -> dict[str, Any]:
    try:
        from config_validate import validate_loaded_config
    except ModuleNotFoundError:
        def validate_loaded_config(data: Any) -> dict[str, Any]:
            if data is None:
                raise ValueError("config file is empty")
            if not isinstance(data, dict):
                raise TypeError(f"config root must be a mapping, got {type(data).__name__}")
            return data

    with _config_path().open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raise ValueError("config file is empty")
    return validate_loaded_config(raw)


def save_yaml(cfg: dict[str, Any]) -> None:
    with _config_path().open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            cfg,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )


MASK = "********"


def mask_vmware(v: dict[str, Any]) -> dict[str, Any]:
    out = {k: v[k] for k in ("host", "port", "user", "insecure") if k in v}
    out["host"] = v.get("host", "")
    out["port"] = int(v.get("port", 443))
    out["user"] = v.get("user", "")
    out["insecure"] = bool(v.get("insecure", False))
    out["password_set"] = bool(v.get("password"))
    out["password"] = MASK if out["password_set"] else ""
    return out


def mask_proxmox(p: dict[str, Any]) -> dict[str, Any]:
    out = {
        "host": p.get("host", ""),
        "port": int(p.get("port", 8006)),
        "verify_ssl": bool(p.get("verify_ssl", True)),
        "default_node": p.get("default_node", ""),
        "api_token_id": p.get("api_token_id", ""),
        "user": p.get("user", ""),
        "storage": p.get("storage", ""),
        "bridge": p.get("bridge", ""),
        "vlan": p.get("vlan", ""),
        "net_mtu": p.get("net_mtu", 1500),
    }
    tok = bool((p.get("api_token_secret") or "").strip())
    pwd = bool((p.get("password") or "").strip())
    out["api_token_secret_set"] = tok
    out["api_token_secret"] = MASK if tok else ""
    out["password_set"] = pwd
    out["password"] = MASK if pwd else ""
    return out


def require_ui_session(request: Request) -> None:
    if not request.session.get("ui_authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")


class LoginBody(BaseModel):
    password: str = Field(..., min_length=1)


class VMwarePartial(BaseModel):
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    insecure: bool | None = None


class ProxmoxPartial(BaseModel):
    host: str | None = None
    port: int | None = None
    verify_ssl: bool | None = None
    default_node: str | None = None
    api_token_id: str | None = None
    api_token_secret: str | None = None
    user: str | None = None
    password: str | None = None
    storage: str | None = None
    bridge: str | None = None
    vlan: int | str | None = None
    net_mtu: int | None = None


class CredentialsPut(BaseModel):
    vmware: VMwarePartial | None = None
    proxmox: ProxmoxPartial | None = None


class VMwareTestOverrides(BaseModel):
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    insecure: bool | None = None


class ProxmoxTestOverrides(BaseModel):
    host: str | None = None
    port: int | None = None
    verify_ssl: bool | None = None
    api_token_id: str | None = None
    api_token_secret: str | None = None
    user: str | None = None
    password: str | None = None


router = APIRouter(tags=["ui"])


SESSION_CREDENTIALS_VERIFIED = "credentials_verified"


@router.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, bool]:
    """Includes credentials_verified so the SPA does not need a separate /api/auth/gate call."""
    authed = bool(request.session.get("ui_authenticated"))
    cfg = load_yaml()
    return {
        "authenticated": authed,
        "credentials_verified": bool(request.session.get(SESSION_CREDENTIALS_VERIFIED)) if authed else False,
        "vmware_configured": vmware_credentials_configured(cfg),
    }


@router.get("/api/auth/gate")
def auth_gate(request: Request) -> dict[str, Any]:
    """Deprecated: use GET /api/auth/status (includes credentials_verified). Kept for older clients."""
    authed = bool(request.session.get("ui_authenticated"))
    if not authed:
        return {"authenticated": False, "credentials_verified": False, "vmware_configured": False}
    cfg = load_yaml()
    return {
        "authenticated": True,
        "credentials_verified": bool(request.session.get(SESSION_CREDENTIALS_VERIFIED)),
        "vmware_configured": vmware_credentials_configured(cfg),
    }


def vmware_credentials_configured(cfg: dict[str, Any]) -> bool:
    """True when host, user, and password are all non-empty (user intends to use vCenter)."""
    vc = cfg.get("vmware") or {}
    return all(str(vc.get(k) or "").strip() for k in ("host", "user", "password"))


def _verify_vmware_saved(cfg: dict[str, Any]) -> tuple[bool, str]:
    vc = cfg.get("vmware") or {}
    for k in ("host", "user", "password"):
        if not (str(vc.get(k) or "").strip()):
            return False, f"vmware.{k} is missing (save credentials first)"
    try:
        with _vc_from_dict(vc) as si:
            _ = si.RetrieveContent().rootFolder.name
        return True, "vCenter reachable"
    except Exception as exc:
        return False, str(exc)


def _verify_proxmox_saved(cfg: dict[str, Any]) -> tuple[bool, str]:
    px = cfg.get("proxmox") or {}
    if not (str(px.get("host") or "").strip()):
        return False, "proxmox.host is missing"
    try:
        api = _proxmox_from_dict(px)
        api.version.get()
        return True, "Proxmox API reachable"
    except Exception as exc:
        return False, str(exc)


def health_proxmox_component(cfg: dict[str, Any]) -> dict[str, Any]:
    """Required dependency for /health (orchestrator + UI core)."""
    ok, msg = _verify_proxmox_saved(cfg)
    return {"ok": ok, "detail": msg}


def health_vmware_component(cfg: dict[str, Any]) -> dict[str, Any]:
    """Optional; failures do not fail overall /health status."""
    if not vmware_credentials_configured(cfg):
        return {"ok": True, "detail": "not configured (optional)", "optional": True}
    ok, msg = _verify_vmware_saved(cfg)
    return {"ok": ok, "detail": msg, "optional": True}


def _complete_credentials_check_impl(request: Request) -> dict[str, Any]:
    cfg = load_yaml()
    px_ok, px_msg = _verify_proxmox_saved(cfg)
    vw_present = vmware_credentials_configured(cfg)
    vw_ok, vw_msg = (False, "VMware not configured (optional — guests are listed from Proxmox ESXi storage)")
    if vw_present:
        vw_ok, vw_msg = _verify_vmware_saved(cfg)

    if px_ok:
        request.session[SESSION_CREDENTIALS_VERIFIED] = True
        return {
            "ok": True,
            "vmware": vw_msg,
            "proxmox": px_msg,
            "vmware_configured": vw_present,
            "vmware_ok": vw_ok if vw_present else None,
        }
    request.session.pop(SESSION_CREDENTIALS_VERIFIED, None)
    return {
        "ok": False,
        "vmware_ok": vw_ok if vw_present else None,
        "vmware_message": vw_msg,
        "proxmox_ok": px_ok,
        "proxmox_message": px_msg,
        "vmware_configured": vw_present,
    }


@router.post("/api/auth/complete-credentials-check", dependencies=[Depends(require_ui_session)])
def complete_credentials_check(request: Request) -> dict[str, Any]:
    """Verify VMware + Proxmox using saved config; mark session so Home shows the VM list."""
    return _complete_credentials_check_impl(request)


@router.post("/api/ui/complete-onboarding", dependencies=[Depends(require_ui_session)])
def complete_onboarding(request: Request) -> dict[str, Any]:
    """Same as POST /api/auth/complete-credentials-check (under /api/ui for environments where auth paths differ)."""
    return _complete_credentials_check_impl(request)


def _reopen_credentials_impl(request: Request) -> dict[str, str]:
    request.session[SESSION_CREDENTIALS_VERIFIED] = False
    return {"ok": "true"}


@router.post("/api/auth/reopen-credentials", dependencies=[Depends(require_ui_session)])
def reopen_credentials(request: Request) -> dict[str, str]:
    """Return to the credentials-first flow (does not clear saved passwords)."""
    return _reopen_credentials_impl(request)


@router.post("/api/ui/reopen-credentials-setup", dependencies=[Depends(require_ui_session)])
def reopen_credentials_setup(request: Request) -> dict[str, str]:
    """Same as POST /api/auth/reopen-credentials."""
    return _reopen_credentials_impl(request)


@router.post("/api/auth/login")
def auth_login(request: Request, body: LoginBody) -> dict[str, str]:
    cfg = load_yaml()
    expected = (cfg.get("ui") or {}).get("password") or ""
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ui.password is not set in config.yaml",
        )
    a = body.password.encode("utf-8")
    b = expected.encode("utf-8")
    try:
        ok = secrets.compare_digest(a, b)
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid password")
    request.session["ui_authenticated"] = True
    return {"ok": "true"}


@router.post("/api/auth/logout")
def auth_logout(request: Request) -> dict[str, str]:
    request.session.clear()
    return {"ok": "true"}


@router.get("/api/ui/credentials", dependencies=[Depends(require_ui_session)])
def get_credentials() -> dict[str, Any]:
    cfg = load_yaml()
    return {
        "vmware": mask_vmware(cfg.get("vmware") or {}),
        "proxmox": mask_proxmox(cfg.get("proxmox") or {}),
    }


@router.put("/api/ui/credentials", dependencies=[Depends(require_ui_session)])
def put_credentials(body: CredentialsPut) -> dict[str, str]:
    cfg = load_yaml()
    if body.vmware is not None:
        vm = cfg.setdefault("vmware", {})
        patch = body.vmware.model_dump(exclude_unset=True)
        if "password" in patch:
            if patch["password"] == MASK:
                del patch["password"]
            elif patch["password"] == "":
                vm["password"] = ""
            else:
                vm["password"] = patch["password"]
            del patch["password"]
        vm.update(patch)
    if body.proxmox is not None:
        px = cfg.setdefault("proxmox", {})
        patch = body.proxmox.model_dump(exclude_unset=True)
        for secret_key in ("api_token_secret", "password"):
            if secret_key in patch:
                val = patch[secret_key]
                if val == MASK:
                    pass
                elif val == "":
                    px[secret_key] = ""
                else:
                    px[secret_key] = val
                del patch[secret_key]
        px.update(patch)
    save_yaml(cfg)
    return {"ok": "true"}


@contextmanager
def _vc_from_dict(vc: dict[str, Any]) -> Iterator[Any]:
    si = SmartConnect(
        host=vc["host"],
        user=vc["user"],
        pwd=vc["password"],
        port=int(vc.get("port", 443)),
        disableSslCertValidation=bool(vc.get("insecure", False)),
    )
    try:
        yield si
    finally:
        Disconnect(si)


def _proxmox_from_dict(p: dict[str, Any]) -> ProxmoxAPI:
    token_id = (p.get("api_token_id") or "").strip()
    token_secret = (p.get("api_token_secret") or "").strip()
    if token_id and token_secret:
        user, sep, token_name = token_id.partition("!")
        user = user.strip()
        token_name = token_name.strip()
        if not sep or not user or not token_name:
            raise ValueError(
                "Proxmox: api_token_id must look like 'user@realm!token_name' "
                "(example: root@pam!migration)"
            )
        return ProxmoxAPI(
            host=p["host"],
            user=user,
            token_name=token_name,
            token_value=token_secret,
            port=int(p.get("port", 8006)),
            verify_ssl=bool(p.get("verify_ssl", True)),
        )
    user = (p.get("user") or "").strip()
    password = (p.get("password") or "").strip()
    if not user or not password:
        raise ValueError("Proxmox: need api_token_id + api_token_secret or user + password")
    return ProxmoxAPI(
        host=p["host"],
        user=user,
        password=password,
        port=int(p.get("port", 8006)),
        verify_ssl=bool(p.get("verify_ssl", True)),
    )


def _storage_content_list(entry: dict[str, Any]) -> list[str]:
    c = entry.get("content")
    if c is None:
        return []
    if isinstance(c, str):
        return [x.strip() for x in c.split(",") if x.strip()]
    if isinstance(c, list):
        return [str(x).strip() for x in c if str(x).strip()]
    if isinstance(c, dict):
        return [k for k, v in c.items() if v]
    return []


def _as_bool_int(v: Any, default: int = 1) -> bool:
    if v is None:
        return bool(default)
    try:
        return int(v) != 0
    except (TypeError, ValueError):
        return bool(v)


@router.get("/api/ui/proxmox-storages", dependencies=[Depends(require_ui_session)])
def list_proxmox_storages(node: str | None = None) -> dict[str, Any]:
    """
    List storages on a Proxmox node (for ZFS/RBD/etc. as migration targets).
    `supports_disk_import` is true when `images` is in the storage content list.
    """
    cfg = load_yaml()
    px = cfg.get("proxmox") or {}
    nod = (node or px.get("default_node") or "").strip()
    if not nod:
        raise HTTPException(
            status_code=400,
            detail="Pass ?node=pve (or set proxmox.default_node in config.yaml).",
        )
    try:
        api = _proxmox_from_dict(px)
        raw = api.nodes(nod).storage.get()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Proxmox API: {exc}") from exc

    if not isinstance(raw, list):
        raw = []

    storages: list[dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        sid = s.get("storage")
        if not sid:
            continue
        contents = _storage_content_list(s)
        supports = "images" in contents
        enabled = _as_bool_int(s.get("enabled"), 1)
        try:
            active = int(s.get("active", 0))
        except (TypeError, ValueError):
            active = 1 if s.get("active") else 0
        storages.append(
            {
                "id": str(sid),
                "type": str(s.get("type", "")),
                "content": contents,
                "supports_disk_import": supports,
                "enabled": enabled,
                "active": active,
                "shared": _as_bool_int(s.get("shared"), 0),
                "usable_for_vm_disks": supports and enabled and active == 1,
            }
        )

    storages.sort(key=lambda x: (not x["usable_for_vm_disks"], not x["supports_disk_import"], x["id"]))
    return {"node": nod, "storages": storages}


@router.get("/api/ui/proxmox-bridges", dependencies=[Depends(require_ui_session)])
def list_proxmox_bridges(node: str | None = None) -> dict[str, Any]:
    """
    Linux ``vmbr*`` bridge names on a node (from ``GET /nodes/{node}/network``), for UI dropdowns.
    """
    cfg = load_yaml()
    px = cfg.get("proxmox") or {}
    nod = (node or px.get("default_node") or "").strip()
    if not nod:
        raise HTTPException(
            status_code=400,
            detail="Pass ?node=pve (or set proxmox.default_node in config.yaml).",
        )
    try:
        api = _proxmox_from_dict(px)
        raw = api.nodes(nod).network.get()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Proxmox API: {exc}") from exc

    if not isinstance(raw, list):
        raw = []

    bridges: list[str] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        iface = str(row.get("iface") or "").strip()
        if not iface:
            continue
        typ = str(row.get("type") or "").strip().lower()
        if typ == "bridge" and iface.lower().startswith("vmbr"):
            bridges.append(iface)

    bridges = sorted(set(bridges), key=lambda s: (len(s), s.lower()))
    return {"node": nod, "bridges": bridges}


def esxi_import_uri_shallow_alias(uri: str, vm_name: str) -> bool:
    """
    True for ``esxi:<segment>/<vmname>/<vmname>.vmdk`` or the same with ``.vmx`` (exactly three
    path segments after ``esxi:``). Some PVE setups mis-resolve this layout while deeper paths work;
    inventory and discover **deprioritize** these vs longer paths but still use them when nothing
    else matches (many real VMs only have this layout).
    """
    name = (vm_name or "").strip()
    if not name:
        return False
    low = uri.lower()
    pos = low.find("esxi:")
    if pos < 0:
        return False
    tail = uri[pos + 5 :].strip().strip("/")
    parts = [p for p in tail.split("/") if p]
    if len(parts) != 3:
        return False
    folder, fn = parts[1], parts[2]
    n = name.lower()
    fl = fn.lower()
    return folder.lower() == n and (fl == f"{n}.vmdk" or fl == f"{n}.vmx")


def _esxi_uri_from_volid(volid: str) -> str | None:
    """
    Normalize Proxmox ESXi volids to ``esxi:...`` URI form.

    Proxmox variants seen in the wild:
    - ``...esxi:dc/path/file.vmdk`` (already normalized)
    - ``<storage-id>:dc/path/file.vmdk`` (common in pvesh output)
    """
    raw = (volid or "").strip()
    if not raw:
        return None
    low = raw.lower()
    pos = low.find("esxi:")
    if pos >= 0:
        return raw[pos:].strip()
    if ":" in raw:
        _sid, _sep, tail = raw.partition(":")
        t = tail.strip().strip("/")
        if t:
            return "esxi:" + t
    return None


def _listing_item_size_bytes(item: dict[str, Any]) -> int:
    """
    Bytes from a Proxmox ``/nodes/{node}/storage/{id}/content`` row (``size`` field).
    The API may return numbers as int, float, or string (including ``\"…\"`` floats); missing or
    non-positive values yield 0.
    """
    v = item.get("size")
    if v is None or v == "":
        return 0
    try:
        if isinstance(v, bool):
            return 0
        if isinstance(v, int):
            return v if v > 0 else 0
        if isinstance(v, float):
            n = int(v)
            return n if n > 0 else 0
        s = str(v).strip()
        if not s:
            return 0
        n = int(float(s))
        return n if n > 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _esxi_uri_parent_directory_lower(uri: str) -> str | None:
    """Lowercased ``esxi:…/dir`` containing the file (VM folder), or None."""
    u = (uri or "").strip()
    if not u.lower().startswith("esxi:"):
        return None
    tail = u[5:].strip().strip("/")
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return None
    return ("esxi:" + "/".join(parts[:-1])).lower()


def _guess_esxi_vmdk_from_vmx_uri(uri: str) -> list[str]:
    """
    Derive likely ``.vmdk`` import URIs from a ``.vmx`` URI.
    Primary candidate swaps only the extension; secondary candidate uses parent folder name.
    """
    u = (uri or "").strip()
    if not u.lower().startswith("esxi:"):
        return []
    tail = u[5:].strip().strip("/")
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return []
    file = parts[-1]
    if not file.lower().endswith(".vmx"):
        return []
    stem = file[:-4]
    parent = parts[-2]
    base = parts[:-1]
    out: list[str] = []
    # Most common case: VM config and primary descriptor share basename.
    out.append("esxi:" + "/".join([*base, f"{stem}.vmdk"]))
    # Fallback for layouts where the descriptor follows folder name.
    if parent.lower() != stem.lower():
        out.append("esxi:" + "/".join([*base, f"{parent}.vmdk"]))
    # Preserve order but deduplicate.
    seen: set[str] = set()
    dedup: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            dedup.append(c)
    return dedup


def _guess_import_refs_from_vmx(vmx_uri: str, vmx_volid: str) -> list[str]:
    """
    Guess ``import-from`` references for a VMX entry while preserving storage-id prefix when available.

    Example:
    - vmx_volid: ``sh12:ha-datacenter/DS1/VM/VM.vmx``
    - guessed uri: ``esxi:ha-datacenter/DS1/VM/VM.vmdk``
    -> import ref: ``sh12:ha-datacenter/DS1/VM/VM.vmdk``
    """
    guesses = _guess_esxi_vmdk_from_vmx_uri(vmx_uri)
    if not guesses:
        return []

    refs: list[str] = []
    raw = (vmx_volid or "").strip()
    sid = ""
    if raw and ":" in raw and "esxi:" not in raw.lower():
        sid, _sep, _tail = raw.partition(":")
        sid = sid.strip()

    if sid:
        for g in guesses:
            gl = g.lower()
            if gl.startswith("esxi:"):
                refs.append(f"{sid}:{g[5:]}")
            else:
                refs.append(f"{sid}:{g}")
    refs.extend(guesses)

    out: list[str] = []
    seen: set[str] = set()
    for r in refs:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _inventory_uri_rank(uri: str, name_lower: str, *, ext: str) -> tuple[int, int, int, int]:
    """
    Sort key for picking a canonical disk/config path. Higher rank is better; last int is 1 for
    .vmdk so we prefer a disk over a .vmx when both represent the same guest.
    """
    ulow = uri.lower()
    nlow = name_lower
    if ext == "vmdk":
        if ulow.endswith(f"/{nlow}/{nlow}.vmdk"):
            rank = 300
        elif f"/{nlow}/" in ulow:
            rank = 200
        elif ulow.endswith(f"/{nlow}.vmdk"):
            rank = 150
        else:
            rank = 50
        prefer_disk = 1
    elif ext == "vmx":
        if ulow.endswith(f"/{nlow}/{nlow}.vmx"):
            rank = 300
        elif f"/{nlow}/" in ulow:
            rank = 200
        elif ulow.endswith(f"/{nlow}.vmx"):
            rank = 150
        else:
            rank = 50
        prefer_disk = 0
    else:
        rank = 0
        prefer_disk = 0
    tail = uri[5:].strip("/") if uri.lower().startswith("esxi:") else uri.strip("/")
    depth = len([p for p in tail.split("/") if p])
    return (depth, rank, 0, prefer_disk)


def discover_esxi_import_from_pve(node: str, vm_name: str) -> str | None:
    """
    Resolve ``import-from`` as ``esxi:…`` by listing ESXi-type storages on a PVE node.
    This matches how the hypervisor enumerates sources for disk import; no vCenter API call.
    If no ``.vmdk`` matches by name, falls back to any ``.vmdk`` in the same directory as a
    matching ``.vmx`` (some PVE listings surface vmx but not every vmdk in name-based search).
    Returns None if nothing matches (wrong node, permissions, or VM not visible on linked ESXi).
    """
    name = (vm_name or "").strip()
    nod = (node or "").strip()
    if not name or not nod:
        return None
    try:
        cfg = load_yaml()
        px = cfg.get("proxmox") or {}
        api = _proxmox_from_dict(px)
        raw = api.nodes(nod).storage.get()
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    esxi_ids = [
        str(s.get("storage"))
        for s in raw
        if isinstance(s, dict)
        and str(s.get("type", "")).lower() == "esxi"
        and str(s.get("storage", "")).strip()
    ]
    # (depth, rank, size, prefer, uri_norm, shallow, import_ref)
    # import_ref is what we actually send to ``import-from`` (usually original volid from PVE).
    scored: list[tuple[int, int, int, int, str, int, str]] = []
    nlow = name.lower()
    vmx_dirs: set[str] = set()
    vmx_uris: list[tuple[int, int, str, int, str]] = []

    for sid in esxi_ids:
        try:
            items = api.nodes(nod).storage(sid).content.get()
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            volid = str(it.get("volid") or "").strip()
            if not volid:
                continue
            uri = _esxi_uri_from_volid(volid)
            if not uri:
                continue
            vlow = uri.lower()
            size = _listing_item_size_bytes(it)

            if vlow.endswith(".vmx"):
                label = _guest_label_from_esxi_path(uri)
                if label and label.strip().lower() == nlow:
                    d = _esxi_uri_parent_directory_lower(uri)
                    if d:
                        vmx_dirs.add(d)
                    depth, rank, _, _ = _inventory_uri_rank(uri, nlow, ext="vmx")
                    shallow = 1 if esxi_import_uri_shallow_alias(uri, name) else 0
                    vmx_uris.append((depth, rank, uri, shallow, volid))
                continue

            if not vlow.endswith(".vmdk"):
                continue
            vlabel = _guest_label_from_esxi_path(uri)
            if not vlabel or vlabel.strip().lower() != nlow:
                continue
            depth, rank, _, prefer = _inventory_uri_rank(uri, nlow, ext="vmdk")
            shallow = 1 if esxi_import_uri_shallow_alias(uri, name) else 0
            scored.append((depth, rank, size, prefer, uri, shallow, volid))

    if scored:
        scored.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3], x[5]))
        return scored[0][6]

    if not vmx_dirs:
        return None

    for sid in esxi_ids:
        try:
            items = api.nodes(nod).storage(sid).content.get()
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            volid = str(it.get("volid") or "").strip()
            if not volid or not volid.lower().endswith(".vmdk"):
                continue
            uri = _esxi_uri_from_volid(volid)
            if not uri:
                continue
            parent = _esxi_uri_parent_directory_lower(uri)
            if not parent or parent not in vmx_dirs:
                continue
            size = _listing_item_size_bytes(it)
            depth, rank, _, prefer = _inventory_uri_rank(uri, nlow, ext="vmdk")
            shallow = 1 if esxi_import_uri_shallow_alias(uri, name) else 0
            scored.append((depth, rank, size, prefer, uri, shallow, volid))

    if not scored:
        if not vmx_uris:
            return None
        vmx_uris.sort(key=lambda x: (-x[0], -x[1], x[3]))
        for _depth, _rank, vmx_uri, _shallow, vmx_volid in vmx_uris:
            guessed = _guess_import_refs_from_vmx(vmx_uri, vmx_volid)
            if guessed:
                return guessed[0]
        return None
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3], x[5]))
    return scored[0][6]


def _guest_label_from_esxi_path(uri: str) -> str | None:
    """
    Infer a guest name from an esxi:… path ending in ``.vmdk`` or ``.vmx`` (folder name when it
    matches the file stem, else stem). Used for inventory and vmx-directory fallback in discover.
    """
    u = (uri or "").strip()
    if not u.lower().startswith("esxi:"):
        return None
    tail = u[5:].strip().strip("/")
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return None
    file = parts[-1]
    low = file.lower()
    if low.endswith(".vmdk"):
        stem = file[:-5]
    elif low.endswith(".vmx"):
        stem = file[:-4]
    else:
        return None
    parent = parts[-2]
    if parent.lower() == stem.lower():
        return parent
    return stem


def _guest_label_from_esxi_uri(uri: str) -> str | None:
    """Backward-compatible alias for :func:`_guest_label_from_esxi_path`."""
    return _guest_label_from_esxi_path(uri)


def enumerate_guests_from_pve_esxi(cfg: dict[str, Any], node: str | None) -> list[dict[str, Any]]:
    """
    List guests visible on Proxmox ESXi-linked storage (storage content API). No vCenter.
    Scans both ``.vmdk`` and ``.vmx`` volids (PVE may list one more reliably than the other).
    One row per inferred guest name; best path prefers a matching ``.vmdk`` when scores tie.

    Each row includes ``esxi_storage_ids``: sorted Proxmox **storage** ids (e.g. ``sh12``, ``sh13``) where
    that guest appeared in listings — used in the UI for import parallelism hints.

    ``esxi_listing_size_bytes`` is the ``size`` field from the storage content API for the chosen
    import candidate (metadata only; no VMX/VMDK file download). Values are parsed flexibly because
    some PVE/API layers return ``size`` as a string or float.
    """
    nod = (node or "").strip() or str((cfg.get("proxmox") or {}).get("default_node") or "").strip()
    if not nod:
        return []
    px = cfg.get("proxmox") or {}
    try:
        api = _proxmox_from_dict(px)
        raw = api.nodes(nod).storage.get()
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    esxi_ids = [
        str(s.get("storage"))
        for s in raw
        if isinstance(s, dict)
        and str(s.get("type", "")).lower() == "esxi"
        and str(s.get("storage", "")).strip()
    ]
    # shallow=1 deprioritized vs deeper paths (same guest may list both)
    # tuple: (depth, rank, size, prefer, uri_norm, shallow, import_ref, pve_storage_id)
    buckets: dict[str, list[tuple[int, int, int, int, str, int, str, str]]] = {}

    for sid in esxi_ids:
        try:
            items = api.nodes(nod).storage(sid).content.get()
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            volid = str(it.get("volid") or "").strip()
            if not volid:
                continue
            vlow = volid.lower()
            if not (vlow.endswith(".vmdk") or vlow.endswith(".vmx")):
                continue
            uri = _esxi_uri_from_volid(volid)
            if not uri:
                continue
            label = _guest_label_from_esxi_path(uri)
            if not label:
                continue
            size = _listing_item_size_bytes(it)
            k = label.lower()
            ext = "vmdk" if vlow.endswith(".vmdk") else "vmx"
            depth, rank, _, prefer = _inventory_uri_rank(uri, k, ext=ext)
            shallow = 1 if esxi_import_uri_shallow_alias(uri, label) else 0
            buckets.setdefault(k, []).append((depth, rank, size, prefer, uri, shallow, volid, sid))

    out: list[dict[str, Any]] = []
    for _k, candidates in buckets.items():
        if not candidates:
            continue
        candidates.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3], x[5]))
        best_uri = candidates[0][4]
        best_import_ref = candidates[0][6]
        best_size = candidates[0][2]
        if best_uri.lower().endswith(".vmx"):
            vmdk_only = [c for c in candidates if c[4].lower().endswith(".vmdk")]
            if vmdk_only:
                vmdk_only.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3], x[5]))
                best_uri = vmdk_only[0][4]
                best_import_ref = vmdk_only[0][6]
                best_size = vmdk_only[0][2]
        display = _guest_label_from_esxi_path(best_uri) or _k
        import_uri = best_import_ref if best_uri.lower().endswith(".vmdk") else None
        guesses: list[str] = []
        if import_uri is None and best_uri.lower().endswith(".vmx"):
            guesses = _guess_import_refs_from_vmx(best_uri, best_import_ref)
            import_uri = guesses[0] if guesses else None
            if import_uri and guesses:
                ref_set = {str(r).strip() for r in guesses if str(r).strip()}
                norm_set: set[str] = set()
                for r in ref_set:
                    n = (_esxi_uri_from_volid(r) or "").strip().lower()
                    if n:
                        norm_set.add(n)
                best_size = 0
                for c in candidates:
                    vol = str(c[6] or "").strip()
                    if vol in ref_set:
                        best_size = c[2]
                        break
                    cn = (_esxi_uri_from_volid(vol) or "").strip().lower()
                    if cn and cn in norm_set:
                        best_size = c[2]
                        break
        storage_ids = sorted(
            {str(c[7]).strip() for c in candidates if len(c) > 7 and str(c[7]).strip()}
        )
        out.append(
            {
                "name": display,
                "vmware_moref": "",
                "uuid": None,
                "power_state": None,
                "inventory_source": "pve_esxi",
                "esxi_import_uri": import_uri,
                "esxi_storage_ids": storage_ids,
                "esxi_listing_size_bytes": best_size if best_size > 0 else None,
            }
        )
    out.sort(key=lambda x: x["name"].lower())
    return out


def _merge_vmware(base: dict[str, Any], over: VMwareTestOverrides | None) -> dict[str, Any]:
    m = copy.deepcopy(base)
    if not over:
        return m
    for k, v in over.model_dump(exclude_unset=True).items():
        if v is not None:
            m[k] = v
    return m


def _merge_proxmox(base: dict[str, Any], over: ProxmoxTestOverrides | None) -> dict[str, Any]:
    m = copy.deepcopy(base)
    if not over:
        return m
    for k, v in over.model_dump(exclude_unset=True).items():
        if v is not None:
            m[k] = v
    return m


@router.post("/api/ui/test/vmware", dependencies=[Depends(require_ui_session)])
def test_vmware(body: VMwareTestOverrides | None = None) -> dict[str, Any]:
    cfg = load_yaml()
    vc = _merge_vmware(cfg.get("vmware") or {}, body)
    need = ("host", "user", "password")
    for k in need:
        if not (vc.get(k) or "").strip():
            raise HTTPException(status_code=400, detail=f"vmware.{k} is required (save or send in request)")
    try:
        with _vc_from_dict(vc) as si:
            _ = si.RetrieveContent().rootFolder.name
        return {"ok": True, "message": "Connected to vCenter successfully"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@router.post("/api/ui/test/proxmox", dependencies=[Depends(require_ui_session)])
def test_proxmox(body: ProxmoxTestOverrides | None = None) -> dict[str, Any]:
    cfg = load_yaml()
    px = _merge_proxmox(cfg.get("proxmox") or {}, body)
    if not (px.get("host") or "").strip():
        raise HTTPException(status_code=400, detail="proxmox.host is required")
    try:
        api = _proxmox_from_dict(px)
        ver = api.version.get()
        return {
            "ok": True,
            "message": "Connected to Proxmox API",
            "version": ver,
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
