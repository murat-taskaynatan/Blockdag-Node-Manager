#!/usr/bin/env bash
set -euo pipefail

# tune_nvme_node.sh
# Safely tune a bare-metal NVMe host for fsync-heavy workloads (e.g., blockchain nodes).
#
# What it does (by default, safe changes only):
# - Sets CPU to performance governor
# - Sets NVMe default power state latency target to 0 (reduces APST latency)
# - Sets NVMe I/O scheduler to 'none' (fallback: 'mq-deadline')
# - Remounts the node data filesystem with sensible options
#
# Optional (risk: power-loss data loss if no PLP):
# - Enable NVMe Volatile Write Cache (VWC) with --enable-vwc yes
#
# Usage examples:
#   sudo scripts/tune_nvme_node.sh --mountpoint /mnt/node
#   sudo scripts/tune_nvme_node.sh --mountpoint /mnt/node --enable-vwc yes
#   sudo scripts/tune_nvme_node.sh --mountpoint /mnt/node --device /dev/nvme0n1
#
# Notes:
# - Requires root. Does not persist across reboot; see docs/nvme_tuning.md for persistence.

MOUNTPOINT=""
DEVICE=""
ENABLE_VWC="no"
DO_CPU="yes"
DO_NVME_LATENCY="yes"
DO_SCHEDULER="yes"
DO_REMOUNT="yes"
REVERT="no"

error() { echo "[ERROR] $*" >&2; }
info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }

need_root() {
  if [[ $(id -u) -ne 0 ]]; then
    error "Run as root (sudo)."; exit 1
  fi
}

usage() {
  cat <<EOF
Usage: sudo $0 --mountpoint <dir> [--device <dev>] [--enable-vwc yes|no] [--cpu yes|no] [--nvme-latency yes|no] [--scheduler yes|no] [--remount yes|no] [--revert yes|no]

Options:
  --mountpoint DIR     Filesystem mountpoint holding node data (required)
  --device DEV         Block device for scheduler/trace (auto-detected from mountpoint)
  --enable-vwc yes|no  Enable NVMe volatile write cache (default: no)
  --cpu yes|no         Set CPU governor performance (default: yes)
  --nvme-latency yes|no  Set nvme_core.default_ps_max_latency_us=0 (default: yes)
  --scheduler yes|no   Set IO scheduler to none/mq-deadline (default: yes)
  --remount yes|no     Remount FS with tuned options (default: yes)
  --revert yes|no      Revert tweaks to defaults (best-effort) (default: no)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mountpoint)
      MOUNTPOINT=${2:-}; shift 2;;
    --device)
      DEVICE=${2:-}; shift 2;;
    --enable-vwc)
      ENABLE_VWC=${2:-no}; shift 2;;
    --cpu)
      DO_CPU=${2:-yes}; shift 2;;
    --nvme-latency)
      DO_NVME_LATENCY=${2:-yes}; shift 2;;
    --scheduler)
      DO_SCHEDULER=${2:-yes}; shift 2;;
    --remount)
      DO_REMOUNT=${2:-yes}; shift 2;;
    --revert)
      REVERT=${2:-no}; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      error "Unknown arg: $1"; usage; exit 1;;
  esac
done

need_root

if [[ -z "$MOUNTPOINT" ]]; then
  error "--mountpoint is required"; usage; exit 1
fi

if [[ ! -d "$MOUNTPOINT" ]]; then
  error "Mountpoint '$MOUNTPOINT' not found"; exit 1
fi

# Resolve device from mountpoint if not provided
if [[ -z "$DEVICE" ]]; then
  SRC=$(findmnt -no SOURCE --target "$MOUNTPOINT" || true)
  if [[ -z "$SRC" ]]; then
    error "Cannot resolve device for mountpoint $MOUNTPOINT"; exit 1
  fi
  # Handle LVM/partitions: readlink to real device path
  REAL=$(readlink -f "$SRC")
  DEVICE="$REAL"
fi

if [[ ! -b "$DEVICE" ]]; then
  error "Device '$DEVICE' is not a block device"; exit 1
fi

# Derive base block device (e.g., nvme0n1 from nvme0n1p2)
DEV_BASENAME=$(basename "$(readlink -f "$DEVICE")")
SYS_BLOCK="/sys/block"
if [[ -d "$SYS_BLOCK/$DEV_BASENAME" ]]; then
  BASE="$DEV_BASENAME"
else
  # Try to strip partition suffixes
  if [[ "$DEV_BASENAME" =~ ^(nvme[0-9]+n[0-9]+)p[0-9]+$ ]]; then
    BASE="${BASH_REMATCH[1]}"
  else
    # generic: strip trailing digits
    BASE="${DEV_BASENAME%%[0-9]*}"
  fi
  if [[ ! -d "$SYS_BLOCK/$BASE" ]]; then
    error "Cannot locate sysfs for base device of '$DEVICE'"; exit 1
  fi
fi

BASE_DEV_PATH="$SYS_BLOCK/$BASE"

FSTYPE=$(findmnt -no FSTYPE --target "$MOUNTPOINT" || true)
if [[ -z "$FSTYPE" ]]; then
  error "Cannot determine filesystem type for $MOUNTPOINT"; exit 1
fi

info "Mountpoint: $MOUNTPOINT (fstype=$FSTYPE)"
info "Device: $DEVICE (base=$BASE)"

set_cpu_governor() {
  local governor="performance"
  if [[ "$REVERT" == "yes" ]]; then
    # Prefer schedutil if available, else ondemand
    governor="schedutil"
    local avail=""
    if [[ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors ]]; then
      avail=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null || true)
      if [[ "$avail" != *schedutil* ]]; then governor="ondemand"; fi
    fi
  fi
  if command -v cpupower >/dev/null 2>&1; then
    info "Setting CPU governor via cpupower -> $governor"
    cpupower frequency-set -g "$governor" || warn "cpupower failed; falling back to sysfs"
  fi
  for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [[ -f "$gov" ]] || continue
    echo "$governor" > "$gov" 2>/dev/null || true
  done
}

set_nvme_latency_mode() {
  local p="/sys/module/nvme_core/parameters/default_ps_max_latency_us"
  if [[ -w "$p" ]]; then
    if [[ "$REVERT" == "yes" ]]; then
      info "Reverting NVMe default_ps_max_latency_us to 200000"
      echo 200000 > "$p" || warn "Failed to write $p"
    else
      info "Setting NVMe default_ps_max_latency_us=0 (low latency)"
      echo 0 > "$p" || warn "Failed to write $p"
    fi
  else
    warn "Cannot set $p (not writable) — make persistent via kernel param. See docs/nvme_tuning.md"
  fi
}

set_scheduler() {
  local sched_file="$BASE_DEV_PATH/queue/scheduler"
  if [[ -f "$sched_file" ]]; then
    local available=$(cat "$sched_file")
    local target
    if [[ "$REVERT" == "yes" ]]; then
      # Revert to mq-deadline where possible
      target="mq-deadline"
      if [[ "$available" != *mq-deadline* ]]; then target="none"; fi
    else
      # Choose 'none' if available, else mq-deadline
      target="none"
      if [[ "$available" != *none* ]]; then target="mq-deadline"; fi
    fi
    info "Setting I/O scheduler for $BASE to '$target' (available: $available)"
    echo "$target" > "$sched_file" || warn "Failed to set scheduler"
  else
    warn "Scheduler file not found for $BASE"
  fi
}

remount_fs() {
  case "$FSTYPE" in
    ext4)
      if [[ "$REVERT" == "yes" ]]; then
        info "Remounting $MOUNTPOINT with ext4 defaults: relatime,commit=5,data=ordered"
        mount -o remount,relatime,commit=5,data=ordered "$MOUNTPOINT" || warn "Remount failed"
      else
        info "Remounting $MOUNTPOINT with ext4 opts: noatime,commit=30,data=ordered"
        mount -o remount,noatime,commit=30,data=ordered "$MOUNTPOINT" || warn "Remount failed"
      fi
      ;;
    xfs)
      if [[ "$REVERT" == "yes" ]]; then
        info "Remounting $MOUNTPOINT with xfs defaults: relatime"
        mount -o remount,relatime "$MOUNTPOINT" || warn "Remount failed"
      else
        info "Remounting $MOUNTPOINT with xfs opts: noatime,logbufs=8,logbsize=262144"
        mount -o remount,noatime,logbufs=8,logbsize=262144 "$MOUNTPOINT" || warn "Remount failed"
      fi
      ;;
    *)
      warn "Filesystem $FSTYPE not explicitly tuned by script; skipping remount"
      ;;
  esac
}

enable_vwc_if_requested() {
  if ! command -v nvme >/dev/null 2>&1; then
    warn "nvme-cli not installed; cannot manage VWC"
    return 0
  fi
  local ctrl="/dev/${BASE%%p*}"
  [[ -b "$ctrl" ]] || ctrl="$DEVICE"
  if [[ "$ENABLE_VWC" == "yes" ]]; then
    info "Checking NVMe VWC support on $ctrl"
    if nvme get-feature -f 0x06 -H "$ctrl" >/tmp/nvme_vwc.$$ 2>/dev/null; then
      if grep -qi "Volatile Write Cache: Present" /tmp/nvme_vwc.$$ || grep -qi "(ENABLED)" /tmp/nvme_vwc.$$; then
        info "Enabling NVMe VWC on $ctrl (risk: data loss on power failure without PLP)"
        if nvme set-feature -f 0x06 -v=1 "$ctrl" >/dev/null 2>&1; then
          info "VWC enabled"
        else
          warn "Failed to enable VWC via nvme set-feature"
        fi
      else
        warn "Controller does not report VWC support; skipping"
      fi
      rm -f /tmp/nvme_vwc.$$ || true
    else
      warn "nvme get-feature failed on $ctrl"
    fi
    return 0
  fi
  info "Ensuring NVMe VWC disabled on $ctrl"
  if nvme set-feature -f 0x06 -v=0 "$ctrl" >/dev/null 2>&1; then
    info "VWC disabled"
  else
    warn "Failed to disable VWC via nvme set-feature"
  fi
}

disable_vwc_if_revert() {
  if [[ "$REVERT" != "yes" ]]; then return 0; fi
  if ! command -v nvme >/dev/null 2>&1; then
    warn "nvme-cli not installed; cannot disable VWC"
    return 0
  fi
  local ctrl="/dev/${BASE%%p*}"
  [[ -b "$ctrl" ]] || ctrl="$DEVICE"
  info "Disabling NVMe VWC on $ctrl (if supported)"
  nvme set-feature -f 0x06 -v=0 "$ctrl" >/dev/null 2>&1 || true
}

main() {
  if [[ "$DO_CPU" == "yes" ]]; then set_cpu_governor; else info "Skipping CPU governor"; fi
  if [[ "$DO_NVME_LATENCY" == "yes" ]]; then set_nvme_latency_mode; else info "Skipping NVMe latency mode"; fi
  if [[ "$DO_SCHEDULER" == "yes" ]]; then set_scheduler; else info "Skipping IO scheduler"; fi
  if [[ "$DO_REMOUNT" == "yes" ]]; then remount_fs; else info "Skipping FS remount"; fi
  if [[ "$REVERT" == "yes" ]]; then disable_vwc_if_revert; else enable_vwc_if_requested; fi
  if [[ "$REVERT" == "yes" ]]; then info "Revert complete."; else info "Done. Re-run your fsync benchmark or node and observe latency improvements."; fi
}

main "$@"
