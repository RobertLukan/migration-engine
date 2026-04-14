# nginx reverse proxy (TLS in front of the orchestrator)

Run the migration UI/API **behind nginx** when you already terminate HTTPS on nginx and want a single public hostname (or internal hostname with TLS).

The FastAPI app listens on **port 8000** by default (`uvicorn` / Docker `compose` publish). nginx should **proxy HTTP** to that upstream; the browser talks **HTTPS** to nginx only.

**Unauthenticated paths** (safe to pass through the same `location /`): **`GET /health`**, **`GET /version`**, **`GET /`** (dashboard HTML). Everything under **`/jobs`**, **`/api/`**, etc. expects the session cookie after login.

---

## What nginx must send

These headers are standard practice so the app (and any future logic) can know the original host and scheme:

| Header | Purpose |
|--------|--------|
| `Host` | Original host header from the client (often set automatically by `proxy_set_header Host $host;`). |
| `X-Real-IP` | Client address (single IP). |
| `X-Forwarded-For` | Client chain (append with `$proxy_add_x_forwarded_for`). |
| `X-Forwarded-Proto` | `https` or `http` as seen by the client — useful if you later enable secure cookies or URL generation based on scheme. |

The dashboard uses **cookie sessions** (`SessionMiddleware`, `SameSite=Lax`). It does **not** currently read `X-Forwarded-Proto` to flip `Secure` on cookies; serving the site over **https://** to users is still correct and the session cookie works for normal use behind TLS at nginx.

---

## Upstream: app on the same machine (Docker publishes 8000 on localhost)

If Docker Compose maps the API container like `127.0.0.1:8000:8000`, nginx on the host can use:

```nginx
# In http { } block (or separate file included from nginx.conf)
upstream migration_engine_api {
    # Keep traffic on loopback so the app is not exposed on all interfaces
    server 127.0.0.1:8000;
    keepalive 16;
}
```

If the app runs **without** Docker on the same host (e.g. `uvicorn main:app --host 127.0.0.1 --port 8000`), the upstream is the same idea: `server 127.0.0.1:8000;`.

---

## Example `server { }` — HTTPS on nginx, proxy to the app

Replace certificate paths, `server_name`, and upstream name to match your site.

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;

    server_name migration.example.com;

    # Your existing TLS material (Let's Encrypt, internal PKI, etc.)
    ssl_certificate     /etc/ssl/certs/migration.example.com.fullchain.pem;
    ssl_certificate_key /etc/ssl/private/migration.example.com.key;

    # Optional: increase if you POST large JSON bodies (credentials saves, bulk enqueue)
    client_max_body_size 4m;

    location / {
        proxy_pass http://migration_engine_api;

        # HTTP/1.1 + Connection for keepalive to upstream
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Optional: long-running requests (rare for this UI); raise if you see 504s
        # proxy_read_timeout 300s;
        # proxy_send_timeout 300s;
    }

    # Optional: allow an external monitor to hit health without hitting the whole stack
    # location = /health {
    #     proxy_pass http://migration_engine_api/health;
    #     proxy_http_version 1.1;
    #     proxy_set_header Host $host;
    # }
}
```

**HTTP → HTTPS redirect** (on a separate `server` listening on 80) is usually already in your nginx layout; add a `return 301 https://$host$request_uri;` server if needed.

---

## Subpath vs root

The app is mounted at **`/`** (dashboard at `/`, API under `/jobs`, `/api/…`, `/health`). Putting it under a **subpath** (e.g. `https://example.com/migration/`) would require **URL prefix / root_path** support in the app and adjusted static links; that is **not** configured out of the box. Prefer:

- a **dedicated hostname** (e.g. `migration.internal.example.com`), or  
- a **separate server block** with its own `server_name` pointing at this `location /`.

---

## Docker and nginx on different hosts

Point `upstream` at the machine IP and published port (firewall permitting), e.g. `server 10.0.0.50:8000;`. Prefer **private network** + TLS between tiers if policy requires it (then you might use `proxy_pass https://...` with your own certs to the backend).

---

## See also

- **[DOCKER.md](DOCKER.md)** — compose stack, ports, config path.  
- **`SessionMiddleware`** in `main.py` — `https_only`, `same_site`, `max_age` if you later tighten cookies for HTTPS-only deployments.
