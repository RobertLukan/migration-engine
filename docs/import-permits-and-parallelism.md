# Import permits and parallelism

## Why this exists

The heaviest step in a VMware → Proxmox move is usually **Proxmox `import-from`**: the hypervisor reads the source VMDK from ESXi-linked storage while creating the new VM disk. That phase stresses **ESXi and the data path** far more than later stages (boot prep, guest-agent checks).

The worker therefore uses a **Redis-backed import permit** so operators can cap **concurrent import-from operations per bucket**, instead of accidentally running many disk pulls at once.

**Important:** The permit covers the **import-from window** (including the worker’s wait until Proxmox finishes the async import), not the entire job. Other pipeline stages can overlap between jobs.

## What a “bucket” is

A bucket is the **string key** for one permit pool in Redis. Jobs that share the same bucket compete for the same token list.

### PVE volid form (typical from ESXi storage listings)

Example:

`sh12:ha-datacenter/DS1/VM1/vm1.vmdk`

- **Bucket** = the Proxmox **linked ESXi storage id** — the substring **before the first `:`** → here **`sh12`**.
- Two jobs using **`sh12:…`** share one pool → **serialized** import-from when `max_concurrent_imports_per_esxi_storage` is `1`.
- A job using **`sh13:…`** uses a **different** pool → it can import **in parallel** with a `sh12` job (one permit per bucket).

### `esxi:…` URI form (path-style import reference)

Example:

`esxi:ha-datacenter/test2/clone1/clone1.vmdk`

There is **no** `storage-id:` prefix. The worker buckets on **`esxi:<first>/<second>`** path segments (lowercased), e.g. **`esxi:ha-datacenter/test2`**, so imports under the same DC/datastore prefix share a pool.

Very short `esxi:` paths (not enough segments) fall back to a single global pool key **`esxi_legacy_uri`**.

## Default behaviour (no extra config)

With defaults:

- **`sh12` vs `sh13`** → **different buckets** → **parallel** import-from (each bucket allows up to `worker.max_concurrent_imports_per_esxi_storage` concurrent imports, usually **1**).
- **Two jobs, both `sh12:…`** → **same bucket** → **one import-from at a time**; the other waits on the permit.
- **Same VM name, two jobs** → **blocked at enqueue** (HTTP **409**): only one non-terminal migration per guest name is allowed, regardless of bucket.

## Optional: merge buckets (off by default)

If several Proxmox storage ids should **share one permit** (for example they funnel through the same ESXi host and you want a host-level cap), set:

```yaml
worker:
  esxi_import_permit_bucket_override:
    sh12: esx-host-dc1
    sh13: esx-host-dc1
```

If **`esxi_import_permit_bucket_override` is omitted**, no merging happens — **`sh12` and `sh13` stay independent** for parallelism.

## Related settings

| Setting | Role |
|--------|------|
| `worker.max_concurrent_imports_per_esxi_storage` | Max concurrent **import-from** operations **per bucket** (shared across all worker processes via Redis). |
| `worker.esxi_import_permit_bucket_override` | Optional map from computed bucket → canonical permit key (merge pools). **Default: unset.** |
| `worker.import_permit_prefix` | Optional Redis key prefix for permit lists (advanced). |

## UI

On **Guests**, the **PVE ESXi storages** column lists the Proxmox storage ids where each guest appears (from ESXi-linked storage scans). For typical `volid` paths, each id matches the **import permit bucket** prefix — choosing guests on **different** ids allows **parallel** import-from (subject to `max_concurrent_imports_per_esxi_storage` per bucket). **Size** shows the `size` field from the same listing for the chosen disk candidate (API metadata only).

On **Migrations**, the **VMware / import queue** column and job detail text describe bucket, permit state (**WAITING** / **ACQUIRED** / …), and vCenter API host where applicable. The list status line summarizes **IMPORTING** jobs by waiting vs running per bucket.
