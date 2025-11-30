## v1.7.5 - 2025-11-30

### Launchpad Improvements
- Peer connectivity: host/internal peer ports stay aligned, UDP is exposed alongside TCP, and libp2p is launched with matching `--p2ptcpport/--p2pudpport/--port` plus `--maxpeers` so nodes actively listen and dial; external IP is injected for correct announce.
- External reachability: Step 3 surfaces external IP sources (ipify/ifconfig) and the check-host.net port probe, showing “Resolving…” while status is loading so operators can confirm P2P port reachability before launch.

