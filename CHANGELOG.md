# Changelog

## v1.5.0 - 2025-11-10
- Launchpad now calls the backend to preview auto-selected ports before you reach the Review step, so the UI shows the exact external bindings and blocks launch until the preview succeeds.
- The `/api/node-manager/launch/preview` endpoint reuses the launch logic to expose the resolved P2P/RPC/WS/peer mappings without starting a container.
- Peer fields on the Review screen now keep the internal container port (18150) while the “External peer” row lists the published host port so the mapping is unambiguous.
- Version badge, installer defaults, and docs now point at `v1.5.0` to keep `/opt/blockdag-node-manager` in sync with the latest tag.

## v1.4.9 - 2025-11-09

### Enhancements
- Memory-pressure settings now show a bold `%` prefix, matching toggle sizing, and descriptions that call out the threshold percentage.
- The version badge, installer defaults, and docs now point at `v1.4.9`, keeping `/opt/blockdag-node-manager` and the remote bootstrap helpers aligned with the latest release artifacts.
- Fresh installs now seed `BDAG_LIVENESS_RECOVER_COOLDOWN_SEC=240` and `BDAG_LIVENESS_MAX_RESTARTS=3` in `/etc/blockdag-node-manager/node-manager.env`, so liveness auto-recover reacts within four minutes and escalates to a snapshot restore after three failed restarts.
- Added an Automation Logs panel under Settings that streams recent auto restarts, liveness restores, and auto snapshots so operators can audit recovery activity without leaving the UI.
- `/api/node-manager/nodes` now returns cached NodeContext snapshots (background sampling honors `BDAG_SAMPLE_SEC`, seeded to 30 seconds for new installs) instead of forcing fresh samples on every request.

## v1.4.8 - 2025-11-08

### Enhancements
- Remote installs now ship with a `BDAG_LOGIN_ENABLED=0` toggle so the login portal is disabled by default while `/opt` deployments stay in sync with the release badge.
- Installer docs, bootstrap helpers, and `/etc/blockdag-node-manager/node-manager.env` notes were updated to explain how to flip `BDAG_LOGIN_ENABLED=1` and provide credentials when you need authentication.
- The CPU monitor now defaults to `/mnt/hgfs/vmshared/cpu_temp.txt` (with `BDAG_CPU_TEMP_PATH` seeded accordingly) so shared-VM temperature files work without extra setup, and changing the path through Settings persists into `config/settings.json`.

## v1.4.7 - 2025-11-07

### Enhancements
- Summary counters now distinguish healthy (running) nodes from forced/stalled ones so the “Offline” tally only includes truly offline containers.
- Liveness auto-recovery now also reacts to “Illegal withdrawal at block:difflayer…” messages so the health guard triggers on this corruption warning.
- Liveness auto-recovery now attempts a restore from the latest snapshot before falling back to a container restart when health checks fail.
- Liveness auto-recovery now triggers restores only; container restarts stay under the “Auto restart on error” toggle.
- Liveness auto-recovery now tries a small number of restarts before falling back to a snapshot restore, keeping restores as a last resort.
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
