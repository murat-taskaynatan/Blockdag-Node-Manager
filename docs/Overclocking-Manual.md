# Overclocking Manual (NVMe/CPU/FS Tuning)

This manual explains how to use the Node Manager “Overclock” tab to tune a bare‑metal Ubuntu host for fsync‑heavy workloads like BlockDAG node syncing. It also covers safety checks, verification, revert, and persistence.

## Overview

Overclock applies a set of host‑level optimizations that typically reduce fsync latency and improve sync speed:
- CPU governor → performance (reduces latency/power‑state overhead)
- NVMe low‑latency mode (`nvme_core.default_ps_max_latency_us=0`)
- I/O scheduler → `none` (fallback `mq-deadline`) for NVMe
- Filesystem remount with tuned options
  - ext4: `noatime,commit=30,data=ordered`
  - xfs: `noatime,logbufs=8,logbsize=256k`

## Prerequisites

- Ubuntu or similar Linux host with sudo
- Node Manager service running on the same host
- `fio` for the Verify benchmark
  - The installer attempts to install `fio` (apt/dnf). The app will also try to install it on demand when you click Verify.

## Using the Overclock Tab

1) Open Node Manager UI → Overclock tab.
2) Data directory
   - Auto‑filled from discovered containers that bind host data to `/bdag/data`.
   - You can enter any path under your node’s data filesystem; we’ll detect the mountpoint automatically.
3) Preflight safety check (runs automatically)
   - Summarizes detected device, mountpoint, and PLP (power loss protection) info when available.
   - If PLP shows “unknown”, you may need root for `smartctl` or your device may not expose it.
4) Select Tweaks
   - CPU governor: performance
   - NVMe low‑latency mode
   - I/O scheduler: none (mq‑deadline fallback)
   - Filesystem remount (ext4/xfs tuned)
5) Tweaks apply automatically as soon as you toggle them.
   - The status line (left of the buttons) confirms success or reports if root access is required.
6) Verify
   - Click “Test” to run a 10‑second fsync micro‑benchmark on the same filesystem.
   - The UI shows IOPS, bandwidth, p50/p99 and plots IOPS/p50 over time.
7) Revert
   - Click “Revert” to best‑effort restore typical defaults (see Revert section).

## What Each Tweak Does

- CPU governor → performance
  - Keeps CPUs at higher clocks; reduces latency from frequency scaling and deep C‑states.

- NVMe low‑latency mode
  - Sets `nvme_core.default_ps_max_latency_us=0`, which discourages aggressive NVMe power states that add wake latency.

- I/O scheduler → none (NVMe)
  - Bypasses legacy scheduling. On very new kernels, `none` or `mq-deadline` are recommended for NVMe.

- Filesystem remount
  - ext4: `noatime,commit=30,data=ordered` reduces metadata writes from access time updates and batches journal commits.
  - xfs: `noatime,logbufs=8,logbsize=256k` increases log buffers and reduces metadata write overhead.

## Verify Benchmark (fio)

The Verify button runs a short benchmark using `fio` with parameters similar to:

```
fio --name=fsync --directory=<mount> --rw=write --bs=4k \
    --ioengine=psync --numjobs=1 --size=64m --fsync=1 \
    --time_based=1 --runtime=10 --group_reporting=1
```

Interpreting results:
- IOPS: Higher is better (more 4k sync writes per second)
- BW: Aggregate bandwidth
- p50/p99: Lower is better (microseconds). p99 is “tail” latency; large drops indicate better interactive performance.

If `fio` is missing, the app tries to install it automatically via apt/dnf. If it can’t (needs root), the UI shows a copyable command.

## Revert (Best‑Effort)

Revert restores typical defaults:
- CPU governor → `schedutil` if available (else `ondemand`)
- NVMe latency → `default_ps_max_latency_us=200000`
- I/O scheduler → `mq-deadline` (fallback `none` if not available)
- Filesystem remount → ext4 `relatime,commit=5,data=ordered`; xfs `relatime`
- Ensure NVMe VWC remains disabled (when the controller exposes the feature)

Note: Device/vendor defaults can vary. Revert aims for sensible, generally safe defaults rather than exact pre‑tuning state.

## Persistence Across Reboots

For permanent settings, configure the system as follows:

- NVMe latency (kernel cmdline):
  - Add `nvme_core.default_ps_max_latency_us=0` to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`
  - `sudo update-grub && sudo reboot`

- I/O scheduler via udev:
  - `/etc/udev/rules.d/60-nvme-io-scheduler.rules`
  - `ACTION=="add|change", KERNEL=="nvme*n*", ATTR{queue/scheduler}="none"`
  - `sudo udevadm control --reload && sudo udevadm trigger`

- CPU governor via systemd (cpupower):
  - Install: `sudo apt-get install -y linux-tools-common linux-tools-$(uname -r)`
  - Create `/etc/systemd/system/cpupower-performance.service` to set `performance` at boot.

- Filesystem mount options:
  - Edit `/etc/fstab` for the node’s data volume.
  - ext4: `noatime,commit=30,data=ordered`
  - xfs: `noatime,logbufs=8,logbsize=262144`

See also: `docs/nvme_tuning.md` for detailed persistence steps.

## Troubleshooting

- Verify shows very low latency but node still slow
  - Check node logs for compaction/backpressure (RocksDB/LevelDB). Increase DB cache, tune `bytes_per_sync` and WAL settings if supported.
- Verify fails: `fio not installed`
  - Use the command shown in UI (apt/dnf). Ensure the host has network and sudo.
- Apply shows “Needs root”
  - Copy the hinted `sudo scripts/tune_nvme_node.sh ...` command into a root shell and re‑try.
- Preflight says “unknown”
  - Some drives do not expose PLP. This does not necessarily mean unsafe; treat the preflight summary as informational.

## CLI Script (Advanced)

All tweaks are implemented by `scripts/tune_nvme_node.sh` and can be run manually as root:

```
sudo scripts/tune_nvme_node.sh --mountpoint /mnt/node \
  --cpu yes --nvme-latency yes --scheduler yes --remount yes

# Revert example
sudo scripts/tune_nvme_node.sh --mountpoint /mnt/node --revert yes \
  --cpu yes --nvme-latency yes --scheduler yes --remount yes
```

## Safety Notes

- Prefer an enterprise NVMe with PLP for production nodes; if not available, use a UPS and regular snapshots.
- Avoid using Overclock on ZFS/Btrfs for the DB volume unless you are familiar with the trade‑offs (fsync often slower without a proper SLOG).

## FAQ

- Why is my VM faster than bare metal?
  - The hypervisor and guest page cache often turn fsyncs into fast RAM writes; bare metal pays real device flush latency. Overclock aligns bare‑metal behavior closer to that cached path (safely where possible).

- Do I need to reboot after applying tweaks?
  - No. Most tweaks apply immediately. Persistence steps (GRUB/udev/systemd) require reboot to take effect on next boot.

- Can I apply only some tweaks?
  - Yes. Toggle each item on/off in the Overclock tab before applying.
