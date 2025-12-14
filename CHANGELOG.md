## v1.7.8 - 2025-12-14

### Launchpad
- Drop the required contract address check in the launchpad/nodeworker flow so nodes can start without a deployed contract and avoid “missing bytecode” crashes.

## v1.7.7 - 2025-12-13

### Launchpad
- Stop forcing `--gcmode=archive` when launching nodes; the launchpad now strips archive GC flags so existing databases aren’t tripped by state-scheme mismatches.

## v1.7.6 - 2025-12-07

### Fixes
- Wallet 24h change now uses the nearest sample before the 24h cutoff as the baseline, producing accurate deltas even with sparse polling.
- Liveness recovery detects RLP header decode errors to unblock recovery when block headers are corrupted.
- Default `nodes.json` remains empty so fresh installs don’t inherit local node entries.
- Launchpad: aligned p2p ports, expose UDP + TCP, set maxpeers, and inject external IP for correct announce.
- External reachability checks via ipify/ifconfig and check-host.net port probe to catch firewall/NAT blocks early.

## v1.7.5 - 2025-11-30

### Launchpad Improvements
- Peer connectivity: host/internal peer ports stay aligned, UDP is exposed alongside TCP, and libp2p is launched with matching `--p2ptcpport/--p2pudpport/--port` plus `--maxpeers` so nodes actively listen and dial; external IP is injected for correct announce.
- External reachability: Step 3 surfaces external IP sources (ipify/ifconfig) and the check-host.net port probe so operators can confirm the P2P port is reachable from outside before launch—catching firewall/NAT blocks early.
