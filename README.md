# Migration Engine

VMware -> Proxmox migration orchestrator (API/UI + worker + Redis queue).

## Intended use

This project was created primarily to assist migration of **Windows 11 VDI** workloads.
It can also be used for migration of recent **Windows Server** guests.

## Prerequisites

This automation assumes Windows guests are prepared before migration:

1. VirtIO/phantom device preparation is already done on the source VM so the guest can boot on Proxmox without the legacy SATA -> SCSI shuffle.
2. VMware Tools are removed before migration (or handled by your existing pre-cutover process).

Reference guide used by the team for this prep workflow:

- [How to Migrate Windows VMs from VMware ESXi to Proxmox VE Without Boot Failures](https://www.croit.io/blog/migrate-windows-vms-to-proxmox-ve)

## Scope notes

- WinRM/Horizon hooks are optional and currently treated as placeholder integration points for future workflows.
- Core migration path focuses on disk import and bootability on Proxmox with the prepared guest.

## Parallel imports (key capability)

One of the strongest features of this tool is controlled **parallel disk import**.

- The expensive step is Proxmox `import-from` (reading VMDK data from ESXi-linked storage).
- The worker can run multiple jobs in parallel, but import pressure is controlled **per source bucket** to avoid overloading one ESXi datastore/path.
- Jobs on different buckets can import at the same time; jobs on the same bucket are capped.

### How to enable and tune

1. Run multiple worker processes/containers (for dequeue and pipeline concurrency).
2. Set `worker.max_concurrent_imports_per_esxi_storage` in `config.yaml`.
3. Ensure import paths map cleanly to distinct buckets (for example separate PVE ESXi storage IDs).

Example:

```yaml
worker:
  max_concurrent_imports_per_esxi_storage: 2
  job_thread_pool_size: 24
```

With the example above:

- bucket `sh12` can run 2 concurrent imports,
- bucket `sh13` can run 2 concurrent imports,
- so total concurrent imports can be 4 if jobs are split across both buckets.

### Practical scaling guidance

- Start with `max_concurrent_imports_per_esxi_storage: 1` (safe baseline).
- Increase to `2` only after confirming ESXi datastore/network stability.
- Scale workers only when Redis, Proxmox API, and storage IO can sustain it.
- Keep VM name uniqueness and VMID safety checks enabled (already handled by the API).

For deeper details and bucket strategy, see [`docs/IMPORT_PARALLELISM.md`](docs/IMPORT_PARALLELISM.md).
