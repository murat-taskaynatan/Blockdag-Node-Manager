#!/bin/sh
set -e

# Prefer a bundled binary when present; fall back to whatever nodeworker ships.
if [ -x /opt/bdag/bdag ]; then
  BIN=/opt/bdag/bdag
elif [ -n "${NODE_BINARY:-}" ] && [ -x "${NODE_BINARY:-}" ]; then
  BIN="${NODE_BINARY:-}"
else
  BIN=/usr/local/bin/bdag
fi

echo "Using node binary: $BIN"

exec nodeworker \
  --health.liveness-timeout="${HEALTH_LIVENESS_TIMEOUT:-5m}" \
  --node.addpeer="/ip4/3.70.60.211/tcp/18150/p2p/16Uiu2HAmAu2xQq5E5DywJCu7ktGUsWYLGLAs5LCW2wnxcxKHiwUx" \
  --node.addpeer="/ip4/18.193.1.54/tcp/18150/p2p/16Uiu2HAm3QMgYN8SsaidjknA7dfj7DZNPkrMrdSd8ZPY1hPnrVsj" \
  --node.addpeer="/ip4/18.199.187.186/tcp/18150/p2p/16Uiu2HAkxH7Pk8b9ZqGEHcy9FfHnJDNCiqLLTgvWVNqphUrAPeuV" \
  --node.addpeer="/ip4/52.57.26.194/tcp/18150/p2p/16Uiu2HAmQMJYd2c5WwHNzG7sMwd4Bv2M1M7fCCHR6ttfdYa4AaMe" \
  --node.addpeer="/ip4/3.75.65.108/tcp/18150/p2p/16Uiu2HAmPMcXGqLeiWFsbCf2RGb7h1r6ixcvQBxEGdpB9njntpbD" \
  --node.addpeer="/ip4/3.76.183.36/tcp/18150/p2p/16Uiu2HAm43ujChzL8CVTLqZVbethMofivUDTtaG4fyQ4ndhjJgKc" \
  --node.addpeer="/ip4/13.238.245.105/tcp/18150/p2p/16Uiu2HAmCG4SA8DwxwCf78vGbYax3oPafPjztzryyFvCUCRmX7Ym" \
  --node.addpeer="/ip4/18.193.175.174/tcp/18150/p2p/16Uiu2HAkxH7Pk8b9ZqGEHcy9FfHnJDNCiqLLTgvWVNqphUrAPeuV" \
  --node.addpeer="/ip4/52.29.230.15/tcp/18150/p2p/16Uiu2HAmG1RKi2C3abmQthNWGC8xcQJyoAwnwo19v2VwZoiphGu8" \
  --node.addpeer="/ip4/122.150.184.87/tcp/45664/p2p/16Uiu2HAmURWT6koLHMSpwoUpCAeLYqCPBjA9vmXUoA7RrhbLMqh8" \
  --node.addpeer="/ip4/143.177.191.213/tcp/3141/p2p/16Uiu2HAmEtivgi4CYdoajKavQbjFYcpHY6wGnLCosLJd6UJasRHE" \
  --node.addpeer="/ip4/175.32.40.0/tcp/18150/p2p/16Uiu2HAmKL2BDZ4GHBX9Riz9SFMEjYR6y6pKGvcM1gCpbNCt8p2S" \
  --node.addpeer="/ip4/18.156.10.168/tcp/18150/p2p/16Uiu2HAmKxRXGfDHBuyEdFuBfs1VRyhiDi1ri2STQhbe82sp8xx7" \
  --node.addpeer="/ip4/2.97.156.70/tcp/18150/p2p/16Uiu2HAkvQYqBaqZ4wJNJo3x5bXzB5rVjoJzz2pijzPUQzJDEEL1" \
  --node.addpeer="/ip4/23.23.91.192/tcp/18150/p2p/16Uiu2HAm7YEhEMz2QHdcVuHSPbxpCYKrFgZzdd7VvT2TLhZgfMMn" \
  --node.addpeer="/ip4/23.254.229.135/tcp/18150/p2p/16Uiu2HAm1Sj1PDEyKFf17Ezukdi1T4W3dPmes35uz4StmvFLCAo4" \
  --node.addpeer="/ip4/24.201.247.187/tcp/42457/p2p/16Uiu2HAmUucnTKnMgNtW3G68U97n3kW6we24DmfbPEcdDegpa2DR" \
  --node.addpeer="/ip4/35.73.110.26/tcp/18150/p2p/16Uiu2HAmMnSp4ZZZReaMm9SCmXpFDCyr8DjxNT7zPQy95nBEWnST" \
  --node.addpeer="/ip4/47.156.11.30/tcp/9919/p2p/16Uiu2HAmV1y282RiNTB2kuR7R5oAGqFA27nLVzUsYmZot5o6Y1ys" \
  --node.addpeer="/ip4/51.21.203.97/tcp/18150/p2p/16Uiu2HAmQRxSnb2XekPJSKCJGe4qomYhHCUVNzwGicTdbpSh3ji4" \
  --node.addpeer="/ip4/67.11.196.227/tcp/1024/p2p/16Uiu2HAmUem242D95piix42HRS6RKPZuU68N1B7TZHETGKf5QyiV" \
  --node.addpeer="/ip4/70.55.177.213/tcp/1028/p2p/16Uiu2HAm4QFoLYHhXmSReaNkTHiHa6y345W6QB5n9xgmwjnEmce6" \
  --node.addpeer="/ip4/71.197.176.15/tcp/44065/p2p/16Uiu2HAm3UkeBHfBMFo5vFChWVA96MNAZWvKAbEvtthzBPApmW6Q" \
  --node.addpeer="/ip4/73.79.66.255/tcp/18150/p2p/16Uiu2HAmPD3CBbP2VDy6kkYSGn5vzEgEXRVezARzXBKwGPdgs7JT" \
  --node.addpeer="/ip4/89.160.134.191/tcp/3015/p2p/16Uiu2HAm5u7h69RxsgXSamUzsTKtzMNg4FCdWxgEbrE7YuM2a4Db" \
  --node-binary="$BIN" \
  --node-args="${NODE_ARGS:-}" \
  --rpc-url="${RPC_URL:-}" \
  --contract-address="${CONTRACT_ADDRESS:-}" \
  --rollout-window="${ROLLOUT_WINDOW:-}" \
  --persist-root="${PERSIST_ROOT:-}" \
  --health-min-peers="${HEALTH_MIN_PEERS:-}" \
  --contract-deploy-block="${CONTRACT_DEPLOY_BLOCK:-}"
