# Import Parallelism Guide

This tool is designed to migrate many VMs efficiently by parallelizing work while protecting the source storage path.

## Why parallelism matters

For VMware -> Proxmox moves, the slowest and most resource-heavy phase is usually:

- Proxmox `import-from` (disk pull from ESXi-linked source).

CPU/memory config and post-boot steps are typically much lighter than sustained disk transfer.

## How the tool controls concurrency

The worker uses a per-bucket permit model:

- each import path is mapped to an **import bucket**,
- each bucket has a max parallel limit from config,
- jobs on different buckets can import simultaneously,
- jobs in the same bucket wait for a free permit.

Primary knob:

```yaml
worker:
  max_concurrent_imports_per_esxi_storage: 1
```

## Achieving high throughput

Use all three layers together:

1. **Worker replicas/processes**: increase job execution capacity.
2. **Per-bucket import limit**: raise carefully from `1` to `2` if source can handle it.
3. **Bucket distribution**: enqueue VMs so imports are spread across distinct source buckets.

Example capacity:

- `max_concurrent_imports_per_esxi_storage: 2`
- jobs distributed across buckets `sh12`, `sh13`, `sh14`

Potential concurrent imports = `2 + 2 + 2 = 6` (assuming enough workers and healthy storage path).

## Recommended rollout

1. Baseline with one worker and per-bucket limit `1`.
2. Add worker replicas first.
3. Increase per-bucket limit to `2` only after measuring:
   - ESXi latency
   - datastore throughput
   - Proxmox task queue health
   - migration error rate/timeouts
4. Keep `1` for unstable or shared production datastores.

## Operator checklist

- Confirm linked ESXi storages are visible in Proxmox.
- Ensure migration jobs reference the intended import path style (so bucket mapping is predictable).
- Watch active jobs in the Migrations view and check import wait/run behavior.
- Prefer staggered waves instead of flooding one datastore.

## Notes

- This is controlled parallelism, not unlimited fan-out.
- The safest way to speed up is spreading jobs across buckets and adding worker replicas gradually.
- WinRM/Horizon placeholders do not affect import-throughput tuning.
