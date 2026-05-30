# Migration orchestrator

FastAPI dashboard and API plus a Redis-backed **worker** that runs VMware → Proxmox (QEMU) migrations: disk import, first-boot on SATA, driver check, SCSI disk move, and optional guest automation over WinRM.

## What you need

- **Python** 3.10+ (3.11+ recommended)
- **Redis** (job state, logs, queue, ESXi import permit pools)
- **config.yaml** at the default path or pointed to by **`MIGRATION_ENGINE_CONFIG`**
- Network reachability from the API host and the worker to **Proxmox** and (if used) **vCenter** and guests (**WinRM**)

`config.yaml` is validated at startup (API, worker, and dashboard credential loads). Invalid or incomplete config fails fast with a clear error instead of dying mid-migration.

## Quick start (development)

```bash
cd migration-engine
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp config.docker.example.yaml config.yaml   # or start from your own file
# Edit config.yaml: ui.password, ui.session_secret (≥16 chars), redis.url, proxmox.*, etc.

# Terminal 1 — API + static UI (adjust host/port as needed)
uvicorn main:app --host 0.0.0.0 --port 8000

# Terminal 2 — worker (one process; scale with multiple terminals or containers)
python worker.py
```

Open **http://127.0.0.1:8000/** and sign in with `ui.password`. The footer shows **version** (and optional **build** id from `MIGRATION_ENGINE_GIT_REVISION`). Public JSON: `GET /version`, `GET /health`.

## Docker

See **[DOCKER.md](DOCKER.md)** for Compose (Redis + API + worker). Scale workers for more dequeue capacity, for example:

```bash
docker compose up -d --scale worker=4
```

Import parallelism is still governed by **`worker.max_concurrent_imports_per_esxi_storage`** and per-storage permit buckets in Redis — see **[docs/import-permits-and-parallelism.md](docs/import-permits-and-parallelism.md)**.

## Worker shutdown (SIGTERM / SIGINT)

The worker stops dequeuing new queue entries after **SIGTERM** or **CTRL+C (SIGINT)**. Any job id already removed from the queue but not yet started is **pushed back** onto the queue. In-flight migrations run to completion on the thread pool before the process exits (Compose `stop_grace_period` should allow enough time for long imports).

## Configuration

- **Example / template:** [config.docker.example.yaml](config.docker.example.yaml)
- **Horizon / WinRM / resume:** [docs/](docs/)
- **Ideas backlog:** [PRODUCT.md](PRODUCT.md)

## Tests

```bash
export MIGRATION_ENGINE_CONFIG="$(pwd)/tests/fixtures/minimal_config.yaml"
pytest
```

Some tests use **fakeredis** (see `requirements-dev.txt`) for queue/job-hash behaviour without a real Redis server. API tests exercise authenticated `/jobs` and `/migration-defaults` with Proxmox calls mocked.

### Self-hosted CI on Docker (e.g. Portainer)

Use **`Dockerfile.ci`** + **`docker-compose.ci.yml`**: the image installs dev dependencies and runs `pytest` with **`tests/fixtures/minimal_config.yaml` only** (no production secrets in the build context).

```bash
docker compose -f docker-compose.ci.yml run --rm ci
```

In **Portainer**: build the image with **Dockerfile.ci** (build context = repo root), then either run **`docker compose -f docker-compose.ci.yml run --rm ci`** from the VM shell on a **cron** timer, or add a stack from `docker-compose.ci.yml` and use **Duplicate/Edit** after each merge if you prefer a button-driven rebuild. Do not mount production `config.yaml`; the fixture baked into the image is enough for pytest.

#### Automating CI (no manual uploads)

You do not need to upload the project by hand if the Docker VM can **`git pull`** from **your** Git server (SSH deploy key, or HTTPS with a read-only token).

1. **One-time:** on the VM, `git clone <your-internal-repo-url>` to e.g. `/opt/migration-engine`.
2. **Schedule:** use **cron** or a **systemd timer** to run **`scripts/ci-pull-and-run.sh`** (sets `CI_REPO_DIR` if the clone is not next to `scripts/`). That script pulls latest `main`/`master` and runs `docker compose … run --rm ci`.

**Push-triggered (optional):** if you use **Forgejo / Gitea / GitLab** on the LAN, add a **webhook** on push that `curl`s an HTTPS or HTTP URL on the VM that runs the same script (protect it with a **shared secret** query param or basic auth, or bind to localhost and call it over SSH from the git server). That gives “CI on every push” without any public SaaS.

**Portainer “stack from Git”:** if your Portainer version supports deploying a stack from a **private Git** URL with credentials, it can pull the compose file on each **redeploy**; you still need a step that runs **tests** (e.g. a second stack or the cron script above), because the main `docker-compose.yml` is for the app, not pytest.

## Build metadata (optional)

Set **`MIGRATION_ENGINE_GIT_REVISION`** in the environment (e.g. Docker `ARG` / `ENV` at build time) so `/version` and the UI footer show a short git SHA or pipeline id.
