(() => {
  const state = {
    nodes: new Map(), // id -> { card, meta }
    charts: new Map(), // id -> Chart instance
    chartViews: new Map(), // id -> active chart view
    paused: new Set(),
    lastRates: new Map(), // id -> last non-null sync rate
    lastProgress: new Map(), // id -> last non-null sync progress
    nodeLogs: new Map(), // id -> { lines, ts, loading, error }
    logPollTimers: new Map(),
    lastMetricsTs: 0,
    settings: {},
    settingsDirty: false,
    snapshots: {
      items: [],
      locations: [],
      dir: '',
      job: null,
    },
    snapshotsLoaded: false,
    snapshotStatus: { text: '', level: '', manual: false },
  };

  const cardsContainer = document.getElementById('fleetCards');
  const emptyStateCard = document.getElementById('emptyFleetState');
  const cardTemplate = document.getElementById('nodeCardTemplate');
  const summaryBadge = document.getElementById('globalSummaryBadge');
  const summaryDynamicTitle = document.getElementById('summaryDynamicTitle');
  const summaryDynamicDesc = document.getElementById('summaryDynamicDesc');

  const summaryTabButtons = Array.from(document.querySelectorAll('[data-summary-tab]'));
  const summaryPanes = Array.from(document.querySelectorAll('[data-summary-pane]'));
  const summaryActions = document.querySelector('[data-summary-view="stats"]');
  const settingsForm = document.getElementById('settingsForm');
  const saveSettingsBtn = document.getElementById('btnSaveSettings');
  const settingsStatus = document.getElementById('settingsStatus');
  const snapshotList = document.getElementById('snapshotList');
  const snapshotStatus = document.getElementById('snapshotStatus');
  const snapshotEmptyState = document.getElementById('snapshotEmptyState');
  const snapshotRefreshBtn = document.getElementById('btnRefreshSnapshots');
  const snapshotScanBtn = document.getElementById('btnScanSnapshots');
  const walletAddressValue = document.getElementById('walletAddressValue');
  const walletBalanceValue = document.getElementById('walletBalanceValue');
  const walletUpdatedValue = document.getElementById('walletUpdatedValue');
  const walletHistoryList = document.getElementById('walletHistoryList');
  const walletHistoryEmpty = document.getElementById('walletHistoryEmpty');
  const walletPane = document.getElementById('walletPane');
  const autoRestartToggle = document.getElementById('settingAutoRestart');
  const autoRestartHoursInput = document.getElementById('settingAutoRestartHours');
  const autoRestartHoursControl = document.querySelector('[data-tooltip-target="auto-restart-hours"]');
  let settingsStatusTimer = null;
  let snapshotPollTimer = null;
  const defaultSettings = {
    liveness_auto_recover: false,
    auto_restart_on_error: false,
    auto_restart_hours: 0,
    display_wallet_balance: false,
    snapshot_max: 0,
  };

  state.settings = { ...defaultSettings };
  state.walletHistory = [];
  state.lastWalletSnapshot = null;

  const fmt = new Intl.NumberFormat();
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const fmtDateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  const fmtShortDateTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  function numberOrZero(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : 0;
  }

  function recentWindow(series, fallback) {
    const values = Array.isArray(series) && series.length ? series.map(numberOrZero) : [numberOrZero(fallback)];
    return values.length > 5 ? values.slice(-5) : values;
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
    const scaled = value / 1024 ** index;
    const precision = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
    return `${scaled.toFixed(precision)} ${units[index]}`;
  }

  function formatPercent(value) {
    if (!Number.isFinite(value)) return null;
    if (value >= 99.95) return '100%';
    if (value >= 10) return `${value.toFixed(1)}%`;
    return `${value.toFixed(2)}%`;
  }

  function formatDurationShort(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return null;
    const total = Math.round(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    const parts = [];
    if (hours) parts.push(`${hours}h`);
    if (hours || minutes) parts.push(`${minutes}m`);
    parts.push(`${secs}s`);
    return parts.join(' ');
  }

  function formatSnapshotDate(value) {
    if (!value) return '—';
    try {
      const date = typeof value === 'string' ? new Date(value) : value;
      if (Number.isNaN(Number(date))) return '—';
      return fmtDateTime.format(date);
    } catch (_) {
      return String(value);
    }
  }

  function updateAutoRestartCooldownState(settings = state.settings) {
    if (!autoRestartHoursInput) return;
    const enabled = Boolean(settings?.auto_restart_on_error);
    autoRestartHoursInput.disabled = enabled;
  }

  const defaultChartView = 'height';
  const LOG_REFRESH_COOLDOWN_MS = 5000;
  const LOG_AUTO_REFRESH_MS = Math.max(6000, Math.floor(LOG_REFRESH_COOLDOWN_MS * 1.5));

  function formatChartLabels(rawLabels) {
    if (!Array.isArray(rawLabels)) return [];
    return rawLabels.map((stamp) => {
      try {
        return fmtTime.format(new Date(stamp));
      } catch (_) {
        return String(stamp);
      }
    });
  }

  function normalizeSeries(series, length) {
    const target = Number.isFinite(length) && length >= 0
      ? length
      : Array.isArray(series) ? series.length : 0;
    const values = Array.isArray(series) ? series : [];
    const output = [];
    for (let idx = 0; idx < target; idx += 1) {
      const raw = values[idx];
      const num = Number(raw);
      output.push(Number.isFinite(num) ? num : null);
    }
    return output;
  }

  function hasUsableValues(series) {
    return Array.isArray(series) && series.some((value) => Number.isFinite(value));
  }

  function computeDeltaSeries(metrics, length) {
    const localSeries = Array.isArray(metrics.local) ? metrics.local : [];
    const remoteSeries = Array.isArray(metrics.remote) ? metrics.remote : [];
    const targetLength = Number.isFinite(length) && length >= 0
      ? length
      : Math.max(localSeries.length, remoteSeries.length);
    const output = [];
    for (let idx = 0; idx < targetLength; idx += 1) {
      const localVal = Number(localSeries[idx]);
      const remoteVal = Number(remoteSeries[idx]);
      if (!Number.isFinite(localVal) || !Number.isFinite(remoteVal)) {
        output.push(null);
        continue;
      }
      const delta = remoteVal - localVal;
      output.push(Number.isFinite(delta) ? delta : null);
    }
    return output;
  }

  function formatPercentValue(value) {
    if (!Number.isFinite(value)) return '—';
    if (value >= 99.95) return '100%';
    if (value <= 0) return '0%';
    if (value >= 10) return `${value.toFixed(1)}%`;
    return `${value.toFixed(2)}%`;
  }

  function formatPercentTick(value) {
    if (!Number.isFinite(value)) return '';
    if (value >= 100) return '100%';
    if (value <= 0) return '0%';
    if (value >= 10) return `${Math.round(value)}%`;
    return `${value.toFixed(1)}%`;
  }

  function formatBlocksValue(value) {
    if (!Number.isFinite(value)) return '—';
    const safe = Math.max(value, 0);
    return `${fmt.format(safe)} blocks`;
  }

  function formatBlocksTick(value) {
    if (!Number.isFinite(value)) return '';
    return fmt.format(Math.max(value, 0));
  }

  function formatPeersValue(value) {
    if (!Number.isFinite(value)) return '—';
    return `${fmt.format(Math.max(value, 0))} peers`;
  }

  function formatLatencyValue(value) {
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
    if (value >= 100) return `${value.toFixed(0)} ms`;
    return `${value.toFixed(0)} ms`;
  }

  function formatLatencyTick(value) {
    if (!Number.isFinite(value) || value < 0) return '';
    if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
    return `${Math.round(value)}ms`;
  }

  function formatRateValue(value) {
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value >= 10) return `${value.toFixed(1)} blk/s`;
    if (value >= 1) return `${value.toFixed(2)} blk/s`;
    return `${value.toFixed(3)} blk/s`;
  }

  function formatRateTick(value) {
    if (!Number.isFinite(value) || value < 0) return '';
    if (value >= 10) return `${value.toFixed(0)}`;
    if (value >= 1) return `${value.toFixed(1)}`;
    return `${value.toFixed(2)}`;
  }

  const chartViewConfigs = {
    height: {
      label: 'Height Δ',
      color: '#ffb74d',
      background: 'rgba(255,183,77,0.2)',
      data: (metrics, length) => normalizeSeries(computeDeltaSeries(metrics, length), length),
      tooltip: formatBlocksValue,
      tick: formatBlocksTick,
    },
    sync: {
      label: 'Sync activity',
      color: '#44f2a8',
      background: 'rgba(68,242,168,0.18)',
      data: (metrics, length) => normalizeSeries(metrics.sync_progress_series, length),
      tooltip: formatPercentValue,
      tick: formatPercentTick,
      fallback: {
        label: 'Height Δ',
        color: '#ffb74d',
        background: 'rgba(255,183,77,0.2)',
        data: (metrics, length) => normalizeSeries(computeDeltaSeries(metrics, length), length),
        tooltip: formatBlocksValue,
        tick: formatBlocksTick,
      },
    },
    peers: {
      label: 'Peers',
      color: '#64b5f6',
      background: 'rgba(100,181,246,0.22)',
      data: (metrics, length) => normalizeSeries(metrics.peers_series, length),
      tooltip: formatPeersValue,
      tick: formatBlocksTick,
    },
    rpc: {
      label: 'RPC latency',
      color: '#81d4fa',
      background: 'rgba(129,212,250,0.25)',
      data: (metrics, length) => normalizeSeries(metrics.rpc_latency_series, length),
      tooltip: formatLatencyValue,
      tick: formatLatencyTick,
    },
    block: {
      label: 'Block activity',
      color: '#ce93d8',
      background: 'rgba(206,147,216,0.25)',
      data: (metrics, length) => normalizeSeries(metrics.block_rate_series, length),
      tooltip: formatRateValue,
      tick: formatRateTick,
    },
  };

  function materializeChartConfig(metrics, config, length, depth = 0) {
    const fallbackDepth = Number(depth) || 0;
    if (!config) {
      return {
        config: chartViewConfigs.height,
        data: normalizeSeries(computeDeltaSeries(metrics, length), length),
      };
    }
    const values = typeof config.data === 'function'
      ? config.data(metrics, length)
      : normalizeSeries([], length);
    const usable = hasUsableValues(values);
    if (usable || !config.fallback || fallbackDepth > 3) {
      return { config, data: values };
    }
    return materializeChartConfig(metrics, config.fallback, length, fallbackDepth + 1);
  }

  function resolveChartDataset(metrics, viewKey) {
    const labelsRaw = Array.isArray(metrics.labels) ? metrics.labels : [];
    const length = labelsRaw.length;
    const baseConfig = chartViewConfigs[viewKey] || chartViewConfigs[defaultChartView];
    const { config, data } = materializeChartConfig(metrics, baseConfig, length);
    return {
      labels: formatChartLabels(labelsRaw),
      data,
      config,
    };
  }

  function setBusy(btn, busy, text) {
    if (!btn) return;
    if (busy) {
      if (!btn.dataset.originalHtml) {
        btn.dataset.originalHtml = btn.innerHTML;
      }
      if (text) {
        btn.textContent = text;
      }
      btn.disabled = true;
      btn.dataset.busy = '1';
    } else {
      if (btn.dataset.originalHtml !== undefined) {
        btn.innerHTML = btn.dataset.originalHtml;
        delete btn.dataset.originalHtml;
      }
      btn.disabled = false;
      delete btn.dataset.busy;
    }
  }

  function scheduleSnapshotPoll(active) {
    if (active) {
      if (snapshotPollTimer) return;
      snapshotPollTimer = window.setInterval(() => {
        void loadSnapshots({ silent: true });
      }, 5000);
    } else if (snapshotPollTimer) {
      clearInterval(snapshotPollTimer);
      snapshotPollTimer = null;
    }
  }

  function setSnapshotStatus(message, options = {}) {
    const level = options.level || '';
    const manual = options.manual === true && message;
    state.snapshotStatus = { text: message || '', level, manual: Boolean(manual) };
    if (!snapshotStatus) return;
    snapshotStatus.classList.remove('is-ok', 'is-warn', 'is-error');
    if (!message) {
      snapshotStatus.textContent = '';
      return;
    }
    snapshotStatus.textContent = message;
    if (level) {
      snapshotStatus.classList.add(`is-${level}`);
    }
    if (level === 'ok') {
      window.clearTimeout(setSnapshotStatus._timer);
      setSnapshotStatus._timer = window.setTimeout(() => {
        setSnapshotStatus('');
      }, 10000);
    }
  }

  function formatWalletTimestamp(ms) {
    if (!Number.isFinite(ms)) {
      return '—';
    }
    try {
      return fmtDateTime.format(new Date(ms));
    } catch (err) {
      return '—';
    }
  }

  function updateWalletPane(wallet, { enabled = false, timestamp = Date.now() } = {}) {
    if (!walletPane) return;
    const hasWallet = enabled && wallet && wallet.address;
    if (walletPane) {
      walletPane.classList.toggle('is-disabled', !enabled);
    }
    const displayAddress = hasWallet ? wallet.address : '—';
    const displayBalance = hasWallet
      ? wallet.balance_formatted || wallet.short || (wallet.balance_bdag ? `${wallet.balance_bdag} BDAG` : '—')
      : '—';
    const updatedMs = hasWallet && Number.isFinite(timestamp) ? timestamp * 1000 : NaN;
    const updatedText = hasWallet ? formatWalletTimestamp(updatedMs) : '—';
    if (walletAddressValue) walletAddressValue.textContent = displayAddress;
    if (walletBalanceValue) walletBalanceValue.textContent = displayBalance;
    if (walletUpdatedValue) walletUpdatedValue.textContent = updatedText;

    if (walletHistoryList && walletHistoryEmpty) {
      if (wallet && Array.isArray(wallet.history) && wallet.history.length) {
        state.walletHistory = wallet.history
          .map((entry) => ({
            hash: entry.hash || entry.txid || 'unknown',
            amount: entry.amount,
            direction: entry.direction || 'in',
            timestamp: entry.timestamp ? entry.timestamp * 1000 : Date.now(),
          }))
          .slice(0, 25);
        walletHistoryEmpty.hidden = true;
        walletHistoryList.hidden = false;
        walletHistoryList.innerHTML = state.walletHistory
          .map((entry) => {
            const timeText = fmtShortDateTime.format(new Date(entry.timestamp));
            const direction = entry.direction === 'out' ? 'Sent' : 'Received';
            const amount = Number.isFinite(entry.amount)
              ? `${entry.amount} BDAG`
              : entry.amount || '—';
            const hash = entry.hash || '—';
            const shortHash = hash.length > 18 ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : hash;
            return `<li><span class="balance">${direction} ${amount}</span><span class="time">${timeText} · ${shortHash}</span></li>`;
          })
          .join('');
      } else {
        state.walletHistory = [];
        walletHistoryList.hidden = true;
        walletHistoryEmpty.hidden = false;
        walletHistoryEmpty.textContent = enabled
          ? 'No wallet transactions captured yet.'
          : 'Wallet monitoring disabled in settings.';
      }
    }

    state.lastWalletSnapshot = {
      wallet: wallet ? JSON.parse(JSON.stringify(wallet)) : null,
      enabled,
      timestamp,
    };
  }

  function updateSnapshotButtons() {
    const job = (state.snapshots && state.snapshots.job) || null;
    const jobActive = !!(job && job.active);
    const jobDetails = (job && job.details) || {};
    const jobNode = jobDetails && jobDetails.node;
    state.nodes.forEach((entry) => {
      if (!entry || !entry.card || !entry.meta) return;
      const btn = entry.card.querySelector('[data-action="node-snapshot"]');
      const restoreBtn = entry.card.querySelector('[data-action="node-restore"]');
      if (!btn) return;
      const nodeId = entry.meta.id;
      const label = entry.meta.label || nodeId || 'node';
      let title = `Create snapshot for ${label}`;
      let disabled = false;
      if (btn.classList.contains('is-busy')) {
        btn.classList.remove('is-busy');
      }
      if (restoreBtn && restoreBtn.classList.contains('is-busy')) {
        restoreBtn.classList.remove('is-busy');
      }
      if (btn.dataset.busy === '1') {
        disabled = true;
      }
      if (state.snapshots && state.snapshots.job && state.snapshots.job.active) {
        disabled = true;
        if (jobNode && jobNode === nodeId) {
          const progress = job && job.progress ? job.progress : {};
          const pctText = formatPercent(progress.pct);
                    const etaText = formatDurationShort(progress.eta_seconds);
          const parts = [];
          if (pctText) parts.push(pctText);
          if (etaText) parts.push(`ETA ${etaText}`);
          const progressText = parts.length ? ` (${parts.join(' • ')})` : '';
          const mode = jobDetails.mode || 'snapshot';
          const verb = mode === 'restore' ? 'Restore' : 'Snapshot';
          title = `${verb} running for ${label}${progressText}`;
          if (mode === 'restore' && restoreBtn) {
            restoreBtn.classList.add('is-busy');
          } else {
            btn.classList.add('is-busy');
          }
        } else {
          title = jobDetails.mode === 'restore' ? 'Restore in progress' : 'Snapshot in progress';
          btn.classList.remove('is-busy');
        }
      } else {
        btn.classList.remove('is-busy');
      }
      btn.disabled = disabled;
      btn.title = title;
      btn.setAttribute('aria-label', title);
      if (restoreBtn) {
        restoreBtn.disabled = !!jobActive;
        if (!jobActive) {
          restoreBtn.classList.remove('is-busy');
        }
      }

      const toggleBtn = entry.card.querySelector('[data-role="toggle"]');
      if (toggleBtn) {
        const snapshotLock = jobActive && jobNode && jobNode === nodeId;
        if (snapshotLock) {
          toggleBtn.dataset.snapshotLock = '1';
          toggleBtn.disabled = true;
        } else if (toggleBtn.dataset.snapshotLock === '1') {
          delete toggleBtn.dataset.snapshotLock;
          if (!toggleBtn.dataset.manualDisable) {
            toggleBtn.disabled = false;
          }
        }
      }
    });
  }

  function renderSnapshots() {
    const snapshotsState = state.snapshots || {};
    const items = Array.isArray(snapshotsState.items) ? snapshotsState.items : [];
    const dir = snapshotsState.dir || '';
    const job = snapshotsState.job || null;
    const locations = Array.isArray(snapshotsState.locations) ? snapshotsState.locations : [];
    const hasSnapshots = items.length > 0;
    if (!snapshotList) {
      return;
    }
    snapshotList.innerHTML = '';
    if (!items.length) {
      if (snapshotEmptyState) {
        snapshotEmptyState.hidden = false;
        snapshotList.appendChild(snapshotEmptyState);
      }
    } else {
      if (snapshotEmptyState) {
        snapshotEmptyState.hidden = true;
      }
      items.slice(0, 6).forEach((item) => {
        if (!item || !item.name) return;
        const tile = document.createElement('div');
        tile.className = 'snapshot-tile';

        const nameEl = document.createElement('span');
        nameEl.className = 'snapshot-tile__name';
        nameEl.textContent = item.name;
        tile.appendChild(nameEl);

        const metaEl = document.createElement('span');
        metaEl.className = 'snapshot-tile__meta';
        const metaParts = [];
        if (item.modified) {
          metaParts.push(formatSnapshotDate(item.modified));
        }
        if (Number.isFinite(Number(item.size))) {
          metaParts.push(formatBytes(item.size));
        }
        if (item.name) {
          const match = item.name.match(/\.([0-9]+)\.tar$/);
          if (match && match[1]) {
            const heightNum = Number(match[1]);
            if (Number.isFinite(heightNum) && heightNum >= 0) {
              metaParts.push(`height ${heightNum}`);
            }
          }
        }
        metaEl.textContent = metaParts.length ? metaParts.join(' • ') : '—';
        tile.appendChild(metaEl);

        const actions = document.createElement('div');
        actions.className = 'snapshot-tile__actions';
        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'snapshot-tile__delete';
        deleteBtn.dataset.snapshotAction = 'delete';
        deleteBtn.dataset.snapshotName = item.name;
        deleteBtn.textContent = 'Delete';
        if (job && job.active) {
          deleteBtn.disabled = true;
        }
        actions.appendChild(deleteBtn);
        tile.appendChild(actions);

        snapshotList.appendChild(tile);
      });
    }

    const jobDetails = (job && job.details) || {};
    const jobActive = job && job.active;
    const manualOverride = Boolean(state.snapshotStatus && state.snapshotStatus.manual);

    if (jobActive) {
      const progress = job.progress || {};
      const pctText = formatPercent(progress.pct);
      const speedText =
        Number.isFinite(progress.speed_bytes) && progress.speed_bytes > 0
          ? `${formatBytes(progress.speed_bytes)}/s`
          : null;
      const etaText = formatDurationShort(progress.eta_seconds);
      const pieces = [];
      if (pctText) pieces.push(`${pctText} complete`);
      if (speedText) pieces.push(`Read speed ${speedText}`);
      if (etaText) pieces.push(`ETA ${etaText}`);
      const label = jobDetails.label || jobDetails.node || '';
      const mode = jobDetails.mode || 'snapshot';
      const baseMessage = mode === 'restore' ? 'Restore job running…' : 'Snapshot job running…';
      let message = job.message || baseMessage;
      if (label && !message.toLowerCase().includes(label.toLowerCase())) {
        message = mode === 'restore' ? `Restore running for ${label}` : `Snapshot running for ${label}`;
      }
      if (pieces.length) {
        const suffix = pieces.join(' • ');
        message = message.endsWith('…') ? `${message} ${suffix}` : `${message} — ${suffix}`;
      }
      setSnapshotStatus(message, { level: 'warn' });
      scheduleSnapshotPoll(true);
    } else if (job && job.status && job.message && !manualOverride) {
      const levelMap = { completed: 'ok', error: 'error', cancelled: 'warn' };
      const level = levelMap[job.status] || '';
      setSnapshotStatus(job.message, { level });
      scheduleSnapshotPoll(false);
    } else if (state.snapshotStatus && state.snapshotStatus.text) {
      setSnapshotStatus(state.snapshotStatus.text, {
        level: state.snapshotStatus.level,
        manual: manualOverride,
      });
      scheduleSnapshotPoll(false);
    } else {
      setSnapshotStatus('');
      scheduleSnapshotPoll(false);
    }

    if (snapshotRefreshBtn && !snapshotRefreshBtn.dataset.busy) {
      snapshotRefreshBtn.disabled = !!jobActive;
    }
    if (snapshotScanBtn && !snapshotScanBtn.dataset.busy) {
      snapshotScanBtn.disabled = !!jobActive;
    }

    if (snapshotList) {
      snapshotList.querySelectorAll('[data-snapshot-action="delete"]').forEach((btn) => {
        btn.disabled = !!jobActive;
      });
    }

    updateSnapshotButtons();
  }

  async function loadSnapshots(options = {}) {
    const { silent = false, preserveStatus = false } = options;
    if (!silent) {
      setSnapshotStatus('Loading snapshots…', { level: 'warn' });
    }
    try {
      const res = await fetch('/api/snapshots', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      state.snapshots = {
        items: Array.isArray(payload.snapshots) ? payload.snapshots : [],
        locations: Array.isArray(payload.locations) ? payload.locations : [],
        dir: payload.directory || '',
        job: payload.job || null,
      };
      if (!preserveStatus && payload.status && payload.status.text) {
        state.snapshotStatus = {
          text: payload.status.text,
          level: payload.status.level || '',
          manual: false,
        };
      } else if (!silent && !preserveStatus) {
        state.snapshotStatus = { text: 'Snapshots refreshed.', level: 'ok', manual: false };
      }
      state.snapshotsLoaded = true;
      renderSnapshots();
      if (!silent && state.snapshotStatus.text) {
        setSnapshotStatus(state.snapshotStatus.text, { level: state.snapshotStatus.level });
      }
    } catch (err) {
      const message = err && err.message ? err.message : 'Failed to load snapshots';
      if (!silent) {
        setSnapshotStatus(message, { level: 'error' });
      }
    }
  }

  async function createNodeSnapshot(nodeId, btn) {
    if (!nodeId) return;
    const activeJob = state.snapshots && state.snapshots.job && state.snapshots.job.active;
    if (activeJob) {
      setSnapshotStatus('A snapshot job is already in progress.', { level: 'warn' });
      updateSnapshotButtons();
      return;
    }
    if (btn) {
      setBusy(btn, true);
    }
    try {
      const entry = state.nodes.get(nodeId);
      const label = entry && entry.meta ? (entry.meta.label || entry.meta.id || nodeId) : nodeId;
      setSnapshotStatus(`Starting snapshot for ${label}…`, { level: 'warn' });
      const res = await fetch('/api/snapshots/create', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ node: nodeId }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        throw new Error(payload && payload.error ? payload.error : `HTTP ${res.status}`);
      }
      if (payload.job) {
        state.snapshots.job = payload.job;
      }
      const message = payload.message || `Snapshot started for ${label}`;
      setSnapshotStatus(message, { level: 'warn' });
      await loadSnapshots({ silent: true });
    } catch (err) {
      setSnapshotStatus(err && err.message ? err.message : 'Failed to start snapshot', { level: 'error' });
    } finally {
      if (btn) {
        setBusy(btn, false);
      }
      updateSnapshotButtons();
    }
  }

  async function scanSnapshots() {
    if (!snapshotScanBtn || snapshotScanBtn.dataset.busy) return;
    setSnapshotStatus('Scanning snapshot locations…', { level: 'warn' });
    setBusy(snapshotScanBtn, true, 'Scanning…');
    try {
      const res = await fetch('/api/snapshots/scan', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({}),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        throw new Error(payload && payload.error ? payload.error : `HTTP ${res.status}`);
      }
      const message = payload.message || 'Snapshot locations updated.';
      setSnapshotStatus(message, { level: 'ok' });
      await loadSnapshots({ silent: true });
      setSnapshotStatus(message, { level: 'ok' });
    } catch (err) {
      setSnapshotStatus(err && err.message ? err.message : 'Snapshot scan failed', { level: 'error' });
    } finally {
      setBusy(snapshotScanBtn, false);
    }
  }

  async function refreshSnapshots() {
    await loadSnapshots();
  }

  async function restoreNodeSnapshot(nodeId, btn) {
    if (!nodeId) return;
    const activeJob = state.snapshots && state.snapshots.job && state.snapshots.job.active;
    if (activeJob) {
      setSnapshotStatus('A snapshot job is already in progress.', { level: 'warn' });
      updateSnapshotButtons();
      return;
    }
    if (btn && btn.dataset.busy) return;
    if (btn) {
      setBusy(btn, true, 'Restoring…');
    }
    try {
      const entry = state.nodes.get(nodeId);
      const label = entry && entry.meta ? (entry.meta.label || entry.meta.id || nodeId) : nodeId;
      setSnapshotStatus(`Restoring snapshot for ${label}…`, { level: 'warn' });
      const res = await fetch('/api/snapshots/restore', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ node: nodeId }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        throw new Error(payload && payload.error ? payload.error : `HTTP ${res.status}`);
      }
      if (payload.job) {
        state.snapshots.job = payload.job;
      }
      const message = payload.message || `Snapshot restore started for ${label}`;
      setSnapshotStatus(message, { level: 'warn' });
      await loadSnapshots({ silent: true });
    } catch (err) {
      setSnapshotStatus(err && err.message ? err.message : 'Failed to start restore', { level: 'error' });
    } finally {
      if (btn) {
        setBusy(btn, false);
      }
      updateSnapshotButtons();
    }
  }

  async function deleteSnapshot(name, btn) {
    if (!name) return;
    if (btn && btn.dataset.busy) return;
    setSnapshotStatus(`Deleting ${name}…`, { level: 'warn' });
    setBusy(btn, true, 'Deleting…');
    try {
      const res = await fetch('/api/snapshots/delete', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        throw new Error(payload && payload.error ? payload.error : `HTTP ${res.status}`);
      }
      const message = payload.message || `Deleted ${name}`;
      setSnapshotStatus(message, { level: 'ok', manual: true });
      await loadSnapshots({ silent: true, preserveStatus: true });
    } catch (err) {
      setSnapshotStatus(err && err.message ? err.message : `Failed to delete ${name}`, { level: 'error' });
    } finally {
      setBusy(btn, false);
    }
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

  function renderSyncChips(chips, options = {}) {
    if (!chips) return;
    const { progressChip, rateChip, etaChip } = chips;
    const {
      progress,
      rate,
      etaInfo,
      meta = {},
    } = options;
    const local = meta.local_height ?? meta.local;
    const remote = meta.remote_height ?? meta.remote;
    if (progressChip) {
      const hasProgress = Number.isFinite(progress);
      const progressValue = hasProgress ? progress : null;
      const progressText = hasProgress ? `${progressValue.toFixed(1)}%` : '—';
      progressChip.textContent = `Synced ${progressText}`;
      progressChip.classList.remove('is-ok', 'is-warn', 'is-danger');
      if (hasProgress) {
        let progressVariant = 'danger';
        if (progressValue >= 99.9) {
          progressVariant = 'ok';
        } else if (progressValue >= 70) {
          progressVariant = 'warn';
        }
        progressChip.classList.add(`is-${progressVariant}`);
      }
      const details = [];
      if (hasProgress && Number.isFinite(local) && Number.isFinite(remote)) {
        details.push(`Local ${fmt.format(local)} of ${fmt.format(remote)}`);
      }
      if (details.length) {
        progressChip.title = details.join(' • ');
      } else {
        progressChip.removeAttribute('title');
      }
    }
    if (rateChip) {
      const hasRate = Number.isFinite(rate) && rate > 0;
      const rateValue = hasRate ? (rate >= 10 ? rate.toFixed(1) : rate.toFixed(2)) : '—';
      rateChip.textContent = `Rate ${rateValue} blk/s`;
      if (hasRate) {
        rateChip.title = `${rateValue} blocks per second`;
      } else {
        rateChip.removeAttribute('title');
      }
    }
    if (etaChip) {
      etaChip.classList.remove('is-ok', 'is-warn', 'is-danger');
      if (etaInfo && etaInfo.text) {
        etaChip.textContent = etaInfo.text;
        if (etaInfo.variant) {
          etaChip.classList.add(`is-${etaInfo.variant}`);
        }
        etaChip.title = etaInfo.hint || etaInfo.text;
      } else {
        etaChip.textContent = 'ETA —';
        etaChip.removeAttribute('title');
      }
    }
  }

function resolveHealth(stats, running, options = {}) {
  const { forceOffline = false } = options;
  const detail = (stats.health_detail || stats.health_text || '').toString().trim();
  let display = 'Offline';
  let code = 'offline';
  if (running) {
    if (forceOffline) {
      display = 'Stalled';
      code = 'warn';
    } else {
      display = 'Online';
      code = 'online';
    }
  }
  return { display, detail, code };
}


function switchSummaryTab(target) {
  const view = target && target.dataset ? target.dataset.summaryTab : null;
  const activeView = view || 'stats';
  summaryTabButtons.forEach((button) => {
    const isActive = button === target || button.dataset.summaryTab === activeView;
    button.classList.toggle('is-active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  summaryPanes.forEach((pane) => {
    const paneView = pane.dataset.summaryPane;
    pane.hidden = paneView !== activeView;
  });
  if (summaryActions) {
    const hide = activeView !== 'stats';
    summaryActions.hidden = hide;
    summaryActions.setAttribute('aria-hidden', hide ? 'true' : 'false');
  }
  if (summaryDynamicTitle && summaryDynamicDesc) {
    const copy = {
      stats: {
        title: 'Global Stats',
        desc: 'Real-time snapshot of every node discovered on the local network.',
      },
      settings: {
        title: 'Settings',
        desc: 'Configure automatic recovery and display preferences for the fleet.',
      },
      snapshots: {
        title: 'Snapshots',
        desc: 'Latest archived snapshots for quick recovery.',
      },
      wallet: {
        title: 'Wallet',
        desc: 'Wallet address, balance, and recent history collected from node snapshots.',
      },
    };
    const next = copy[activeView] || copy.stats;
    summaryDynamicTitle.textContent = next.title;
    summaryDynamicDesc.textContent = next.desc;
  }
  if (activeView === 'wallet' && state.lastWalletSnapshot) {
    updateWalletPane(state.lastWalletSnapshot.wallet, state.lastWalletSnapshot);
  }
  if (activeView === 'snapshots' && !state.snapshotsLoaded) {
    void loadSnapshots();
  }
}

function updateSettingsStatus(message, options = {}) {
  if (!settingsStatus) return;
  if (settingsStatusTimer) {
    clearTimeout(settingsStatusTimer);
    settingsStatusTimer = null;
  }
  settingsStatus.textContent = message || '';
  settingsStatus.classList.remove('is-error', 'is-success');
  if (options.error) {
    settingsStatus.classList.add('is-error');
  } else if (options.success) {
    settingsStatus.classList.add('is-success');
    if (message) {
      settingsStatusTimer = window.setTimeout(() => {
        settingsStatus.textContent = '';
        settingsStatus.classList.remove('is-success');
        settingsStatusTimer = null;
      }, options.duration || 3000);
    }
  }
}

function applySettingsToForm(settings = {}) {
  const merged = { ...defaultSettings, ...settings };
  state.settings = { ...merged };
  state.settingsDirty = false;
  if (settingsForm) {
    const inputs = settingsForm.querySelectorAll('[data-setting-key]');
    inputs.forEach((input) => {
      const key = input.dataset.settingKey;
      if (!key) return;
      const type = input.dataset.settingType || input.type;
      if (type === 'number') {
        const raw = merged[key];
        const value = Number(raw);
        const safeValue = Number.isFinite(value) && value >= 0 ? value : Number(defaultSettings[key] || 0);
        input.value = safeValue;
        state.settings[key] = safeValue;
      } else if (type === 'text') {
        const raw = merged[key];
        const value = typeof raw === 'string' ? raw : raw == null ? '' : String(raw);
        input.value = value;
        state.settings[key] = value;
      } else {
        const checked = !!merged[key];
        input.checked = checked;
        state.settings[key] = checked;
      }
    });
    updateAutoRestartCooldownState(merged);
  }
  if (saveSettingsBtn) {
    saveSettingsBtn.disabled = true;
  }
  updateSettingsStatus('');
}

function markSettingsDirty() {
  state.settingsDirty = true;
  if (saveSettingsBtn) {
    saveSettingsBtn.disabled = false;
  }
  updateSettingsStatus('Unsaved changes');
}

async function loadSettings() {
  if (!settingsForm) {
    return;
  }
  try {
    const res = await fetch('/api/settings', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    applySettingsToForm(payload.settings || {});
  } catch (err) {
    console.error('[settings] failed to load', err);
    if (!Object.keys(state.settings || {}).length) {
      applySettingsToForm(defaultSettings);
    }
    updateSettingsStatus('Failed to load settings', { error: true });
  }
}

async function saveSettings() {
  if (!settingsForm || !state.settingsDirty) {
    return;
  }
  try {
    if (saveSettingsBtn) {
      saveSettingsBtn.disabled = true;
    }
    updateSettingsStatus('Saving…');
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(state.settings),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    applySettingsToForm(payload.settings || state.settings);
    updateSettingsStatus('Settings saved', { success: true });
    await loadNodes();
  } catch (err) {
    console.error('[settings] failed to save', err);
    updateSettingsStatus(err.message || 'Failed to save settings', { error: true });
    if (saveSettingsBtn) {
      saveSettingsBtn.disabled = false;
    }
  }
}

  async function loadNodes() {
    try {
      const res = await fetch('/api/node-manager/nodes', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      const nodes = Array.isArray(payload.nodes) ? payload.nodes : [];
      const stalled = nodes.reduce((count, node) => {
        if (!node || !node.id) return count;
        const stats = node.status || {};
        const rawRunning = isRunningFlag(stats.running);
        const forced = shouldForceOffline(stats, rawRunning, state.lastProgress.get(node.id));
        return forced ? count + 1 : count;
      }, 0);
      const summary = { ...(payload.summary || {}), stalled };
      renderSummary(summary);
      syncCards(nodes);
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
    const stalledEl = document.getElementById('statStalled');
    const maxLocalEl = document.getElementById('statMaxLocal');
    const maxRemoteEl = document.getElementById('statMaxRemote');

    if (!state.settingsDirty && summary && summary.settings) {
      const incoming = summary.settings || {};
      const differs = Object.keys(defaultSettings).some((key) => {
        if (typeof defaultSettings[key] === 'number') {
          const currentValue = Number(state.settings[key] ?? defaultSettings[key] ?? 0);
          const incomingValue = Number(incoming[key] ?? defaultSettings[key] ?? 0);
          return Number.isFinite(currentValue) && Number.isFinite(incomingValue)
            ? currentValue !== incomingValue
            : false;
        }
        return !!state.settings[key] !== !!incoming[key];
      });
      if (differs) {
        applySettingsToForm(incoming);
      }
    }

    if (!summary || Object.keys(summary).length === 0) {
      summaryBadge.textContent = '—';
      summaryBadge.removeAttribute('title');
      countEl.textContent = onlineEl.textContent = offlineEl.textContent = maxLocalEl.textContent = maxRemoteEl.textContent = '—';
      if (stalledEl) stalledEl.textContent = '—';
      return;
    }

    const count = summary.count ?? 0;
    const online = summary.running ?? 0;
    const offline = summary.offline ?? Math.max(count - online, 0);
    const stalled = summary.stalled ?? 0;

    countEl.textContent = fmt.format(count);
    onlineEl.textContent = fmt.format(online);
    offlineEl.textContent = fmt.format(offline);
    if (stalledEl) stalledEl.textContent = fmt.format(stalled);
    maxLocalEl.textContent = summary.max_local_height !== undefined ? fmt.format(summary.max_local_height) : '—';
    maxRemoteEl.textContent = summary.max_remote_height !== undefined ? fmt.format(summary.max_remote_height) : '—';

    const walletEnabled = !!summary.wallet_enabled;
    const wallet = walletEnabled ? summary.wallet || {} : null;
    const ts = summary.timestamp ? new Date(summary.timestamp * 1000) : null;
    updateWalletPane(wallet, { enabled: walletEnabled, timestamp: summary.timestamp });
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
      badgeText = 'updated';
    }
    if (!badgeText) {
      badgeText = 'updated';
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
        state.chartViews.delete(nodeId);
        state.paused.delete(nodeId);
        state.lastRates.delete(nodeId);
        state.lastProgress.delete(nodeId);
        state.nodeLogs.delete(nodeId);
        stopLogPolling(nodeId);
      }
    });
    updateSnapshotButtons();
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

    const toggleBtn = details.querySelector('[data-role="toggle"]');
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
    const snapshotBtn = details.querySelector('[data-action="node-snapshot"]');
    if (snapshotBtn) {
      const handler = async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await createNodeSnapshot(node.id, snapshotBtn);
      };
      snapshotBtn.addEventListener('click', handler);
      snapshotBtn.addEventListener('mousedown', (event) => event.stopPropagation());
      snapshotBtn.addEventListener('mouseup', (event) => event.stopPropagation());
      snapshotBtn.title = 'Create snapshot';
    }
    const restoreBtn = details.querySelector('[data-action="node-restore"]');
    if (restoreBtn) {
      const handler = async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await restoreNodeSnapshot(node.id, restoreBtn);
      };
      restoreBtn.addEventListener('click', handler);
      restoreBtn.addEventListener('mousedown', (event) => event.stopPropagation());
      restoreBtn.addEventListener('mouseup', (event) => event.stopPropagation());
      restoreBtn.title = 'Restore snapshot';
    }
    cardsContainer.appendChild(details);
    state.nodes.set(node.id, { card: details, meta: node });
    state.chartViews.set(node.id, defaultChartView);
    updateCardHeader(node);

    const canvas = details.querySelector('canvas');
    if (canvas && typeof Chart === 'function') {
      const chart = createChart(canvas.getContext('2d'));
      state.charts.set(node.id, chart);
    } else {
      console.warn('[fleet] chart unavailable; skipping chart init for', node.id);
    }

    const chartTabs = Array.from(details.querySelectorAll('[data-chart-tab]'));
    if (chartTabs.length) {
      chartTabs.forEach((button) => {
        button.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          selectChartTab(node.id, button.dataset.chartTab, details);
        });
        button.addEventListener('mousedown', (event) => event.stopPropagation());
        button.addEventListener('mouseup', (event) => event.stopPropagation());
        button.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            selectChartTab(node.id, button.dataset.chartTab, details);
          }
        });
      });
    }

    const logsToggle = details.querySelector('[data-role="logs-toggle"]');
    if (logsToggle) {
      logsToggle.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleNodeLogs(node.id, details);
      });
      logsToggle.addEventListener('mousedown', (event) => event.stopPropagation());
      logsToggle.addEventListener('mouseup', (event) => event.stopPropagation());
    }
    const logsRefresh = details.querySelector('[data-role="logs-refresh"]');
    if (logsRefresh) {
      logsRefresh.addEventListener('click', async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await loadNodeLogs(node.id, details, { force: true });
      });
      logsRefresh.addEventListener('mousedown', (event) => event.stopPropagation());
      logsRefresh.addEventListener('mouseup', (event) => event.stopPropagation());
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
    const orderingContext = (() => {
      const keys = Array.from(state.nodes.keys());
      const infoMap = new Map();
      const baseWithExplicit = new Set();
      keys.forEach((key) => {
        const text = String(key || '').trim();
        const match = text.match(/^(.*?)-(\d+)$/);
        if (match) {
          const base = match[1];
          const num = Number(match[2]);
          infoMap.set(key, {
            base,
            num,
            hasExplicit: Number.isFinite(num),
            raw: text,
          });
          if (Number.isFinite(num)) {
            baseWithExplicit.add(base);
          }
        } else {
          infoMap.set(key, {
            base: text,
            num: null,
            hasExplicit: false,
            raw: text,
          });
        }
      });
      const sortedKeys = keys.slice().sort((a, b) => {
        const infoA = infoMap.get(a) || { base: '', num: null, hasExplicit: false, raw: '' };
        const infoB = infoMap.get(b) || { base: '', num: null, hasExplicit: false, raw: '' };
        const aHasNum = infoA.hasExplicit || baseWithExplicit.has(infoA.base);
        const bHasNum = infoB.hasExplicit || baseWithExplicit.has(infoB.base);
        const aNum = infoA.hasExplicit ? infoA.num : (aHasNum ? 1 : Number.POSITIVE_INFINITY);
        const bNum = infoB.hasExplicit ? infoB.num : (bHasNum ? 1 : Number.POSITIVE_INFINITY);
        if (aHasNum && bHasNum && aNum !== bNum) {
          return aNum - bNum;
        }
        if (aHasNum !== bHasNum) {
          return aHasNum ? -1 : 1;
        }
        return infoA.raw.localeCompare(infoB.raw);
      });
      return { infoMap, baseWithExplicit, sortedKeys };
    })();
    const workerLabel = (() => {
      const idText = String(node.id || '').trim();
      const info = orderingContext.infoMap.get(node.id) || { base: idText, num: null, hasExplicit: false };
      const hasBaseNumbers = orderingContext.baseWithExplicit.has(info.base);
      const explicitNum = info.hasExplicit && Number.isFinite(info.num) ? info.num : null;
      const assignedNum = explicitNum ?? (hasBaseNumbers ? 1 : null);
      const index = orderingContext.sortedKeys.indexOf(node.id);
      const ordinal = index >= 0 ? index + 1 : state.nodes.size + 1;
      const finalNumber = assignedNum ?? ordinal;
      return `Node Worker - ${finalNumber}`;
    })();
    card.querySelector('.node-name').textContent = workerLabel;
    const metaEl = card.querySelector('.node-meta');
    if (metaEl) {
      const info = orderingContext.infoMap.get(node.id);
      const baseLabel = (node.label && String(node.label).trim()) || (info && info.base) || String(node.id || '').trim();
      const container = (node.container && String(node.container).trim()) || '';
      metaEl.textContent = container && container !== baseLabel ? `${container} · ${baseLabel}` : baseLabel;
    }

    const summaryHealthChip = card.querySelector('.summary-health-chip');
    const syncChips = {
      progressChip: card.querySelector('[data-role="sync-progress"]'),
      rateChip: card.querySelector('[data-role="sync-rate"]'),
      etaChip: card.querySelector('[data-role="sync-eta"]'),
    };
    const statusEl = card.querySelector('.status-text');
    const stats = node.status || {};
    entry.meta.status = stats;
    const containerRunning = isRunningFlag(
      stats.container_running ?? stats.raw_running ?? stats.running
    );
    const effectiveRunning = isRunningFlag(stats.running);
    const forceOfflineHeader = shouldForceOffline(
      stats,
      containerRunning,
      state.lastProgress.get(node.id)
    );
    const health = resolveHealth(stats, containerRunning, { forceOffline: forceOfflineHeader });
    const displayHealth = health.display;
    const code = health.code;
    const healthDetail = health.detail;
    if (statusEl) {
      statusEl.textContent = displayHealth;
      statusEl.parentElement.classList.remove('is-ok', 'is-warn');
      if (code === 'online') {
        statusEl.parentElement.classList.add('is-ok');
      } else if (code === 'warn') {
        statusEl.parentElement.classList.add('is-warn');
      }
    }
    if (summaryHealthChip) {
      summaryHealthChip.textContent = displayHealth || 'Status';
      if (healthDetail || displayHealth) {
        summaryHealthChip.title = healthDetail || displayHealth;
      } else {
        summaryHealthChip.removeAttribute('title');
      }
      summaryHealthChip.classList.remove('health-online', 'health-offline', 'health-warn');
      if (code === 'online') {
        summaryHealthChip.classList.add('health-online');
      } else if (code === 'warn') {
        summaryHealthChip.classList.add('health-warn');
      } else {
        summaryHealthChip.classList.add('health-offline');
      }
    }

    renderSyncChips(syncChips, {
      progress: state.lastProgress.get(node.id),
      rate: state.lastRates.get(node.id),
      etaInfo: stats.eta_info || null,
      meta: {
        local_height: stats.local_height,
        remote_height: stats.remote_height,
      },
    });

    setStat(card, '.stat-local', stats.local_height);
    setStat(card, '.stat-remote', stats.remote_height);
    setStat(card, '.stat-delta', stats.height_delta, { sign: true });
    setStat(card, '.stat-peers', stats.peers);
    updateUptime(card, stats.uptime_seconds);
    updateStartStopButton(card.querySelector('[data-role="toggle"]'), containerRunning, {
      effectiveRunning,
      forcedOffline: forceOfflineHeader,
    });
    if (entry.meta && entry.meta.status) {
      entry.meta.status.container_running = containerRunning;
      entry.meta.status.forced_offline = forceOfflineHeader;
      entry.meta.status.running = effectiveRunning;
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
      return {
        text: 'Fully synced',
        variant: 'ok',
        hint: 'Local height matches remote height.',
      };
    }
    const rate = averageHeightRate(metrics.labels, metrics.local);
    if (!Number.isFinite(rate) || rate <= 0) {
      return {
        text: 'ETA pending…',
        variant: null,
        hint: 'Waiting for sufficient block data to estimate rate.',
      };
    }
    const etaSec = remaining / rate;
    if (!Number.isFinite(etaSec) || etaSec <= 0 || etaSec > 86400 * 30) {
      return {
        text: 'ETA pending…',
        variant: null,
        hint: 'Rate data is insufficient to estimate ETA.',
      };
    }
    const pretty = formatEtaDuration(etaSec);
    let variant = 'warn';
    if (etaSec <= 900) {
      variant = 'ok';
    } else if (etaSec >= 21600) {
      variant = 'danger';
    }
    const rateValue = rate >= 10 ? rate.toFixed(1) : rate.toFixed(2);
    return {
      text: `ETA ~ ${pretty}`,
      variant,
      hint: `Approximately ${fmt.format(Math.max(remaining, 0))} blocks remaining at ${rateValue} blk/s.`,
    };
  }

  function updateEta(card, metrics) {
    const info = computeEtaInfo(metrics);
    const etaEl = card.querySelector('.stat-eta');
    if (!etaEl) return info;
    etaEl.classList.remove('is-ok', 'is-warn', 'is-danger');
    if (!info) {
      etaEl.textContent = '—';
      return null;
    }
    etaEl.textContent = info.text;
    if (info.variant) {
      etaEl.classList.add(`is-${info.variant}`);
    }
    return info;
  }

  function createChart(ctx) {
    const chart = new Chart(ctx, {
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
              label: () => '',
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#7681a8', maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
            grid: { color: 'rgba(255,255,255,0.06)' },
          },
          y: {
            ticks: { color: '#7681a8', callback: () => '', align: 'inner', crossAlign: 'near' },
            grid: { color: 'rgba(255,255,255,0.06)' },
            position: 'right',
          },
        },
      },
    });
    chart.$formatValue = formatBlocksValue;
    chart.$tickFormatter = (value) => formatBlocksTick(value);
    chart.options.plugins.tooltip.callbacks.label = (tooltipItem) => {
      const formatter = typeof chart.$formatValue === 'function'
        ? chart.$formatValue
        : (value) => formatBlocksValue(value);
      return `${tooltipItem.dataset.label}: ${formatter(tooltipItem.parsed.y)}`;
    };
    chart.options.scales.y.ticks.callback = (value) => {
      const formatter = typeof chart.$tickFormatter === 'function'
        ? chart.$tickFormatter
        : (val) => formatBlocksTick(val);
      return formatter(Number(value));
    };
    return chart;
  }

  function getChartView(nodeId) {
    if (!state.chartViews.has(nodeId)) {
      state.chartViews.set(nodeId, defaultChartView);
    }
    const view = state.chartViews.get(nodeId);
    return chartViewConfigs[view] ? view : defaultChartView;
  }

  function refreshNodeChart(nodeId) {
    const chart = state.charts.get(nodeId);
    if (!chart) return;
    const entry = state.nodes.get(nodeId);
    const metrics = entry?.meta?.status;
    if (!metrics) return;
    const view = getChartView(nodeId);
    const dataset = resolveChartDataset(metrics, view);
    chart.data.labels = dataset.labels;
    chart.data.datasets[0].data = dataset.data;
    chart.data.datasets[0].label = dataset.config.label;
    chart.data.datasets[0].borderColor = dataset.config.color;
    chart.data.datasets[0].backgroundColor = dataset.config.background;
    chart.$formatValue = typeof dataset.config.tooltip === 'function'
      ? dataset.config.tooltip
      : formatBlocksValue;
    chart.$tickFormatter = typeof dataset.config.tick === 'function'
      ? dataset.config.tick
      : (value) => formatBlocksTick(value);
    chart.update('none');
  }

  function selectChartTab(nodeId, nextView, card) {
    const targetView = chartViewConfigs[nextView] ? nextView : defaultChartView;
    state.chartViews.set(nodeId, targetView);
    if (card) {
      const buttons = Array.from(card.querySelectorAll('[data-chart-tab]'));
      buttons.forEach((button) => {
        const isActive = button.dataset.chartTab === targetView;
        button.classList.toggle('is-active', isActive);
      });
    }
    refreshNodeChart(nodeId);
  }

  function locateNodeContainer(nodeId) {
    const entry = state.nodes.get(nodeId);
    if (!entry) return null;
    const meta = entry.meta || {};
    const status = meta.status || {};
    return meta.container || status.container || null;
  }

  function stopLogPolling(nodeId) {
    const timer = state.logPollTimers.get(nodeId);
    if (timer) {
      clearInterval(timer);
      state.logPollTimers.delete(nodeId);
    }
  }

  function startLogPolling(nodeId, card) {
    if (state.logPollTimers.has(nodeId)) return;
    const interval = setInterval(() => {
      const entry = state.nodes.get(nodeId);
      if (!entry || !entry.card || !document.body.contains(entry.card)) {
        stopLogPolling(nodeId);
        return;
      }
      const panel = entry.card.querySelector('[data-role="logs-panel"]');
      if (!panel || panel.hasAttribute('hidden')) return;
      loadNodeLogs(nodeId, entry.card, { force: true, silent: true });
    }, LOG_AUTO_REFRESH_MS);
    state.logPollTimers.set(nodeId, interval);
  }

  function updateLogsDisplay(nodeId, card, logsState = {}) {
    const wrapper = card.querySelector('.node-logs');
    const panel = card.querySelector('[data-role="logs-panel"]');
    const output = card.querySelector('[data-role="logs-output"]');
    const meta = card.querySelector('[data-role="logs-meta"]');
    if (!panel || !output) return;
    const shouldAutoScroll = (() => {
      try {
        const nearBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 20;
        return nearBottom;
      } catch (_) {
        return false;
      }
    })();
    if (logsState.loading) {
      output.textContent = 'Loading logs…';
    } else if (logsState.unavailable) {
      output.textContent = 'Logs unavailable for this node.';
    } else if (logsState.error) {
      const message = logsState.error && logsState.error.message ? ` (${logsState.error.message})` : '';
      output.textContent = `Unable to load logs${message}.`;
    } else if (Array.isArray(logsState.lines) && logsState.lines.length) {
      output.textContent = logsState.lines.join('\n');
    } else {
      output.textContent = 'No recent log entries.';
    }
    if (meta) {
      if (logsState.loading) {
        meta.textContent = 'Loading…';
      } else if (logsState.unavailable) {
        meta.textContent = 'Container offline.';
      } else if (logsState.error) {
        meta.textContent = 'Failed to refresh logs.';
      } else if (Number.isFinite(logsState.ts)) {
        meta.textContent = `Updated ${fmtShortDateTime.format(new Date(logsState.ts))}`;
      } else {
        meta.textContent = 'Updated —';
      }
    }
    if (wrapper) {
      wrapper.classList.toggle('has-error', Boolean(logsState.error));
    }
    if (shouldAutoScroll) {
      try {
        output.scrollTop = output.scrollHeight;
      } catch (_) {
        /* ignore */
      }
    }
  }

  async function loadNodeLogs(nodeId, card, options = {}) {
    const entry = state.nodes.get(nodeId);
    if (!entry) return;
    const limit = Number.isFinite(options.limit) && options.limit > 0 ? Math.min(options.limit, 200) : 80;
    const container = locateNodeContainer(nodeId);
    const silent = options.silent === true;
    if (!container) {
      const unavailableState = {
        lines: [],
        ts: Date.now(),
        loading: false,
        error: null,
        unavailable: true,
        limit,
      };
      state.nodeLogs.set(nodeId, unavailableState);
      updateLogsDisplay(nodeId, card, unavailableState);
      return;
    }
    const cached = state.nodeLogs.get(nodeId);
    const force = options.force === true;
    if (!force && cached && !cached.error && !cached.unavailable) {
      const age = Date.now() - (cached.ts || 0);
      if (age < LOG_REFRESH_COOLDOWN_MS) {
        updateLogsDisplay(nodeId, card, cached);
        return;
      }
    }
    if (!force && cached?.loading) {
      updateLogsDisplay(nodeId, card, cached);
      return;
    }
    const loadingState = {
      ...(cached || {}),
      loading: true,
      error: null,
      unavailable: false,
      limit,
      ts: Date.now(),
    };
    state.nodeLogs.set(nodeId, loadingState);
    if (!silent) {
      updateLogsDisplay(nodeId, card, loadingState);
    }
    try {
      const res = await fetch(
        `/api/node-manager/logs?node=${encodeURIComponent(nodeId)}&limit=${limit}`,
        { cache: 'no-store' },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      const lines = Array.isArray(payload?.lines) ? payload.lines : [];
      const nextState = {
        lines,
        ts: Date.now(),
        loading: false,
        error: null,
        unavailable: false,
        limit,
      };
      state.nodeLogs.set(nodeId, nextState);
      updateLogsDisplay(nodeId, card, nextState);
    } catch (err) {
      console.error('[fleet] fetch recent logs failed', err);
      const failureState = {
        lines: [],
        ts: Date.now(),
        loading: false,
        error: err,
        unavailable: false,
        limit,
      };
      state.nodeLogs.set(nodeId, failureState);
      updateLogsDisplay(nodeId, card, failureState);
    }
  }

  function toggleNodeLogs(nodeId, card) {
    const wrapper = card.querySelector('.node-logs');
    const panel = card.querySelector('[data-role="logs-panel"]');
    const toggle = card.querySelector('[data-role="logs-toggle"]');
    if (!panel || !toggle || !wrapper) return;
    const isHidden = panel.hasAttribute('hidden');
    if (isHidden) {
      panel.removeAttribute('hidden');
      toggle.setAttribute('aria-expanded', 'true');
      wrapper.classList.add('is-open');
      void loadNodeLogs(nodeId, card);
      startLogPolling(nodeId, card);
    } else {
      panel.setAttribute('hidden', '');
      toggle.setAttribute('aria-expanded', 'false');
      wrapper.classList.remove('is-open');
      stopLogPolling(nodeId);
    }
  }

  function updateStartStopButton(btn, containerRunning, options = {}) {
    if (!btn) return;
    const { effectiveRunning = containerRunning, forcedOffline = false } = options;
    let action = 'start';
    if (containerRunning && !forcedOffline) {
      action = 'stop';
    } else if (containerRunning && forcedOffline) {
      action = 'restart';
    }
    btn.dataset.running = containerRunning ? '1' : '0';
    btn.dataset.effectiveRunning = effectiveRunning ? '1' : '0';
    btn.dataset.action = action;
    btn.classList.toggle('is-stalled', Boolean(containerRunning && forcedOffline));
    let icon = '▶';
    let aria = 'Start node';
    let title = 'Start container';
    if (action === 'restart') {
      icon = '⟳';
      aria = 'Restart node';
      title = 'Restart container (stalled detection)';
    } else if (containerRunning) {
      icon = '⏹';
      aria = 'Stop node';
      title = 'Stop container';
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
    const containerRunning = isRunningFlag(
      status.container_running ?? status.raw_running ?? status.running
    );
    const effectiveRunning = isRunningFlag(status.running);
    const previousProgress = state.lastProgress.get(nodeId);
    const forcedOffline = shouldForceOffline(status, containerRunning, previousProgress);
    let action = 'docker_start';
    if (containerRunning && !forcedOffline) {
      action = 'docker_stop';
    } else if (containerRunning && forcedOffline) {
      action = 'docker_restart';
    }
    const optimisticState = (() => {
      if (action === 'docker_start' || action === 'docker_restart') {
        return { containerRunning: true, effectiveRunning: true, forcedOffline: false };
      }
      if (action === 'docker_stop') {
        return { containerRunning: false, effectiveRunning: false, forcedOffline: false };
      }
      return null;
    })();
    if (btn && optimisticState) {
      updateStartStopButton(btn, optimisticState.containerRunning, {
        effectiveRunning: optimisticState.effectiveRunning,
        forcedOffline: optimisticState.forcedOffline,
      });
    }
    if (optimisticState) {
      entry.meta.status = {
        ...(entry.meta.status || {}),
        ...status,
        container_running: optimisticState.containerRunning,
        running: optimisticState.effectiveRunning,
        effective_running: optimisticState.effectiveRunning,
        forced_offline: optimisticState.forcedOffline,
      };
    }
    if (btn) {
      btn.disabled = true;
      btn.dataset.manualDisable = '1';
    }
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
      if (btn) {
        delete btn.dataset.manualDisable;
        if (!btn.dataset.snapshotLock) {
          btn.disabled = false;
        }
      }
      await loadNodes();
      await refreshMetrics();
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

      const containerRunning = isRunningFlag(
        metrics.container_running ?? metrics.raw_running ?? metrics.running
      );
      const effectiveRunning = isRunningFlag(metrics.running);
      const summaryHealthChip = card.querySelector('.summary-health-chip');
      const previousProgress = state.lastProgress.get(nodeId);
      const forceOffline = shouldForceOffline(metrics, containerRunning, previousProgress);
      const health = resolveHealth(metrics, containerRunning, { forceOffline });
      const displayHealth = health.display;
      const healthDetail = health.detail;
      const code = health.code;
      const nodeStatusEl = card.querySelector('.node-status');
      if (nodeStatusEl) {
        nodeStatusEl.classList.toggle('is-ok', code === 'online');
        nodeStatusEl.classList.toggle('is-warn', code !== 'online');
        const textEl = nodeStatusEl.querySelector('.status-text');
        if (textEl) {
          textEl.textContent = healthDetail || '';
        }
      }
      if (summaryHealthChip) {
        summaryHealthChip.textContent = displayHealth || 'Status';
        if (healthDetail || displayHealth) {
          summaryHealthChip.title = healthDetail || displayHealth;
        } else {
          summaryHealthChip.removeAttribute('title');
        }
        summaryHealthChip.classList.remove('health-online', 'health-offline', 'health-warn');
        if (code === 'online') {
          summaryHealthChip.classList.add('health-online');
        } else if (code === 'warn') {
          summaryHealthChip.classList.add('health-warn');
        } else {
          summaryHealthChip.classList.add('health-offline');
        }
      }

      const syncChips = {
        progressChip: card.querySelector('[data-role="sync-progress"]'),
        rateChip: card.querySelector('[data-role="sync-rate"]'),
        etaChip: card.querySelector('[data-role="sync-eta"]'),
      };
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
      const etaInfo = updateEta(card, metrics);
      renderSyncChips(syncChips, {
        progress,
        rate,
        etaInfo,
        meta: {
          local_height: metrics.local_height,
          remote_height: metrics.remote_height,
        },
      });

      updateStartStopButton(card.querySelector('[data-role="toggle"]'), containerRunning, {
        effectiveRunning,
        forcedOffline: forceOffline,
      });
      entry.meta.status = {
        ...(entry.meta.status || {}),
        ...metrics,
        container_running: containerRunning,
        forced_offline: forceOffline,
        effective_running: effectiveRunning,
        running: effectiveRunning,
        eta_info: etaInfo || null,
      };

      const tsEl = card.querySelector('.stat-updated');
      if (tsEl) {
        const ts = metrics.last_updated ? new Date(metrics.last_updated) : new Date(now);
        tsEl.textContent = fmtTime.format(ts);
      }

      if (state.paused.has(nodeId)) {
        return;
      }

      if (state.charts.has(nodeId)) {
        refreshNodeChart(nodeId);
      }
    });
  }

  function attachEventHandlers() {
    const discoverBtn = document.getElementById('btnDiscoverNodes');
    if (discoverBtn) {
      discoverBtn.addEventListener('click', () => discoverNodes());
    }
    summaryTabButtons.forEach((button) => {
      button.addEventListener('click', () => switchSummaryTab(button));
      button.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          switchSummaryTab(button);
        }
      });
    });
    if (settingsForm) {
      settingsForm.addEventListener('change', (event) => {
        const target = event.target;
        if (target && target.matches('[data-setting-key]')) {
          const key = target.dataset.settingKey;
          const type = target.dataset.settingType || target.type;
          if (type === 'number') {
            let value = Number.parseInt(target.value, 10);
            if (!Number.isFinite(value) || value < 0) {
              value = 0;
            }
            target.value = value;
            state.settings[key] = value;
          } else if (type === 'text') {
            const value = target.value.trim();
            target.value = value;
            state.settings[key] = value;
          } else {
            state.settings[key] = !!target.checked;
          }
          markSettingsDirty();
          updateAutoRestartCooldownState(state.settings);
        }
      });
    }
    if (saveSettingsBtn) {
      saveSettingsBtn.addEventListener('click', () => {
        void saveSettings();
      });
    }
    if (snapshotRefreshBtn) {
      snapshotRefreshBtn.addEventListener('click', () => {
        void refreshSnapshots();
      });
    }
    if (snapshotScanBtn) {
      snapshotScanBtn.addEventListener('click', () => {
        void scanSnapshots();
      });
    }
    if (snapshotList) {
      snapshotList.addEventListener('click', (event) => {
        const target = event.target.closest('[data-snapshot-action]');
        if (!target) return;
        const action = target.dataset.snapshotAction;
        if (action === 'delete') {
          const name = target.dataset.snapshotName;
          void deleteSnapshot(name, target);
        }
      });
    }
  }

  async function init() {
    attachEventHandlers();
    const initialTab = summaryTabButtons.find((button) => button.classList.contains('is-active')) || summaryTabButtons[0] || null;
    if (initialTab) {
      switchSummaryTab(initialTab);
    }
    await loadSettings();
    await loadNodes();
    await refreshMetrics();
    await loadSnapshots({ silent: true });
    await discoverNodes({ auto: true });
    await refreshMetrics();
    setInterval(refreshMetrics, 5000);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshMetrics();
      if (state.snapshotsLoaded) {
        void loadSnapshots({ silent: true });
      }
    }
  });

  window.addEventListener('load', init);
})();
