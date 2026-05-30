# Horizon / Omnissa Connection Server API (pool moves, re-registration)

Design note: using **Connection Server REST** from this app (e.g. move or re-register a desktop from an **automatic** vSphere-backed pool toward a **manual** pool after migration to Proxmox).

For the current stable release, Horizon/WinRM integration is treated as an **optional placeholder path** rather than a required workflow stage.

---

## Is it a good idea?

**Often yes, as an optional integration** — if your operational model is “migrated VM must show up in Horizon under a **manual** (externally provisioned) pool,” doing that from the same orchestrator can reduce **manual admin** steps and mistakes.

It is **not** required for the core “disk + guest + WinRM script” pipeline; it is a **second axis**: **Control plane (Horizon)** vs **Data plane (Proxmox + guest)**.

---

## Conceptual reality (automatic vs manual)

- **Automatic / full-clone / instant-clone pools** are built around **Horizon + vSphere managing VM lifecycle**. After you migrate the workload to **Proxmox**, that VM is **no longer** a normal participant in the same **vCenter-provisioned** automatic pool model.
- **Manual pools** (and related “externally provisioned” / **UNMANAGED** style concepts in the APIs) exist precisely for machines **provisioned outside** Horizon’s automated cloning path — which matches **“we built the VM elsewhere (here: Proxmox)”**.

So the goal is usually **not** a literal “flip this one object from automatic to manual in place,” but a **controlled transition**:

1. **Remove or entitle away** the old desktop identity from the automated pool (if it still exists in Horizon/vCenter inventory), and  
2. **Register** the running machine (same SID/guest or new, depending on your migration design) as a **manual** desktop in a target pool,

plus **agent pairing / Blast / certificates** as required by your Horizon version.

Exact steps and API calls depend on **Horizon/Omnissa version**, **pool type**, and whether the VM **still exists in vCenter** during a transition window.

---

## What the app would need (architecture)

1. **Configuration** — Connection Server base URL(s), credentials or certificate-based auth, trust settings, optional pod / Cloud Pod context.
2. **A small Horizon API client** — wrap REST calls, parse errors, respect rate limits, structured logging (no secrets in logs).
3. **A clear trigger** — e.g. after **WinRM Horizon script succeeds**, or as a **separate operator-driven step** / API until behaviour is proven.
4. **Idempotency** — “already in manual pool X” should be a **no-op success**, not a duplicate error.
5. **Failure domain** — Horizon API failure should **not** silently imply the Proxmox VM is wrong; surface **which subsystem** failed (PVE vs guest vs Horizon).

Official API surface moves with vendor branding; treat **version-pinned docs** (Omnissa / Broadcom “Horizon Server API”) as the source of truth when you implement.

---

## Trade-offs

| Pros | Cons |
|------|------|
| Fewer manual Horizon admin steps; repeatable at mass scale | **High-privilege** credentials on the migration app host; strong audit requirements |
| Can align job state in Redis with Horizon inventory actions | **API and behaviour differ** by Horizon version and topology (CPA, pods, etc.) |
| Fits “mass VDI migration” story end-to-end | More **support surface** (certificates, load balancers, AD DNS vs Horizon naming) |

---

## Relation to the PowerShell / WinRM stage

You can **combine** approaches:

- **Guest script** — install/repair **Omnissa Horizon Agent**, local firewall, DNS registration, sysprep-related rules you already need.
- **Connection Server REST** — **pool membership**, entitlements, machine object create/remove, health checks.

Splitting responsibilities avoids putting **only** Connection Server credentials on the guest while still letting the guest do what only the guest can do.

---

## Suggested sequencing

1. Stabilise **guest-side** Omnissa install + **manual** pool registration runbook (even if manual in UI).  
2. Automate the **smallest REST slice** (e.g. “add machine to manual pool by name / AD SID / agent UUID” — whatever your version supports).  
3. Optionally add **“remove from automatic pool”** only if you still have vSphere inventory and a safe ordering with your cutover plan.

---

## See also

- **[HORIZON_RESUME_AND_ERRORS.md](./HORIZON_RESUME_AND_ERRORS.md)** — resuming after Horizon-stage failures; idempotent scripts.  
- **`horizon.powershell_script`** in `config.yaml` — current guest-side hook implemented by the worker.

When you pick a Horizon **version** and **pool types**, add a short appendix here with the exact REST resources you intend to call and the **service account** role list (principle of least privilege).
