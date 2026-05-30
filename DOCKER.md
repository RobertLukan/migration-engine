# Docker (isolated / air-gapped testing)

Stack: **Redis**, **FastAPI UI + API**, **migration worker**. All three talk only on the internal Docker network except port **8000** published for the browser.

## Apple Silicon (M1/M2/M3) → Intel / AMD64 servers

By default **`docker-compose.yml` sets `platform: linux/amd64`** so images match typical Linux x86_64 hosts. On a Mac ARM build, Docker uses **QEMU emulation** for that platform (slower builds; normal runtime on the Intel box).

- **Verify what you built:** `docker image inspect migration-engine:latest --format '{{.Architecture}}'` → should print **`amd64`**.
- **CLI without compose:** `docker buildx build --platform linux/amd64 --load -t migration-engine:latest .`
- **Multi-arch manifest** (optional): `docker buildx build --platform linux/amd64,linux/arm64 -t yourregistry/migration-engine:1.0 --push .`

For **native ARM** images when you only run on Mac ARM, comment out the `platform: linux/amd64` lines in `docker-compose.yml`.

## Build (needs internet once)

From this directory:

```bash
docker compose build
```

That downloads base images (`python:3.12-slim-bookworm`, `redis:7-alpine`) and Python wheels, then bakes them into **migration-engine:latest**.

## Run (no internet)

```bash
docker compose up -d
```

Open **http://localhost:8000**. Log in with `ui.password` from the config file inside the image (default baked from `config.docker.example.yaml`: change it for anything beyond a throwaway lab).

Health: **GET http://localhost:8000/health** (checks Redis).

**Behind nginx (TLS, headers, upstream):** see **[NGINX.md](NGINX.md)** for commented `proxy_set_header` / `upstream` examples when this service sits behind an existing reverse proxy.

## Use your own config (secrets, Proxmox IPs)

1. Copy `config.docker.example.yaml` to `config.docker.yaml`.
2. Keep **`redis.url`** as **`redis://redis:6379/0`** (hostname must match the compose service name).
3. Set **`ui.password`** and **`ui.session_secret`** (session secret must be non-empty; long random string).
4. Uncomment the **`volumes`** lines under `api` and `worker` in `docker-compose.yml` so both processes read `./config.docker.yaml`.
5. `docker compose up -d --build` if you changed the compose file only, or restart containers.

## Redis already on the server (host) vs Redis in Docker

**Short answer:** it is **not** a problem for the host to run Redis and for Compose to also run the **`redis`** service. They are **separate instances** unless you point both at the same socket/port.

- **Default compose file** does **not** publish Redis on the host (`ports:` is only on `api`). The bundled `redis:7-alpine` container listens on **6379 inside the Docker network** only, under the hostname **`redis`**. Your OS-level Redis can keep using **127.0.0.1:6379** on the host without a port collision.
- The app and worker use **whatever URL you set** in `redis.url` (e.g. `redis://redis:6379/0` for the **Compose** Redis). Data for migrations lives in **that** instance only.

**If you want to use the host’s Redis instead of the Compose `redis` service:**

1. Set **`redis.url`** in your mounted config to a URL the containers can reach, e.g.  
   **`redis://host.docker.internal:6379/0`** (Docker Desktop Mac/Windows) or the **host gateway** IP on Linux (often the bridge gateway, e.g. `172.17.0.1`; on Linux you may add `extra_hosts: host.docker.internal:host-gateway` under `api` and `worker` and use `host.docker.internal` in the URL on recent Docker).
2. Ensure **host Redis** accepts TCP from Docker (e.g. `bind 0.0.0.0` or the bridge IP, **not** only `127.0.0.1`, unless you use `network_mode: host` for the app — that has other implications). Prefer **firewall** rules so only the Docker bridge can reach that port.
3. Remove or disable the **`redis`** service and **`depends_on: redis`** in `docker-compose.yml` so you do not run a second unused Redis (optional but cleaner).

**Security:** if you expose host Redis beyond localhost, use **`requirepass`** / ACLs and a **non-default DB index** (e.g. `/14`) dedicated to this app so it cannot flush keys used by other apps on the same server.

## Move to an air-gapped host

On a machine **with** Docker and internet:

```bash
docker compose build
docker pull redis:7-alpine
docker save migration-engine:latest redis:7-alpine -o migration-engine-bundle.tar
```

Copy **`migration-engine-bundle.tar`**, this **`docker-compose.yml`**, and optionally **`config.docker.yaml`** to the offline host, then:

```bash
docker load -i migration-engine-bundle.tar
docker compose up -d
```

(If you use a bind-mounted `config.docker.yaml`, place it next to `docker-compose.yml` before `up`.)

## Environment

- **`MIGRATION_ENGINE_CONFIG`**: path to YAML inside the container (default `/app/config.docker.yaml`). Set in compose for both `api` and `worker`.
- **`MIGRATION_ENGINE_GIT_REVISION`** (optional): short commit or build id; exposed in **`GET /version`** and the in-app Help panel for “which build is running?”.
- **Optional root-only args helper credentials** under `proxmox.args_helper_*`: used only by the worker when writing `qemu args=` (required on PVE because only root can set `args`). This advanced `args=` path is mainly for VBS-focused CPU tuning and should be validated by an admin for your hardware before use.

## Vulnerability scans (Trivy, Grype, Docker Scout, etc.)

**You rarely “fix” everything to zero**; you reduce risk and accept or triage the rest.

1. **Re-pull the base image and rebuild**  
   `docker pull python:3.12-slim-bookworm` then `docker compose build --no-cache`. Many CVEs are in the distro layer; Debian publishes fixes on rolling tags.

2. **OS patches in the Dockerfile**  
   The image runs `apt-get upgrade` during build so Bookworm security updates are applied at build time. Rebuild on a schedule (e.g. weekly) so new fixes are picked up.

3. **Python dependencies**  
   CVEs in PyPI wheels: upgrade pins in `requirements.txt` (or generate a lockfile with `pip-compile` / Poetry) and rebuild. Some findings are dev-only or false positives for how the library is used.

4. **Redis image**  
   `redis:7-alpine` should also be refreshed: `docker pull redis:7-alpine` and re-save your offline bundle. Optionally pin `redis:7-alpine` to an image digest after a good scan for reproducibility.

5. **Shrink what you ship**  
   Fewer packages means fewer CVEs. This image avoids extra tools (healthcheck uses Python’s `urllib`, not `curl`).

6. **Triage**  
   Use the scanner’s **severity**, **fixed version**, and **reachability** (is the vulnerable code path used?). Ignore or suppress only with a documented reason (e.g. kernel not applicable in container).

## Notes

- **Guest prep requirement (important):** This automation assumes Windows VMs were prepped on the source side (VirtIO/phantom-device approach) so boot works directly on Proxmox without a SATA -> SCSI swap loop, and that VMware Tools were removed pre-migration. Team reference: [croit guide](https://www.croit.io/blog/migrate-windows-vms-to-proxmox-ve).
- **Proxmox / VMware** still need reachability from the **worker** container to those networks (Docker bridge to LAN, macvlan/ipvlan, or host network if you change compose). **WinRM/Horizon hooks are optional placeholders** in this release; only require WinRM reachability if you explicitly enable guest scripts. This bundle does not embed Proxmox.
- **`config.yaml`** in the repo is **not** copied into the image (`.dockerignore`) to avoid leaking real credentials; the image ships **`config.docker.example.yaml`** as `/app/config.docker.yaml`.
- **Product / operator UX ideas** (job views, polling, bulk actions, versioning, etc.): see [`PRODUCT.md`](PRODUCT.md).
- **Import parallelism tuning** (bucket model, worker scaling, rollout guidance): see [`docs/IMPORT_PARALLELISM.md`](docs/IMPORT_PARALLELISM.md).
- **Offline deployment failures runbook** (ARM/AMD64 mismatch, compose v1 `ContainerConfig`, stale networks, healthcheck behavior): see [`docs/OFFLINE_DOCKER_TROUBLESHOOTING.md`](docs/OFFLINE_DOCKER_TROUBLESHOOTING.md).
