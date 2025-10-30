# Changelog

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
