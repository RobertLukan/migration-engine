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
