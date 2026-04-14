# Offline Docker Troubleshooting (ARM/AMD64 + Compose v1)

This runbook captures the exact deployment issues seen when shipping this project from a Mac (ARM) to an offline x86_64 Linux host.

---

## 1) Symptom: Redis keeps restarting with `exec format error`

### What you see

- `docker logs migration_redis_1` shows:
  - `exec /usr/local/bin/docker-entrypoint.sh: exec format error`
- `docker ps` shows Redis restarting.

### Root cause

Wrong image architecture on remote host (e.g. `redis:7-alpine` is `arm64` on an `x86_64` server).

### Confirm

```bash
uname -m
docker image inspect redis:7-alpine --format 'arch={{.Architecture}}'
docker image inspect migration-engine:latest --format 'arch={{.Architecture}}'
```

For a Proxmox utility server (`x86_64`), both images must be `amd64`.

### Fix

1. Build/export an **amd64** bundle on a connected machine.
2. On remote:

```bash
docker-compose down --remove-orphans || true
docker rm -f $(docker ps -aq --filter name=migration_) 2>/dev/null || true
docker image rm redis:7-alpine migration-engine:latest 2>/dev/null || true
docker load -i migration-bundle-amd64.tar
docker image inspect redis:7-alpine --format 'arch={{.Architecture}}'
```

---

## 2) Symptom: `KeyError: 'ContainerConfig'` from `docker-compose`

### What you see

- `docker-compose up -d --force-recreate` crashes with Python traceback ending in:
  - `KeyError: 'ContainerConfig'`

### Root cause

Known bug/compat issue in legacy `docker-compose` v1.29.2 with newer Docker image metadata.

### Fix (safe with v1)

Avoid `--force-recreate`. Use full down/up:

```bash
docker-compose down --remove-orphans
docker rm -f $(docker ps -aq --filter name=migration_) 2>/dev/null || true
docker-compose up -d
```

Long-term: move to Compose v2 plugin (`docker compose ...`) when possible.

---

## 3) Symptom: `network ... not found` during container start

### What you see

- Error like:
  - `failed to set up container networking: network <id> not found`

### Root cause

Stale Compose v1 container metadata points to an old/deleted network id.

### Fix

```bash
docker-compose down --remove-orphans || true
docker rm -f $(docker ps -aq --filter name=migration_) 2>/dev/null || true
docker network rm migration_migration 2>/dev/null || true
docker network prune -f
docker-compose up -d
```

---

## 4) Symptom: API/worker reported unhealthy unexpectedly

### What you see

- Compose says services unhealthy while they appear running.

### Root cause

Image-level healthcheck in `Dockerfile` is web-based; worker container runs no HTTP server.

### Current compose fix (already in this repo)

- `api` healthcheck explicitly probes `/version` (liveness)
- `worker` healthcheck is disabled

Check in `docker-compose.yml`:

```yaml
api:
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/version', timeout=5)"]

worker:
  healthcheck:
    disable: true
```

---

## 5) Symptom: `/health` hangs during early setup

### What you see

- `curl http://127.0.0.1:8000/health` appears stuck.

### Root cause

`/health` performs dependency checks (including Proxmox), so network timeouts can delay response.

### Safer checks

```bash
curl -s http://127.0.0.1:8000/version
curl -sS --max-time 5 http://127.0.0.1:8000/health
```

Use `/version` for fast liveness; `/health` for readiness/dependency state.

---

## 6) Known-good bring-up sequence (offline host)

```bash
cd /srv/migration
docker-compose down --remove-orphans || true
docker rm -f $(docker ps -aq --filter name=migration_) 2>/dev/null || true
docker network rm migration_migration 2>/dev/null || true

docker load -i migration-bundle-amd64.tar
docker image inspect redis:7-alpine --format 'arch={{.Architecture}}'
docker image inspect migration-engine:latest --format 'arch={{.Architecture}}'

docker-compose up -d redis
docker-compose ps
docker logs --tail 50 migration_redis_1

docker-compose up -d api worker
docker-compose ps
```

Expected steady state:

- `migration_redis_1` → `Up (healthy)`
- `migration_api_1` → `Up (healthy)`
- `migration_worker_1` → `Up`

