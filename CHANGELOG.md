# Changelog

# Changelog

## v1.6.9 - 2025-11-26

- Added an About pane with version/update chips, inline install button, and release notes for the latest tag when an update is available; update badges now hide unless the tag is newer than the local build.
- Simplified header by removing the version badge while keeping the update indicator; About tab shows a compact version chip and controlled update/install UI.
- Launchpad defaults to `blockdagnetwork/awakening:v0.0.3` with an image selector for testnet builds; install button styling/hover tweaked.

## v1.6.8 - 2025-11-24

- Liveness recovery hardened: worker-stopped states mark nodes offline, cooldown reset bug fixed, and snapshot sanity checks removed to avoid false negatives.
- UI cleanups: widened remote RPC dropdown, repositioned wallet/snapshot inputs, and added remote RPC profile selector with IP endpoint default.
- Snapshot integrity checks relaxed to avoid false failures; automation logs/queue filters refined.

## v1.6.7 - 2025-11-23

- Liveness policy fixes: global cooldown NameError fixed, container name config corrected for node1 to avoid duplicate entries.
- Snapshot/restore jobs defer during memory restarts and surface deferred entries in the automation queue.
- Launchpad ports auto-alignment and external IP/port advertising corrections for P2P mappings.

## v1.6.6 - 2025-11-22

- Archive mode/pebble corruption patterns added to fatal restore triggers; repeated boot failure heuristic added to liveness restores.
- UI tweaks around stalled/offline health chips and queue duplication handling.
- Snapshot workflow allows restores to proceed after integrity warnings; zero state root hash removed from fatal patterns.

## v1.6.5 - 2025-11-20

- Manual snapshot jobs bypass the sync-progress guard so operators can capture backups even while a node is still catching up; manual invocations now tag jobs with `mode=manual_snapshot`.
- Dropped the deprecated `https://rpc.bdagscan.com` endpoint from the default RPC list so remote calls prefer the Awakening hosts plus the relay fallback.
- Version strings and installer defaults now point to `v1.6.5`, keeping fresh installs aligned with the current release.

## v1.5.7 - 2025-11-15

- Liveness failsafe detection now watches for the “node never became ready” and “worker stopped” log sequences that precede stuck BDAG launches, so the watchdog can suspend until the container is truly unhealthy before escalating.
- Sync ETA chips reuse the same health override context the dashboard uses for the main status badge, which stops the ETA line from showing stale text when progress overrides kick in.
- The UI badge, `APP_VERSION`, and installer defaults now all say `v1.5.7`, keeping fresh installs and diagnostics in sync without manual overrides.
- Remote install helpers and `/opt/blockdag-node-manager` sync scripts now pull the `v1.5.7` tag by default so leaning on the documented workflow delivers the release build.

## v1.5.6 - 2025-11-17

- Start/stop controls now stay disabled with a spinner until the backend confirms each action, and health chips update immediately using the new optimistic state patching.
- The Node Manager UI no longer flickers between stop/start icons during control actions thanks to per-node pending state tracking and forced metrics confirmation.
- Installer scripts and default version badges point to `v1.5.6` so fresh installs pull the latest tag without extra flags.

## v1.5.5 - 2025-11-13

### Snapshot & Recovery
- Snapshot jobs now respect a `Snapshot directory` value stored in `config/settings.json` (and exposed under Settings), so you can point backups at a different disk without editing env files; the UI no longer scans every `/media` path or shows stale “location” warnings, and the Rescan button was removed because `/api/snapshots/scan` simply reports the active directory and list.
- Snapshot creation stops the target container before packaging, flushes overlays, and cleanly returns `snapshot already in progress` when a job is active, fixing the deadlock that previously left `_SNAPSHOT_JOB_STATE` wedged.
- Snapshot and restore jobs now stream more detail into the Automation log: every successful snapshot/restore logs the path, trigger, and restart info, and restores triggered while another job is running emit a “deferred” entry so operators know why an auto-recover skipped.

### Automation & Stability
- Liveness auto-restart refuses to touch a container while snapshot or restore work is active, preventing policy restarts from racing maintenance windows.
- Snapshot status messaging was trimmed so only the active job’s status surfaces in the dashboard instead of repeating stale directory warnings.

### Dashboard & Settings
- Settings adds a dedicated Snapshot directory text field plus layout tweaks around the login tiles, and the footer badge now reads `v1.5.5` so the UI matches the shipped release.

### Installer & Runtime
- `install_nm_from_github.sh` and `install_node_manager.sh` now pin to `v1.5.5` so fresh `/opt` installs pull the tagged release by default.
- `run_node_manager.sh` defaults Waitress to 12 worker threads, a backlog of 256, and unlimited connections, matching the new release’s resource profile out of the box.

## v1.5.4 - 2025-11-13

### Dashboard & Settings
- Added a **Require login** toggle plus inline username/password inputs under Settings so you can enable the dashboard gate and rotate credentials without touching `/etc/blockdag-node-manager/node-manager.env`.
- Reworked the login tiles and automation logs card (inline placement, consistent spacing/borders, clarified copy) so the Settings pane exposes the new controls without clutter.
- Wallet summary copy now matches the data pulled from `rpc.awakening.bdagscan.com`, clarifying that the panel surfaces balances and recent history.

### Snapshot & Recovery
- Installs now seed `BDAG_SNAPSHOT_LIGHT_MODE=1`, and overlay flush/staging only run when an OverlayFS overclock toggle is enabled, dramatically cutting snapshot I/O on hosts that stick with ext4/btrfs.
- Restore jobs stop the target container before extraction (the backend and `scripts/restore_offline_nodes.sh` share the same helper), preventing stale containers from breaking subsequent launches.

### Monitoring & Automation
- Health polling caches remote height RPC responses for five seconds by default (`BDAG_REMOTE_RPC_CACHE_SEC`) to reduce duplicate upstream calls on large fleets.
- Liveness auto-recover now ships disabled by default again; flip it back on from Settings once your nodes are steady to avoid surprise recoveries on fresh installs.

### Launchpad
- Launchpad drops a helper entrypoint script into each deployment and mounts it automatically so nodeworker always invokes the preferred BDAG binary (falling back to `/usr/local/bin/bdag`), removing the need to edit docker-compose by hand.

## v1.5.3 - 2025-11-12
- Added a `BDAG_SNAPSHOT_LIGHT_MODE` switch that skips OverlayFS flushes and staging copies during snapshot jobs and routes restores through the Docker extractor, shaving minutes off backups on storage-constrained hosts.

## v1.5.2 - 2025-11-12

### Installer & Runtime Defaults
- The installer pins the manager service to CPU `0` with a systemd drop-in, bumps Waitress concurrency to 24 threads, and documents `SERVICE_CPU_AFFINITY`/`SERVICE_CPU_WEIGHT` overrides so the UI stays responsive on busy hosts.

### Snapshot & Recovery
- Snapshot creation now stages data with rsync (tolerating vanished files), prunes pre-restore backups, auto-chowns staging directories, and fixes `network.key` ownership so permission errors stop derailing backups.
- Restore jobs automatically fall back to Docker extraction when the host cannot write, suspend liveness auto-recover until the recovered node reports healthy, and prune stale backups to keep disks tidy.

### Launchpad & Automation
- Launchpad installs auto-chown their work directories, clean up busy-port/network leftovers, and keep the Launch button disabled after a success so accidental double submits stop happening.
- Liveness auto-recover now trips after two failed restarts, hides manual restart/restore events from the Automation log, and request logging keeps operators informed whenever the manager triggers a recovery.

### Observability & UI
- Global stats now show the manager's hostname and IP, while the wallet balance toggle was removed so the dashboard simply displays balances whenever a wallet address exists.
- `/api/node-manager` endpoints reuse cached samples instead of blocking on fresh collectors, and the background log viewer refreshes automatically so external tools and the UI stay responsive.

## v1.5.0 - 2025-11-10
- Launchpad now calls the backend to preview auto-selected ports before you reach the Review step, so the UI shows the exact external bindings and blocks launch until the preview succeeds.
- The `/api/node-manager/launch/preview` endpoint reuses the launch logic to expose the resolved P2P/RPC/WS/peer mappings without starting a container.
- Peer fields on the Review screen now keep the internal container port (18150) while the “External peer” row lists the published host port so the mapping is unambiguous.
- Version badge, installer defaults, and docs now point at `v1.5.0` to keep `/opt/blockdag-node-manager` in sync with the latest tag.

## v1.4.9 - 2025-11-09

### Enhancements
- Memory-pressure settings now show a bold `%` prefix, matching toggle sizing, and descriptions that call out the threshold percentage.
- The version badge, installer defaults, and docs now point at `v1.4.9`, keeping `/opt/blockdag-node-manager` and the remote bootstrap helpers aligned with the latest release artifacts.
- Fresh installs now seed `BDAG_LIVENESS_RECOVER_COOLDOWN_SEC=240` and `BDAG_LIVENESS_MAX_RESTARTS=2` in `/etc/blockdag-node-manager/node-manager.env`, so liveness auto-recover reacts within four minutes and escalates to a snapshot restore after two failed restarts.
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
