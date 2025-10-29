(() => {
  const state = {
    nodes: new Map(), // id -> { card, meta }
    charts: new Map(), // id -> Chart instance
    paused: new Set(),
    lastRates: new Map(), // id -> last non-null sync rate
    lastProgress: new Map(), // id -> last non-null sync progress
    lastMetricsTs: 0,
  };

  const cardsContainer = document.getElementById('fleetCards');
  const emptyStateCard = document.getElementById('emptyFleetState');
  const cardTemplate = document.getElementById('nodeCardTemplate');
  const summaryBadge = document.getElementById('globalSummaryBadge');

  const fmt = new Intl.NumberFormat();
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  function numberOrZero(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : 0;
  }

  function recentWindow(series, fallback) {
    const values = Array.isArray(series) && series.length ? series.map(numberOrZero) : [numberOrZero(fallback)];
    return values.length > 5 ? values.slice(-5) : values;
  }

  function isRunningFlag(value) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value > 0;
    if (typeof value === 'string') {
      const normalized = value.trim().toLowerCase();
      if (!normalized) return false;
      return ['true', '1', 'running', 'online', 'yes', 'up', 'active'].includes(normalized);
    }
    return false;
  }

  function shouldForceOffline(stats, running, previousProgress) {
    if (!running) {
      return false;
    }
    const localSeries = Array.isArray(stats.local) ? stats.local.map(numberOrZero) : [];
    const remoteSeries = Array.isArray(stats.remote) ? stats.remote.map(numberOrZero) : [];
    const hadSeriesProgress = localSeries.some((value) => value > 0);
    const prevProgressValue = numberOrZero(previousProgress);
    const hadProgress = hadSeriesProgress || prevProgressValue > 0;
    const recentLocal = recentWindow(localSeries, stats.local_height);
    const zeroedRecentLocal = recentLocal.every((value) => value <= 0);
    const localHeight = numberOrZero(stats.local_height);
    const zeroLocal = localHeight <= 0 && zeroedRecentLocal;
    const recentRemote = recentWindow(remoteSeries, stats.remote_height);
    const remotePositive = recentRemote.some((value) => value > 0) || numberOrZero(stats.remote_height) > 0;
    const peers = numberOrZero(stats.peers);
    const uptime = numberOrZero(stats.uptime_seconds);
    if (hadProgress && zeroLocal && remotePositive && peers <= 0) {
      return true;
    }
    if (zeroLocal && remotePositive && peers <= 0 && uptime > 180) {
      return true;
    }
    return false;
  }

  function renderSyncSummary(pill, progress, rate, meta = {}) {
    if (!pill) {
      return;
    }
    const hasProgress = Number.isFinite(progress);
    const hasRate = Number.isFinite(rate) && rate > 0;
    const progressText = hasProgress ? `${progress.toFixed(1)}%` : '—';
    const rateValue = hasRate ? (rate >= 10 ? rate.toFixed(1) : rate.toFixed(2)) : '—';
    pill.textContent = `Synced ${progressText} ${rateValue} blk/s`;
    if (hasProgress || hasRate) {
      const local = meta.local_height ?? meta.local;
      const remote = meta.remote_height ?? meta.remote;
      const details = [];
      if (hasProgress && Number.isFinite(local) && Number.isFinite(remote)) {
        details.push(`Local ${local} of ${remote}`);
      }
      if (hasRate) {
        details.push(`${rateValue} blocks/s`);
      }
      pill.title = details.length ? details.join(' • ') : '';
    } else {
      pill.removeAttribute('title');
    }
  }

  function resolveHealth(stats, running, options = {}) {
    const { forceOffline = false } = options;
    const detail = (stats.health_detail || stats.health_text || '').toString().trim();
    const online = running && !forceOffline;
    const display = online ? 'Online' : 'Offline';
    const code = online ? 'online' : 'offline';
    return { display, detail, code };
  }

  async function loadNodes() {
    try {
      const res = await fetch('/api/node-manager/nodes', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      renderSummary(payload.summary || {});
      syncCards(payload.nodes || []);
      toggleEmptyState();
    } catch (err) {
      console.error('[fleet] failed to load nodes', err);
      renderSummary({ error: err.message });
    }
  }

  function renderSummary(summary) {
    const countEl = document.getElementById('statNodeCount');
    const onlineEl = document.getElementById('statOnline');
    const offlineEl = document.getElementById('statOffline');
    const maxLocalEl = document.getElementById('statMaxLocal');
    const maxRemoteEl = document.getElementById('statMaxRemote');

    if (!summary || Object.keys(summary).length === 0) {
      summaryBadge.textContent = '—';
      summaryBadge.removeAttribute('title');
      countEl.textContent = onlineEl.textContent = offlineEl.textContent = maxLocalEl.textContent = maxRemoteEl.textContent = '—';
      return;
    }

    const count = summary.count ?? 0;
    const online = summary.running ?? 0;
    const offline = summary.offline ?? Math.max(count - online, 0);

    countEl.textContent = fmt.format(count);
    onlineEl.textContent = fmt.format(online);
    offlineEl.textContent = fmt.format(offline);
    maxLocalEl.textContent = summary.max_local_height !== undefined ? fmt.format(summary.max_local_height) : '—';
    maxRemoteEl.textContent = summary.max_remote_height !== undefined ? fmt.format(summary.max_remote_height) : '—';

    const walletEnabled = !!summary.wallet_enabled;
    const wallet = walletEnabled ? summary.wallet || {} : null;
    const ts = summary.timestamp ? new Date(summary.timestamp * 1000) : null;
    let badgeText = '';
    let badgeTitle = '';
    if (walletEnabled) {
      if (wallet.error) {
        badgeText = wallet.error;
      } else if (wallet.balance_formatted) {
        badgeText = wallet.short || wallet.balance_formatted;
        badgeTitle = wallet.address || '';
      } else if (wallet.address) {
        badgeText = '(fetching…)';
        badgeTitle = wallet.address;
      }
    } else {
      badgeText = 'last seen';
    }
    if (!badgeText) {
      badgeText = 'last seen';
    }
    if (ts) {
      const timeText = fmtTime.format(ts);
      badgeText = badgeText ? `${badgeText} · ${timeText}` : timeText;
    }
    summaryBadge.textContent = badgeText;
    if (badgeTitle) {
      summaryBadge.title = badgeTitle;
    } else if (wallet && wallet.address) {
      summaryBadge.title = wallet.address;
    } else {
      summaryBadge.removeAttribute('title');
    }
  }

  function syncCards(nodes) {
    const seen = new Set();
    sortNodes(nodes).forEach((node) => {
      if (!node || !node.id) return;
      seen.add(node.id);
      const existing = state.nodes.get(node.id);
      if (!existing) {
        createCard(node);
      } else {
        updateCardHeader(node);
      }
    });

    // remove cards for nodes that vanished
    Array.from(state.nodes.keys()).forEach((nodeId) => {
      if (!seen.has(nodeId)) {
        const entry = state.nodes.get(nodeId);
        if (entry?.card?.parentElement) {
          entry.card.parentElement.removeChild(entry.card);
        }
        const chart = state.charts.get(nodeId);
        if (chart) {
          try { chart.destroy(); } catch (err) { console.warn('[fleet] chart destroy failed', err); }
        }
        state.nodes.delete(nodeId);
        state.charts.delete(nodeId);
        state.paused.delete(nodeId);
        state.lastRates.delete(nodeId);
        state.lastProgress.delete(nodeId);
      }
    });
  }

  function toggleEmptyState() {
    const hasCards = state.nodes.size > 0;
    if (emptyStateCard) {
      emptyStateCard.style.display = hasCards ? 'none' : 'block';
    }
  }

  function sortNodes(list) {
    return (list || []).slice().sort((a, b) => {
      const parseInfo = (node) => {
        const fallback = { num: null, original: (node && node.id) ? node.id : '' };
        if (!node || !node.id) return fallback;
        const suffix = node.id.toString().split('-').pop();
        const suffixNum = Number(suffix);
        if (Number.isFinite(suffixNum)) return { num: suffixNum, original: node.id };
        const digits = (node.id.match(/\d+/g) || []).map(Number).filter((n) => Number.isFinite(n));
        if (digits.length) return { num: digits[0], original: node.id };
        return fallback;
      };
      const aInfo = parseInfo(a);
      const bInfo = parseInfo(b);
      if (aInfo.num === null && bInfo.num === null) return aInfo.original.localeCompare(bInfo.original);
      if (aInfo.num === null) return -1;
      if (bInfo.num === null) return 1;
      if (aInfo.num !== bInfo.num) return aInfo.num - bInfo.num;
      return aInfo.original.localeCompare(bInfo.original);
    });
  }

  function createCard(node) {
    const fragment = cardTemplate.content.cloneNode(true);
    const details = fragment.querySelector('.fleet-card');
    if (details) {
      details.open = false;
      details.removeAttribute('open');
    }
    details.dataset.nodeId = node.id;

    const nameEl = details.querySelector('.node-name');
    const metaEl = details.querySelector('.node-meta');
    nameEl.textContent = node.label || node.id;
    metaEl.textContent = node.container || '—';

    const summary = details.querySelector('summary.fleet-summary');
    if (summary) {
      summary.setAttribute('title', 'Click to expand');
      details.addEventListener('toggle', () => {
        summary.setAttribute('title', details.open ? 'Click to collapse' : 'Click to expand');
      });
    }

    const toggleBtn = details.querySelector('[data-action="toggle"]');
    if (toggleBtn) {
      const handler = async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await startStopNode(node.id, toggleBtn);
      };
      toggleBtn.addEventListener('click', handler);
      toggleBtn.addEventListener('mousedown', (event) => event.stopPropagation());
      toggleBtn.addEventListener('mouseup', (event) => event.stopPropagation());
      toggleBtn.title = 'Start/Stop container';
    }
    const restartBtn = details.querySelector('[data-action="restart"]');
    if (restartBtn) {
      const handler = async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await restartNode(node.id);
      };
      restartBtn.addEventListener('click', handler);
      restartBtn.addEventListener('mousedown', (event) => event.stopPropagation());
      restartBtn.addEventListener('mouseup', (event) => event.stopPropagation());
      restartBtn.title = 'Restart container';
    }

    cardsContainer.appendChild(details);
    state.nodes.set(node.id, { card: details, meta: node });
    updateCardHeader(node);

    const canvas = details.querySelector('canvas');
    if (canvas && typeof Chart === 'function') {
      const chart = createChart(canvas.getContext('2d'));
      state.charts.set(node.id, chart);
    } else {
      console.warn('[fleet] chart unavailable; skipping chart init for', node.id);
    }
  }

  function updateCardHeader(node) {
    const entry = state.nodes.get(node.id);
    if (!entry) return;
    entry.meta = node;

    const card = entry.card;
    if (card) {
      card.open = false;
      card.removeAttribute('open');
    }
    const workerLabel = (() => {
      const suffix = node.id.toString().split('-').pop();
      const num = Number(suffix);
      if (Number.isFinite(num) && num >= 1) {
        return `Node Worker - ${num}`;
      }
      const digits = (node.id.match(/\d+/g) || []).map(Number).filter((n) => Number.isFinite(n));
      if (digits.length) {
        return `Node Worker - ${digits[0]}`;
      }
      const orderedKeys = Array.from(state.nodes.keys()).sort();
      const index = orderedKeys.indexOf(node.id);
      const ordinal = index >= 0 ? index + 1 : state.nodes.size + 1;
      return `Node Worker - ${ordinal}`;
    })();
    card.querySelector('.node-name').textContent = workerLabel;
    card.querySelector('.node-meta').textContent = node.container || '—';

    const summaryHealthChip = card.querySelector('.summary-health-chip');
    const summarySyncPill = card.querySelector('[data-role="sync-pill"]');
    const statusEl = card.querySelector('.status-text');
    const stats = node.status || {};
    entry.meta.status = stats;
    const rawRunning = isRunningFlag(stats.running);
    const forceOfflineHeader = shouldForceOffline(stats, rawRunning, state.lastProgress.get(node.id));
    const effectiveRunning = rawRunning && !forceOfflineHeader;
    if (statusEl) {
      statusEl.textContent = '';
      statusEl.parentElement.classList.toggle('is-ok', rawRunning);
      statusEl.parentElement.classList.toggle('is-warn', !rawRunning);
    }

    const health = resolveHealth(stats, rawRunning, { forceOffline: forceOfflineHeader });
    const displayHealth = health.display;
    const code = health.code;
    const healthDetail = health.detail;
    if (summaryHealthChip) {
      summaryHealthChip.textContent = displayHealth || 'Status';
      if (healthDetail || displayHealth) {
        summaryHealthChip.title = healthDetail || displayHealth;
      } else {
        summaryHealthChip.removeAttribute('title');
      }
      const isOnline = code === 'online';
      summaryHealthChip.classList.remove('health-online', 'health-offline');
      summaryHealthChip.classList.add(isOnline ? 'health-online' : 'health-offline');
    }

    renderSyncSummary(summarySyncPill, state.lastProgress.get(node.id), state.lastRates.get(node.id), {
      local_height: stats.local_height,
      remote_height: stats.remote_height,
    });

    setStat(card, '.stat-local', stats.local_height);
    setStat(card, '.stat-remote', stats.remote_height);
    setStat(card, '.stat-delta', stats.height_delta, { sign: true });
    setStat(card, '.stat-peers', stats.peers);
    updateUptime(card, stats.uptime_seconds);
    updateStartStopButton(card.querySelector('[data-action="toggle"]'), effectiveRunning, {
      rawRunning,
      forcedOffline: forceOfflineHeader,
    });
    if (entry.meta && entry.meta.status) {
      entry.meta.status.raw_running = rawRunning;
      entry.meta.status.forced_offline = forceOfflineHeader;
      entry.meta.status.effective_running = effectiveRunning;
    }
  }

  function setStat(card, selector, value, opts = {}) {
    const el = card.querySelector(selector);
    if (!el) return;
    if (value === undefined || value === null || Number.isNaN(value)) {
      el.textContent = '—';
      el.classList.remove('is-warn');
      return;
    }
    const num = Number(value);
    const text = opts.sign && num > 0 ? `+${fmt.format(num)}` : fmt.format(num);
    el.textContent = text;
    if (selector.includes('delta')) {
      el.classList.toggle('is-warn', num > 8);
    }
  }

  function updateUptime(card, seconds) {
    const el = card.querySelector('.stat-uptime');
    if (!el) return;
    const total = Number(seconds);
    if (!Number.isFinite(total) || total <= 0) {
      el.textContent = '—';
      return;
    }
    el.textContent = formatEtaDuration(total);
  }

  function formatEtaDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    if (!Number.isFinite(total) || total <= 0) {
      return '<1m';
    }
    const units = [
      { label: 'd', value: 86400 },
      { label: 'h', value: 3600 },
      { label: 'm', value: 60 },
    ];
    const parts = [];
    let remaining = total;
    for (const unit of units) {
      if (remaining >= unit.value) {
        const count = Math.floor(remaining / unit.value);
        parts.push(`${count}${unit.label}`);
        remaining %= unit.value;
      }
      if (parts.length === 2) {
        break;
      }
    }
    if (parts.length === 0) {
      return '<1m';
    }
    return parts.join(' ');
  }

  function averageHeightRate(labels, localSeries, windowSec = 300) {
    const len = Math.min(
      Array.isArray(labels) ? labels.length : 0,
      Array.isArray(localSeries) ? localSeries.length : 0,
    );
    if (len < 2) return null;
    const samples = [];
    for (let idx = len - 1; idx >= 0 && samples.length < 20; idx -= 1) {
      const ts = Number(labels[idx]);
      const rawVal = localSeries[idx];
      if (rawVal === null || rawVal === undefined) continue;
      const val = Number(rawVal);
      if (!Number.isFinite(ts) || !Number.isFinite(val) || val <= 0) continue;
      if (samples.length && samples[samples.length - 1].ts === ts) continue;
      samples.push({ ts, val });
    }
    if (samples.length < 2) return null;
    const latest = samples[0];
    const cutoff = latest.ts - Math.max(1, windowSec) * 1000;
    let anchor = samples[samples.length - 1];
    for (let i = 1; i < samples.length; i += 1) {
      const candidate = samples[i];
      if (candidate.ts <= cutoff || i === samples.length - 1) {
        anchor = candidate;
        break;
      }
    }
    const dtSec = (latest.ts - anchor.ts) / 1000;
    if (!Number.isFinite(dtSec) || dtSec <= 0) return null;
    const delta = latest.val - anchor.val;
    if (!Number.isFinite(delta) || delta <= 0) return null;
    const rate = delta / dtSec;
    if (!Number.isFinite(rate) || rate <= 0) return null;
    return rate;
  }

  function computeSyncProgress(metrics) {
    const remote = Number(metrics.remote_height);
    const local = Number(metrics.local_height);
    if (!Number.isFinite(remote) || remote <= 0 || !Number.isFinite(local) || local < 0) {
      return null;
    }
    const ratio = Math.min(1, Math.max(0, local / remote));
    return ratio * 100;
  }

  function computeEtaInfo(metrics) {
    const remote = Number(metrics.remote_height);
    const local = Number(metrics.local_height);
    if (!Number.isFinite(remote) || !Number.isFinite(local)) {
      return null;
    }
    const remaining = remote - local;
    if (!Number.isFinite(remaining)) {
      return null;
    }
    if (remaining <= 0) {
      return { text: 'Fully synced', variant: 'ok' };
    }
    const rate = averageHeightRate(metrics.labels, metrics.local);
    if (!Number.isFinite(rate) || rate <= 0) {
      return { text: 'ETA pending…', variant: null };
    }
    const etaSec = remaining / rate;
    if (!Number.isFinite(etaSec) || etaSec <= 0 || etaSec > 86400 * 30) {
      return { text: 'ETA pending…', variant: null };
    }
    const pretty = formatEtaDuration(etaSec);
    let variant = 'warn';
    if (etaSec <= 900) {
      variant = 'ok';
    } else if (etaSec >= 21600) {
      variant = 'danger';
    }
    return { text: `ETA ~ ${pretty}`, variant };
  }

  function updateEta(card, metrics) {
    const etaEl = card.querySelector('.stat-eta');
    if (!etaEl) return;
    etaEl.classList.remove('is-ok', 'is-warn', 'is-danger');
    const info = computeEtaInfo(metrics);
    if (!info) {
      etaEl.textContent = '—';
      return;
    }
    etaEl.textContent = info.text;
    if (info.variant) {
      etaEl.classList.add(`is-${info.variant}`);
    }
  }

  function createChart(ctx) {
    return new Chart(ctx, {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          {
            label: 'Height Δ',
            data: [],
            borderColor: '#ffb74d',
            backgroundColor: 'rgba(255,183,77,0.2)',
            fill: true,
            tension: 0.25,
            borderWidth: 2,
            pointRadius: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: {
            labels: { color: '#aeb5cf', usePointStyle: true },
          },
          tooltip: {
            callbacks: {
              title: (items) => items.map((item) => item.label).join(', '),
              label: (ctx) => `${ctx.dataset.label}: ${fmt.format(ctx.parsed.y ?? 0)}`,
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#7681a8', maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
            grid: { color: 'rgba(255,255,255,0.06)' },
          },
          y: {
            ticks: { color: '#7681a8', callback: (val) => fmt.format(val), align: 'inner', crossAlign: 'near' },
            grid: { color: 'rgba(255,255,255,0.06)' },
            position: 'right',
          },
        },
      },
    });
  }

  function updateStartStopButton(btn, running, options = {}) {
    if (!btn) return;
    const { rawRunning = running, forcedOffline = false } = options;
    const effectiveRunning = running === true;
    let action = 'start';
    if (effectiveRunning) {
      action = 'stop';
    } else if (rawRunning && forcedOffline) {
      action = 'restart';
    }
    btn.dataset.running = effectiveRunning ? '1' : '0';
    btn.dataset.rawRunning = rawRunning ? '1' : '0';
    btn.dataset.action = action;
    let icon = '▶';
    let aria = 'Start node';
    let title = 'Start container';
    if (action === 'stop') {
      icon = '⏹';
      aria = 'Stop node';
      title = 'Stop container';
    } else if (action === 'restart') {
      icon = '▶';
      aria = 'Restart node';
      title = 'Restart container';
    }
    btn.innerHTML = `<span class="icon">${icon}</span>`;
    btn.setAttribute('aria-label', aria);
    btn.title = title;
  }

  async function startStopNode(nodeId, btn) {
    const entry = state.nodes.get(nodeId);
    if (!entry) return;
    const meta = entry.meta || {};
    const container = meta.container || meta.id;
    if (!container) return;
    const status = meta.status || {};
    const rawRunning = isRunningFlag(status.running);
    const previousProgress = state.lastProgress.get(nodeId);
    const forcedOffline = shouldForceOffline(status, rawRunning, previousProgress);
    const effectiveRunning = rawRunning && !forcedOffline;
    let action = 'docker_start';
    if (effectiveRunning) {
      action = 'docker_stop';
    } else if (rawRunning && forcedOffline) {
      action = 'docker_restart';
    }
    if (btn) btn.disabled = true;
    try {
      const res = await fetch('/api/control', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action, container, node: nodeId }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('[fleet] start/stop failed', err);
    } finally {
      if (btn) btn.disabled = false;
      await loadNodes();
      await refreshMetrics();
    }
  }

  async function restartNode(nodeId) {
    const entry = state.nodes.get(nodeId);
    if (!entry) return;
    const meta = entry.meta || {};
    const container = meta.container || meta.id;
    if (!container) return;
    const btn = entry.card.querySelector('[data-action="restart"]');
    if (btn) btn.disabled = true;
    try {
      const res = await fetch('/api/control', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action: 'docker_restart', container, node: nodeId }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('[fleet] restart failed', err);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function discoverNodes(options = {}) {
    const auto = options.auto === true;
    const btn = document.getElementById('btnDiscoverNodes');
    if (btn) btn.disabled = true;
    const maxPasses = 8;
    let pass = 0;
    try {
      while (pass < maxPasses) {
        pass += 1;
        const res = await fetch('/api/node-manager/discover', { method: 'POST', headers: { 'content-type': 'application/json' } });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        let payload = {};
        try {
          payload = await res.json();
        } catch (_) {
          payload = {};
        }
        if (payload && payload.ok === false) {
          throw new Error(payload.error || 'discovery rejected');
        }
        await loadNodes();
        await refreshMetrics();
        const added = Array.isArray(payload?.added) ? payload.added.length : 0;
        const removed = Array.isArray(payload?.removed) ? payload.removed.length : 0;
        const updated = Array.isArray(payload?.updated) ? payload.updated.length : 0;
        if (!(added > 0 || removed > 0 || updated > 0)) {
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
    } catch (err) {
      console.error('[fleet] discovery failed', err);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function refreshMetrics() {
    if (!state.nodes.size) return;
    const nodeIds = Array.from(state.nodes.keys());
    const query = encodeURIComponent(nodeIds.join(','));
    try {
      const res = await fetch(`/api/node-manager/metrics?nodes=${query}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      applyMetrics(payload.nodes || {});
    } catch (err) {
      console.error('[fleet] metrics refresh failed', err);
    }
  }

  function applyMetrics(metricsByNode) {
    const now = Date.now();
    Object.entries(metricsByNode).forEach(([nodeId, metrics]) => {
      const entry = state.nodes.get(nodeId);
      if (!entry) return;
      const card = entry.card;

      entry.meta = entry.meta || {};

      setStat(card, '.stat-local', metrics.local_height);
      setStat(card, '.stat-remote', metrics.remote_height);
      setStat(card, '.stat-delta', metrics.height_delta, { sign: true });
      setStat(card, '.stat-peers', metrics.peers);
      updateUptime(card, metrics.uptime_seconds);

      const rawRunning = isRunningFlag(metrics.running);
      const nodeStatusEl = card.querySelector('.node-status');
      if (nodeStatusEl) {
        nodeStatusEl.classList.toggle('is-ok', rawRunning);
        nodeStatusEl.classList.toggle('is-warn', !rawRunning);
        const textEl = nodeStatusEl.querySelector('.status-text');
        if (textEl) {
          textEl.textContent = '';
        }
      }

      const summaryHealthChip = card.querySelector('.summary-health-chip');
      const previousProgress = state.lastProgress.get(nodeId);
      const forceOffline = shouldForceOffline(metrics, rawRunning, previousProgress);
      const effectiveRunning = rawRunning && !forceOffline;
      const health = resolveHealth(metrics, rawRunning, { forceOffline });
      const displayHealth = health.display;
      const healthDetail = health.detail;
      const code = health.code;
      if (summaryHealthChip) {
        summaryHealthChip.textContent = displayHealth || 'Status';
        if (healthDetail || displayHealth) {
          summaryHealthChip.title = healthDetail || displayHealth;
        } else {
          summaryHealthChip.removeAttribute('title');
        }
        const isOnline = code === 'online';
        summaryHealthChip.classList.remove('health-online', 'health-offline');
        summaryHealthChip.classList.add(isOnline ? 'health-online' : 'health-offline');
      }

      const summarySyncPill = card.querySelector('[data-role="sync-pill"]');
      let progress = computeSyncProgress(metrics);
      if (progress === null || !Number.isFinite(progress)) {
        progress = state.lastProgress.get(nodeId);
      } else {
        state.lastProgress.set(nodeId, progress);
      }
      let rate = averageHeightRate(metrics.labels, metrics.local);
      if (Number.isFinite(rate) && rate > 0) {
        state.lastRates.set(nodeId, rate);
      } else {
        rate = state.lastRates.get(nodeId);
      }
      renderSyncSummary(summarySyncPill, progress, rate, {
        local_height: metrics.local_height,
        remote_height: metrics.remote_height,
      });

      updateStartStopButton(card.querySelector('[data-action="toggle"]'), effectiveRunning, {
        rawRunning,
        forcedOffline: forceOffline,
      });
      entry.meta.status = {
        ...(entry.meta.status || {}),
        ...metrics,
        raw_running: rawRunning,
        forced_offline: forceOffline,
        effective_running: effectiveRunning,
      };

      const tsEl = card.querySelector('.stat-updated');
      if (tsEl) {
        const ts = metrics.last_updated ? new Date(metrics.last_updated) : new Date(now);
        tsEl.textContent = fmtTime.format(ts);
      }

      updateEta(card, metrics);

      if (state.paused.has(nodeId)) {
        return;
      }

      const chart = state.charts.get(nodeId);
      if (!chart) return;
      const labels = (metrics.labels || []).map((stamp) => {
        try {
          return fmtTime.format(new Date(stamp));
        } catch (_) {
          return stamp;
        }
      });
      const localSeries = Array.isArray(metrics.local) ? metrics.local : [];
      const remoteSeries = Array.isArray(metrics.remote) ? metrics.remote : [];
      const deltaSeries = localSeries.map((localVal, idx) => {
        const remoteVal = remoteSeries[idx];
        if (localVal === null || localVal === undefined) return null;
        if (remoteVal === null || remoteVal === undefined) return null;
        const localNum = Number(localVal);
        const remoteNum = Number(remoteVal);
        const delta = Number.isFinite(remoteNum - localNum) ? remoteNum - localNum : null;
        return delta;
      });
      chart.data.labels = labels;
      chart.data.datasets[0].data = deltaSeries;
      chart.update('none');
    });
  }

  function attachEventHandlers() {
    const refreshBtn = document.getElementById('btnRefreshFleet');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', async () => {
        refreshBtn.disabled = true;
        await loadNodes();
        await refreshMetrics();
        refreshBtn.disabled = false;
      });
    }
    const discoverBtn = document.getElementById('btnDiscoverNodes');
    if (discoverBtn) {
      discoverBtn.addEventListener('click', () => discoverNodes());
    }
  }

  async function init() {
    attachEventHandlers();
    await loadNodes();
    await refreshMetrics();
    await discoverNodes({ auto: true });
    await refreshMetrics();
    setInterval(refreshMetrics, 5000);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshMetrics();
    }
  });

  window.addEventListener('load', init);
})();
