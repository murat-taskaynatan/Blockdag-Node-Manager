# Changelog

## v1.4.7 - 2025-11-07

### Enhancements
- Summary counters now distinguish healthy (running) nodes from forced/stalled ones so the “Offline” tally only includes truly offline containers.
- Liveness auto-recovery now also reacts to “Illegal withdrawal at block:difflayer…” messages so the health guard triggers on this corruption warning.
- Version badge now updates to reflect the currently deployed release string (`APP_VERSION`).

## v1.4.6 - 2025-11-07

### Enhancements
- Auto-restart on error now reacts to stalled or offline health states instead of relying on log pattern detection.

## v1.4.4 - 2025-11-06

### Fixes
- Version badge and installer defaults now target v1.4.4 so `/opt` deployments report the correct release.

## v1.4.6 - 2025-11-06

### Fixes
- Snapshot restores now reapply the node's existing `network.key` (and create it if missing) after data extraction so restoring someone else's snapshot can’t clone your peer identity.
- Version badge, installers, and docs reference v1.4.6.

## v1.4.5 - 2025-11-06

### Fixes
- Snapshot restores now reapply the service user's ownership after BusyBox extraction so data directories remain accessible and restores no longer fail with permission errors.
- Docs and installers point to the v1.4.5 tag so `/opt` deployments and remote installs fetch the patched build by default.

## v1.4.4 - 2025-11-06

### Fixes
- Peer IDs now populate even when the host filesystem cannot read `network.key`; the manager falls back to `docker exec` to read the file directly from the container.
- Installer docs and helper scripts reference the new v1.4.4 tag so `/opt` deployments pull the latest release by default.

## v1.4.3 - 2025-11-05

### Fixes
- Height Δ charts now use the absolute difference between local and remote heights so the plotted delta always matches the on-card statistic.

## v1.4.2 - 2025-11-05

### Enhancements
- Auto-restart now reacts immediately to critical DAG corruption log lines when error monitoring is enabled.
- Default settings expose the new overclock toggles, wallet slot, and display wallet balance by default to reduce setup time.

### Fixes
- Snapshot creation no longer blocks while a node is still catching up; the height delta guardrail was removed to allow faster snapshot scheduling.

## v1.4.1 - 2025-11-02

### Fixes
- Keep restore progress ETA meaningful while data is unpacked via Docker helpers.

## v1.4.0 - 2025-10-31

### Overclock tab
- New Overclock tab with safe NVMe/CPU/FS tweaks and OverlayFS controls.
- Preflight outlines PLP safety status alongside detected device context.
- Apply/Verify/Revert actions with auto-open logs panel and compact spacing.
- Verify benchmark (fio) with robust JSON parsing and derived IOPS when missing.
- Always-on chart with canvas fallback if Chart.js is unavailable; auto-prime on load.
- Persistent Overclock preferences across refresh; toggles default OFF.
- Removed Manual per request; inline help was removed.

### Installer and scripts
- Ensure fio installed via apt/dnf during install; backend attempts on-demand install.
- Bundled tune script at `scripts/tune_nvme_node.sh` with selective toggles and revert.

### Misc
- Version badge now reflects the runtime version string.

## v1.3.0 - 2025-10-30

### Highlights
- New dashboard metrics, including RPC latency and sync progress tracking
- Recent log viewer with collapsible panel and manual refresh controls
- Auto-restart cooldown setting (hours) with refined restart scheduling
- Updated UI polish: chart tabs, smarter styling, and version badge
- Internal fixes for series caching, block rate calculation, and restart logic

### Upgrade Notes
- Set `BDAG_MANAGER_VERSION` if you need to override `v1.3.0`
- Review the new auto-restart cooldown in Settings → General
- No schema changes; restart services after updating the manager bundle
