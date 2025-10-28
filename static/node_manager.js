(() => {
  const state = {
    nodes: new Map(), // id -> { card, meta }
    charts: new Map(), // id -> Chart instance
    paused: new Set(),
    lastMetricsTs: 0,
  };

  const cardsContainer = document.getElementById('fleetCards');
  const emptyStateCard = document.getElementById('emptyFleetState');
  const cardTemplate = document.getElementById('nodeCardTemplate');
  const summaryBadge = document.getElementById('globalSummaryBadge');

  const fmt = new Intl.NumberFormat();
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

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
      summaryBadge.textContent = 'No data';
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

    const ts = summary.timestamp ? new Date(summary.timestamp * 1000) : null;
    const suffix = ts ? ` · ${fmtTime.format(ts)}` : '';
    summaryBadge.textContent = `${fmt.format(count)} node${count === 1 ? '' : 's'}${suffix}`;
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

    const summaryIndicator = card.querySelector('.summary-status-indicator');
    const summaryStatusText = card.querySelector('.summary-status-text');
    const summaryHealthChip = card.querySelector('.summary-health-chip');
    const statusEl = card.querySelector('.status-text');
    const stats = node.status || {};
    const running = !!stats.running;
    statusEl.textContent = '';
    statusEl.parentElement.classList.toggle('is-ok', running);
    statusEl.parentElement.classList.toggle('is-warn', !running);

    if (summaryIndicator) {
      summaryIndicator.classList.remove('is-ok', 'is-warn');
      summaryIndicator.classList.add(running ? 'is-ok' : 'is-warn');
    }
    if (summaryStatusText) {
      summaryStatusText.textContent = running ? 'Online' : 'Offline';
      summaryStatusText.classList.remove('is-online', 'is-offline');
      summaryStatusText.classList.add(running ? 'is-online' : 'is-offline');
    }
    const healthDetail = (stats.health_detail || stats.health_text || '').toString().trim();
    const healthLabel = (stats.health_label || stats.health_text || '').toString().trim();
    const displayHealth = healthLabel || (running ? 'Healthy' : 'Offline');
    const code = (stats.health_code || '').toString().toLowerCase();
    if (summaryHealthChip) {
      summaryHealthChip.textContent = displayHealth || 'Health';
      if (healthDetail || displayHealth) {
        summaryHealthChip.title = healthDetail || displayHealth;
      } else {
        summaryHealthChip.removeAttribute('title');
      }
      summaryHealthChip.classList.remove('health-ok', 'health-warn', 'health-bad');
      const lower = displayHealth.toLowerCase();
      const okCodes = new Set(['healthy', 'steady', 'mining']);
      const warnCodes = new Set(['syncing', 'downloading', 'initializing', 'no_peers']);
      const badCodes = new Set(['offline', 'stalled', 'error']);
      if (okCodes.has(code) || lower.includes('healthy') || lower.includes('mining')) {
        summaryHealthChip.classList.add('health-ok');
      } else if (badCodes.has(code) || lower.includes('stall') || (!running && lower)) {
        summaryHealthChip.classList.add('health-bad');
      } else if (warnCodes.has(code) || lower.includes('sync') || lower.includes('download')) {
        summaryHealthChip.classList.add('health-warn');
      }
    }

    setStat(card, '.stat-local', stats.local_height);
    setStat(card, '.stat-remote', stats.remote_height);
    setStat(card, '.stat-delta', stats.height_delta, { sign: true });
    setStat(card, '.stat-peers', stats.peers);
    updateStartStopButton(card.querySelector('[data-action="toggle"]'), running);
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
    const labels = Array.isArray(metrics.labels) ? metrics.labels : [];
    const localSeries = Array.isArray(metrics.local) ? metrics.local : [];
    const len = Math.min(labels.length, localSeries.length);
    const samples = [];
    for (let idx = len - 1; idx >= 0 && samples.length < 6; idx -= 1) {
      const ts = Number(labels[idx]);
      const val = Number(localSeries[idx]);
      if (Number.isFinite(ts) && Number.isFinite(val)) {
        samples.push({ ts, val });
      }
    }
    if (samples.length < 2) {
      return { text: 'ETA pending…', variant: null };
    }
    const latest = samples[0];
    const earliest = samples[samples.length - 1];
    const dtSec = (latest.ts - earliest.ts) / 1000;
    const delta = latest.val - earliest.val;
    if (!Number.isFinite(dtSec) || dtSec <= 0 || !Number.isFinite(delta) || delta <= 0) {
      return { text: 'ETA pending…', variant: null };
    }
    const rate = delta / dtSec;
    if (!Number.isFinite(rate) || rate <= 0) {
      return { text: 'ETA pending…', variant: null };
    }
    const etaSec = remaining / rate;
    if (!Number.isFinite(etaSec) || etaSec <= 0) {
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
            ticks: { color: '#7681a8', callback: (val) => fmt.format(val) },
            grid: { color: 'rgba(255,255,255,0.06)' },
          },
        },
      },
    });
  }

  function updateStartStopButton(btn, running) {
    if (!btn) return;
    btn.dataset.running = running ? '1' : '0';
    btn.innerHTML = running ? '<span class="icon">⏹</span>' : '<span class="icon">▶</span>';
    btn.setAttribute('aria-label', running ? 'Stop node' : 'Start node');
    btn.title = running ? 'Stop container' : 'Start container';
  }

  async function startStopNode(nodeId, btn) {
    const entry = state.nodes.get(nodeId);
    if (!entry) return;
    const meta = entry.meta || {};
    const container = meta.container || meta.id;
    if (!container) return;
    const status = meta.status || {};
    const running = !!status.running;
    const action = running ? 'docker_stop' : 'docker_start';
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
      entry.meta.status = { ...(entry.meta.status || {}), ...metrics };

      setStat(card, '.stat-local', metrics.local_height);
      setStat(card, '.stat-remote', metrics.remote_height);
      setStat(card, '.stat-delta', metrics.height_delta, { sign: true });
      setStat(card, '.stat-peers', metrics.peers);

      const statusEl = card.querySelector('.node-status');
      const running = !!metrics.running;
      if (statusEl) {
        statusEl.classList.toggle('is-ok', running);
        statusEl.classList.toggle('is-warn', !running);
        const textEl = statusEl.querySelector('.status-text');
        if (textEl) {
          textEl.textContent = running ? 'Online' : 'Offline';
        }
      }
      const statusChip = card.querySelector('.summary-status-chip');
      if (statusChip) {
        statusChip.textContent = running ? 'Online' : 'Offline';
        statusChip.title = running ? 'Container running' : 'Container offline';
        statusChip.classList.remove('is-online', 'is-offline');
        statusChip.classList.add(running ? 'is-online' : 'is-offline');
      }
      updateStartStopButton(card.querySelector('[data-action="toggle"]'), running);

      const healthChip = card.querySelector('.summary-health-chip');
      if (healthChip) {
        const healthLabel = (metrics.health_label || metrics.health_text || '').toString().trim();
        const healthDetail = (metrics.health_detail || metrics.health_text || '').toString().trim();
        const code = (metrics.health_code || '').toString().toLowerCase();
        const running = !!metrics.running;
        const displayHealth = healthLabel || (running ? 'Healthy' : 'Offline');
        healthChip.textContent = displayHealth;
        if (healthDetail) {
          healthChip.title = healthDetail;
        } else if (displayHealth) {
          healthChip.title = displayHealth;
        } else {
          healthChip.removeAttribute('title');
        }
        healthChip.classList.remove('health-ok', 'health-warn', 'health-bad');
        const lower = displayHealth.toLowerCase();
        const okCodes = new Set(['healthy', 'steady', 'mining']);
        const warnCodes = new Set(['syncing', 'downloading', 'initializing', 'no_peers']);
        const badCodes = new Set(['offline', 'stalled', 'error']);
        if (okCodes.has(code) || lower.includes('healthy') || lower.includes('mining')) {
          healthChip.classList.add('health-ok');
        } else if (badCodes.has(code) || lower.includes('stall') || (!running && lower)) {
          healthChip.classList.add('health-bad');
        } else if (warnCodes.has(code) || lower.includes('sync') || lower.includes('download')) {
          healthChip.classList.add('health-warn');
        }
      }

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
