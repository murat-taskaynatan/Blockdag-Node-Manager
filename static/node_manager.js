(() => {
  const state = {
    nodes: new Map(), // id -> { card, meta }
    charts: new Map(), // id -> Chart instance
    paused: new Set(),
    lastRates: new Map(), // id -> last non-null sync rate
    lastProgress: new Map(), // id -> last non-null sync progress
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
    snapshotStatus: { text: '', level: '' },
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
  let settingsStatusTimer = null;
  let snapshotPollTimer = null;
  const defaultSettings = {
    liveness_auto_recover: false,
    auto_restart_on_error: false,
    display_wallet_balance: false,
  };

  state.settings = { ...defaultSettings };

  const fmt = new Intl.NumberFormat();
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const fmtDateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

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

  function setBusy(btn, busy, text) {
    if (!btn) return;
    if (busy) {
      if (!btn.dataset.originalText) {
        btn.dataset.originalText = btn.textContent;
      }
      if (text) {
        btn.textContent = text;
      }
      btn.disabled = true;
      btn.dataset.busy = '1';
    } else {
      if (btn.dataset.originalText) {
        btn.textContent = btn.dataset.originalText;
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
    state.snapshotStatus = { text: message || '', level };
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
  }

  function updateSnapshotButtons() {
    const job = (state.snapshots && state.snapshots.job) || null;
    const jobActive = !!(job && job.active);
    const jobDetails = (job && job.details) || {};
    const jobNode = jobDetails && jobDetails.node;
    state.nodes.forEach((entry) => {
      if (!entry || !entry.card || !entry.meta) return;
      const btn = entry.card.querySelector('[data-action="node-snapshot"]');
      if (!btn) return;
      const nodeId = entry.meta.id;
      const label = entry.meta.label || nodeId || 'node';
      let title = `Create snapshot for ${label}`;
      let disabled = false;
      if (btn.dataset.busy === '1') {
        disabled = true;
      }
      if (state.snapshots && state.snapshots.job && state.snapshots.job.active) {
        disabled = true;
        if (jobNode && jobNode === nodeId) {
          title = `Snapshot running for ${label}`;
          btn.classList.add('is-busy');
        } else {
          title = 'Snapshot in progress';
          btn.classList.remove('is-busy');
        }
      } else {
        btn.classList.remove('is-busy');
      }
      btn.disabled = disabled;
      btn.title = title;
      btn.setAttribute('aria-label', title);
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
        metaEl.textContent = metaParts.length ? metaParts.join(' • ') : '—';
        tile.appendChild(metaEl);

        snapshotList.appendChild(tile);
      });
    }

    const jobActive = job && job.active;
    if (jobActive) {
      setSnapshotStatus(job.message || 'Snapshot job running…', { level: 'warn' });
      scheduleSnapshotPoll(true);
    } else if (job && job.status && job.message) {
      const levelMap = { completed: 'ok', error: 'error', cancelled: 'warn' };
      const level = levelMap[job.status] || '';
      setSnapshotStatus(job.message, { level });
      scheduleSnapshotPoll(false);
    } else if (state.snapshotStatus && state.snapshotStatus.text) {
      setSnapshotStatus(state.snapshotStatus.text, { level: state.snapshotStatus.level });
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

    updateSnapshotButtons();
  }

  async function loadSnapshots(options = {}) {
    const { silent = false } = options;
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
      if (payload.status && payload.status.text) {
        state.snapshotStatus = {
          text: payload.status.text,
          level: payload.status.level || '',
        };
      } else if (!silent) {
        state.snapshotStatus = { text: 'Snapshots refreshed.', level: 'ok' };
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
      setBusy(btn, true, 'Working…');
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
      state.snapshotStatus = {
        text: payload.message || 'Snapshot locations updated.',
        level: 'ok',
      };
      await loadSnapshots({ silent: true });
      setSnapshotStatus(state.snapshotStatus.text, { level: 'ok' });
    } catch (err) {
      setSnapshotStatus(err && err.message ? err.message : 'Snapshot scan failed', { level: 'error' });
    } finally {
      setBusy(snapshotScanBtn, false);
    }
  }

  async function refreshSnapshots() {
    await loadSnapshots();
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
        if (progressValue >= 95) {
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
    };
    const next = copy[activeView] || copy.stats;
    summaryDynamicTitle.textContent = next.title;
    summaryDynamicDesc.textContent = next.desc;
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
  state.settings = merged;
  state.settingsDirty = false;
  if (settingsForm) {
    const inputs = settingsForm.querySelectorAll('[data-setting-key]');
    inputs.forEach((input) => {
      const key = input.dataset.settingKey;
      if (!key) return;
      input.checked = !!merged[key];
    });
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
      const differs = Object.keys(defaultSettings).some((key) => !!state.settings[key] !== !!incoming[key]);
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
        state.paused.delete(nodeId);
        state.lastRates.delete(nodeId);
        state.lastProgress.delete(nodeId);
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
          state.settings[key] = !!target.checked;
          markSettingsDirty();
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
