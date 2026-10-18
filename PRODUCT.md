# Product & operator experience (ideas backlog)

Practical improvements that help **people running migrations** and **teams maintaining the tool**. Not a commitment order; pick what matches your scale.

---

## Jobs & visibility

- **Job focus view** *(dashboard: “Details” on Migrations)* — Panel with **state timeline**, **last error**, **log tail** auto-refresh (~4s) while open, and **Copy diagnostics** (job id, VM name, node, VMID, state, failed stage, error, etc.).
- **State as a timeline** — Same panel: ordered pipeline with done / current / error styling (and **CANCELLED** when applicable).
- **Clearer failure context** — When a stage fails, show **which subsystem** (Proxmox API, guest agent, queue) and **suggested next check** where you already know common causes.

## Performance & scale

- **Adaptive polling** *(dashboard)* — Jobs list uses **~5s** when the tab is visible and **~20s** when hidden; refreshes once when the tab becomes visible again.
- **Live updates (later)** — If log volume grows, **SSE or WebSocket** for job logs avoids hammering `GET …/log`.

## Operator efficiency

- **Bulk actions** *(dashboard)* — Checkbox column + **Cancel selected (PENDING)** for queued jobs (same stop API as row **Stop**).
- **Worker / build identity** — Log **hostname**, **worker instance id**, and **config path** at worker startup so multi-worker or multi-environment logs are easy to grep.
- **Version in the UI** — Footer or small “About”: **app version** and optionally **git commit** so “which build ran this migration?” is never ambiguous.

## Deployments & reliability

- **Graceful worker shutdown** — On **SIGTERM**, stop dequeuing new work and optionally wait for the current stage to finish (with a cap) so deploys do not multiply partial failures.
- **Liveness vs readiness (optional)** — A **cheap** `/health/live` (process up) vs deeper checks used less often can keep load balancers from hitting Proxmox on every probe. Today’s `/health` is deeper by design; split only if probes become noisy.

## Maintainability (helps operators indirectly)

- **Stricter config at startup** — Validate `config.yaml` with a schema (e.g. Pydantic) so typos and missing keys fail **before** the first migration dies halfway.
- **Split the dashboard script** — As `static/index.html` grows, moving JS into modules (even without a full SPA build) reduces merge pain and regression risk.

## Operator checks

- **Stage timeouts** — Surface **how long** a step waited and **what timed out** in the UI.
- **Dry-run / validate** — A path that checks **reachability and prerequisites** without starting destructive work reduces trial-and-error on production VMs.

---

When you implement items from this list, consider adding or extending **pytest** coverage for the same behaviour so refactors do not quietly remove operator-facing value.
