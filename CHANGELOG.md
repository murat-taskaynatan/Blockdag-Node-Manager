# Changelog

# Changelog

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
