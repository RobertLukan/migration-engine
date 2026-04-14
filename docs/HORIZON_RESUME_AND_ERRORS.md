# Horizon step failures, resume, and error handling (design notes)

Notes from a design discussion to revisit **after** you have the Omnissa / Horizon PowerShell script in place. Nothing here is implemented unless the codebase explicitly says so elsewhere.

---

## Current behaviour (baseline)

- Migration jobs live in **Redis** with a **`state`** field (`PENDING`, `IMPORTING`, …, `HORIZON_CONFIG`, `COMPLETED`, `FAILED`, `CANCELLED`).
- On **`FAILED`**, the worker’s main job processor **returns without doing more work** for that job id. The job is not automatically picked up again from the queue unless something **changes state** and **re-enqueues** it.
- Failures in the Horizon / WinRM stage set **`state = FAILED`**, **`error`**, and **`last_failed_stage`** (often tied to `HORIZON_CONFIG`).

So today a **transient** WinRM or MSI failure at the end of the pipeline is treated like a hard stop: the VM may already be fine on Proxmox, but the operator has no first-class **“try the guest script again”** path without manual Redis surgery or a new job.

---

## Why “resume” is a good idea

- **Earlier stages are expensive or irreversible** (import, boot sequences, SCSI move). Failing only on **Horizon agent / guest configuration** is a common real-world case (network blip, MSI exit code, wrong path, GPO delay).
- **Retrying only the last step** avoids repeating Proxmox work and reduces risk compared to re-running the whole pipeline blindly.

---

## Recommended approach: phases

### Phase 1 — Horizon-only resume (highest value, lowest risk)

**Scope:** Job is `FAILED` and failure is attributable to the **Horizon / WinRM** work (e.g. `last_failed_stage` is `HORIZON_CONFIG` or a sub-label you standardise).

**Behaviour (conceptual):**

1. Operator (or API client) triggers **Resume Horizon** (explicit action, not silent auto-retry).
2. Backend validates the job is in a **resumable** failure bucket.
3. Clear or normalise fields as needed (`error`, `cancel_requested`, etc.).
4. Set **`state`** back to **`HORIZON_CONFIG`** (or introduce a dedicated state like `HORIZON_RETRY` if you want metrics).
5. **Push `job_id` onto the Redis queue** again so the worker runs `process_job` once more.

**Why start here:** The worker already refreshes **`guest_ip`** from Redis in that part of the pipeline; the main requirement on your side is that **`horizon.powershell_script` is idempotent** (safe and correct if run twice after a partial success).

### Phase 2 — Broader “resume from stage X”

**Harder:** Stages such as **`IMPORTING`** or **`SCSI_SWAP`** may be **partially** done after a crash. Resuming requires **per-stage idempotency** (e.g. detect disk already imported, SCSI layout already applied) and clear invariants. Treat this as a **separate project** after Phase 1 and after you have operational experience.

### Automatic retries vs operator-controlled resume

- **Automatic** limited retries with backoff can help for **obviously transient** WinRM errors (timeouts, connection reset).
- Anything that **installs software** or **changes Horizon registration** should stay **operator-approved** until scripts are proven **idempotent** and logs are trustworthy.

---

## Product / API sketch (when you implement)

- **UI:** On the job **Details** view, when the job is `FAILED` and the failed stage is Horizon-related, show **“Resume Horizon step”** with a short warning (guest must be reachable; script must tolerate a second run).
- **API:** e.g. `POST /jobs/{job_id}/resume` with body `{ "mode": "horizon" }` so the feature cannot accidentally widen to dangerous stages without code review.
- **Docs:** Document expectations for **`horizon.powershell_script`** (idempotency, logging, exit codes, reboot handling if any).

---

## PowerShell script checklist (for “later you”)

When you write or finalise the Omnissa Horizon script, favour:

1. **Detect before destroy** — e.g. if the correct agent version is already installed and healthy, exit 0 without reinstalling.
2. **Clear logging** — everything important goes to stdout/stderr or Windows Event Log so WinRM captures it.
3. **Deterministic exit codes** — non-zero on real failure; zero when work is already done or successfully completed.
4. **No secrets in logs** — passwords, connection tokens, etc.
5. **Re-run after partial install** — MSI repair or uninstall-then-install paths tested twice on a clone.

---

## Horizon / VDI context (outside the engine)

Resume fixes **one class** of problem (guest-side automation). Mass Horizon migrations still depend on:

- Pool type (dedicated vs floating vs instant clone) and **what “the same desktop”** means after move.
- **Connection Server**, entitlements, DNS, Blast/UAG, and cutover planning.
- **Wave planning**, capacity, and rollback — usually runbooks and CMDB, not only code.

Keep this file as a **design memo**; when you implement resume, add a line here pointing to the PR or commit and update **PRODUCT.md** if you track roadmap there.
