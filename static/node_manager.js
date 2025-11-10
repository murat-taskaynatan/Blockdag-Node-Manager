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
    summary: null,
    settingsDirty: false,
    snapshots: {
      items: [],
      locations: [],
      dir: '',
      job: null,
    },
    snapshotsLoaded: false,
    snapshotStatus: { text: '', level: '', manual: false },
    snapshotCountdown: null,
    overlayStatus: { items: [], byNode: new Map() },
    automationLogs: {
      items: [],
      expanded: false,
      loading: false,
      error: null,
      lastFetched: 0,
      filter: 'all',
    },
    nodesDiscovering: false,
    discoveryStartTs: 0,
    discoveryDismissed: false,
    launchpad: {
      step: 1,
      data: {
        label: '',
        installPath: '',
        p2pPort: 38130,
        rpcPort: 18545,
        autoPorts: true,
      },
      previewPorts: null,
      previewLoading: false,
      previewError: null,
      previewRequestId: 0,
    },
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
  const systemPane = document.querySelector('[data-summary-pane="system"]');
  const systemCpuValue = document.getElementById('statCpu');
  const systemCpuBar = document.getElementById('statCpuBar');
  const systemMemoryValue = document.getElementById('statMemory');
  const systemMemoryBar = document.getElementById('statMemoryBar');
  const systemDiskValue = document.getElementById('statDisk');
  const systemDiskBar = document.getElementById('statDiskBar');
  const systemCpuTempValue = document.getElementById('statCpuTemp');
  const systemCpuTempBar = document.getElementById('statCpuTempBar');
  const settingsForm = document.getElementById('settingsForm');
  const saveSettingsBtn = document.getElementById('btnSaveSettings');
  const settingsStatus = document.getElementById('settingsStatus');
  const overclockForm = document.getElementById('overclockForm');
  const overclockStatus = document.getElementById('overclockStatus');
  // Data directory input removed; backend auto-detects
  const ocDataPath = document.getElementById('ocDataPath');
  const ocCpu = document.getElementById('ocCpu');
  const ocNvmeLatency = document.getElementById('ocNvmeLatency');
  const ocScheduler = document.getElementById('ocScheduler');
  const ocRemount = document.getElementById('ocRemount');
  // WAL/VWC controls removed from UI
  const ocVwcRisk = document.getElementById('ocVwcRisk');
  let ocChart = null;
  let launchpadPreviewSeq = 0;
  const ocHistory = [];
  const snapshotList = document.getElementById('snapshotList');
  const snapshotStatus = document.getElementById('snapshotStatus');
  const snapshotEmptyState = document.getElementById('snapshotEmptyState');
  const snapshotRefreshBtn = document.getElementById('btnRefreshSnapshots');
  const snapshotScanBtn = document.getElementById('btnScanSnapshots');
  const automationLogPanel = document.getElementById('automationLogPanel');
  const automationLogToggle = document.getElementById('automationLogToggle');
  const automationLogBody = document.getElementById('automationLogBody');
  const automationLogList = document.getElementById('automationLogList');
  const automationLogEmpty = document.getElementById('automationLogEmpty');
  const automationLogStatus = document.getElementById('automationLogStatus');
  const automationLogRefreshBtn = document.getElementById('automationLogRefresh');
  const automationLogCount = document.getElementById('automationLogCount');
  const automationLogFilter = document.getElementById('automationLogFilter');
  const walletAddressValue = document.getElementById('walletAddressValue');
  const walletBalanceValue = document.getElementById('walletBalanceValue');
  const walletTotalValue = document.getElementById('walletTotalValue');
  const walletUpdatedValue = document.getElementById('walletUpdatedValue');
  const walletHistoryList = document.getElementById('walletHistoryList');
  const walletHistoryEmpty = document.getElementById('walletHistoryEmpty');
  const walletPane = document.getElementById('walletPane');
  const walletChartWrapper = document.getElementById('walletChart');
  const walletChartCanvas = document.getElementById('walletBalanceChart');
  const walletChartEmpty = document.getElementById('walletChartEmpty');
  const autoRestartHoursInput = document.getElementById('settingAutoRestartHours');
  const autoSnapshotHoursInput = document.getElementById('settingAutoSnapshotHours');
  const memoryRestartToggle = document.getElementById('settingMemRestartEnabled');
  const memoryRestartThresholdInput = document.getElementById('settingMemThreshold');
  const ocLogsPanel = document.getElementById('ocLogsPanel');
  const ocLogsToggle = document.getElementById('ocLogsToggle');
  const ocLogsBody = document.getElementById('ocLogsBody');
  const ocLogsMeta = document.getElementById('ocLogsMeta');
  const ocLogsOutput = document.getElementById('ocLogsOutput');
  const ocLogsRefreshBtn = document.getElementById('ocLogsRefresh');
  const launchpadStepsContainer = document.getElementById('launchpadSteps');
  const launchpadSections = {
    1: document.querySelector('[data-launchpad-step="1"]'),
    2: document.querySelector('[data-launchpad-step="2"]'),
    3: document.querySelector('[data-launchpad-step="3"]'),
  };
  const launchpadFields = {
    label: document.getElementById('launchpadNodeLabel'),
    installPath: document.getElementById('launchpadInstallPath'),
    p2pPort: document.getElementById('launchpadP2PPort'),
    rpcPort: document.getElementById('launchpadRpcPort'),
    autoPorts: document.getElementById('launchpadAutoPorts'),
    walletAddress: document.getElementById('launchpadWalletAddress'),
    externalP2PPort: document.getElementById('launchpadExternalP2PPort'),
    wsPort: document.getElementById('launchpadWsPort'),
    peerPort: document.getElementById('launchpadPeerPort'),
    externalPeerPort: document.getElementById('launchpadExternalPeerPort'),
  };
  const launchpadSummaryRefs = {
    label: document.getElementById('launchpadSummaryLabel'),
    path: document.getElementById('launchpadSummaryPath'),
    p2pPort: document.getElementById('launchpadSummaryP2P'),
    rpcPort: document.getElementById('launchpadSummaryRpc'),
    wallet: document.getElementById('launchpadSummaryWallet'),
    externalP2P: document.getElementById('launchpadSummaryExternalP2P'),
    ws: document.getElementById('launchpadSummaryWs'),
    peer: document.getElementById('launchpadSummaryPeer'),
    externalPeer: document.getElementById('launchpadSummaryExternalPeer'),
  };
  const launchpadBackBtn = document.getElementById('launchpadBackBtn');
  const launchpadNextBtn = document.getElementById('launchpadNextBtn');
  const launchpadNextIcon = launchpadNextBtn?.querySelector('[data-launch-icon]') ?? null;
  const launchpadNextLabel = launchpadNextBtn?.querySelector('[data-launch-label]') ?? null;
  const launchpadStatus = document.getElementById('launchpadStatus');
  let ocLogPollTimer = null;
  const SYSTEM_POLL_INTERVAL_MS = 10000;
  let systemPollTimer = null;
  const AUTOMATION_LOG_LIMIT = 100;
  const AUTOMATION_LOG_POLL_MS = 30000;
  let automationLogTimer = null;
  const ocChartPane = document.getElementById('overclockChartPane');
  const ocManualPane = document.getElementById('overclockManualPane');
  const ocManualContent = document.getElementById('overclockManualContent');
  let ocManualLoaded = false;
  const ocChartEmpty = document.getElementById('overclockChartEmpty');
  const ocCanvas = document.getElementById('overclockVerifyChart');
  // VM‑Mode removed
  const ocOverlayBdagChain = document.getElementById('ocOverlayBdagChain');
  const ocOverlayBdagEth = document.getElementById('ocOverlayBdagEth');
  const ocOverlayIntervalBdagChain = document.getElementById('ocOverlayIntervalBdagChain');
  const ocOverlayLimitBdagChain = document.getElementById('ocOverlayLimitBdagChain');
  const ocOverlayIntervalBdagEth = document.getElementById('ocOverlayIntervalBdagEth');
  const ocOverlayLimitBdagEth = document.getElementById('ocOverlayLimitBdagEth');
  const ocOverlayStatus = document.getElementById('ocOverlayStatus');
  const ocOverlayChartWrapper = document.getElementById('ocOverlayChartWrapper');
  const ocOverlayChartCanvas = document.getElementById('ocOverlayChart');
  const ocOverlayChartEmpty = document.getElementById('ocOverlayChartEmpty');
  let ocOverlayChart = null;
  const logoutBtn = document.getElementById('btnLogout');
  const nodeDiscoveryMessage = document.getElementById('nodeDiscoveryMessage');
  const nodeDiscoverySubtext = document.getElementById('nodeDiscoverySubtext');
  const nodeDiscoveryDismissBtn = document.getElementById('nodeDiscoveryDismissBtn');
  // Allow init() to push a metric once auto-verify returns
  let ocAppendMetric = null;
  function drawFallbackChart(labels, iopsData, p50Data) {
    if (!ocCanvas) return;
    const ctx = ocCanvas.getContext('2d');
    const w = ocCanvas.width = ocCanvas.clientWidth || 600;
    const h = ocCanvas.height = ocCanvas.clientHeight || 140;
    ctx.clearRect(0, 0, w, h);
    // Padding
    const padL = 32, padR = 12, padT = 12, padB = 18;
    const plotW = Math.max(10, w - padL - padR);
    const plotH = Math.max(10, h - padT - padB);
    // Ranges
    const xs = labels.length;
    const minX = 0, maxX = Math.max(1, xs - 1);
    const iopsVals = iopsData.filter(v => Number.isFinite(v));
    const p50Vals = p50Data.filter(v => Number.isFinite(v));
    const iopsMin = iopsVals.length ? Math.min(...iopsVals) : 0;
    const iopsMax = iopsVals.length ? Math.max(...iopsVals) : 1;
    const p50Min = p50Vals.length ? Math.min(...p50Vals) : 0;
    const p50Max = p50Vals.length ? Math.max(...p50Vals) : 1;
    const scale = (val, min, max) => {
      const r = max - min || 1;
      return Math.max(0, Math.min(1, (val - min) / r));
    };
    // Grid
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i <= 4; i++) {
      const y = padT + (plotH * i) / 4;
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
    }
    ctx.stroke();
    // Draw series helper
    function drawSeries(values, color, min, max) {
      if (!values.length) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i = 0; i < values.length; i++) {
        const v = Number.isFinite(values[i]) ? values[i] : null;
        const x = padL + (plotW * (i - minX)) / (maxX - minX || 1);
        if (v == null) continue;
        const t = scale(v, min, max);
        const y = padT + plotH * (1 - t);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    // IOPS (green, left axis)
    drawSeries(iopsData, '#44f2a8', iopsMin, iopsMax);
    // p50 (blue-ish, right axis) — scale to its own range
    drawSeries(p50Data, '#55aaff', p50Min, p50Max);
    // Axes labels (simple)
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
    if (labels.length) {
      ctx.fillText('IOPS (left), p50 μs (right)', padL, padT - 2);
    }
  }
  function updateOcLayout() {
    const form = document.getElementById('overclockForm');
    if (!form) return;
    const noChart = !ocChartPane || ocChartPane.hidden === true;
    const noManual = !ocManualPane || ocManualPane.hidden === true;
    if (noChart && noManual) {
      form.classList.add('oc-compact-logs');
    } else {
      form.classList.remove('oc-compact-logs');
    }
  }
  function setOcLogsExpanded(expanded) {
    if (!ocLogsPanel) return;
    ocLogsPanel.hidden = false;
    if (ocLogsBody) {
      ocLogsBody.hidden = !expanded;
    }
    if (ocLogsToggle) {
      ocLogsToggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }
    ocLogsPanel.classList.toggle('is-open', !!expanded);
    if (!expanded) {
      if (ocLogsMeta) ocLogsMeta.textContent = 'Collapsed';
      stopOcLogPolling();
    } else {
      startOcLogPolling();
    }
  }
  function openOcLogs() {
    setOcLogsExpanded(true);
    void loadOcLogs({ force: true });
  }
  let settingsStatusTimer = null;
  let snapshotPollTimer = null;
  const defaultSettings = {
    liveness_auto_recover: true,
    auto_restart_on_error: false,
    auto_restart_enabled: false,
    auto_restart_hours: 0,
    auto_restart_mem_enabled: false,
    auto_restart_mem_threshold: 0,
    auto_snapshot_enabled: false,
    auto_snapshot_hours: 0,
    display_wallet_balance: false,
    snapshot_max: 0,
    cpu_temp_path: '',
  };

  state.settings = { ...defaultSettings };
  state.walletHistory = [];
  state.walletBalanceHistory = [];
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
    if (value >= 99.995) return '100%';
    if (value >= 99.5) return `${value.toFixed(2)}%`;
    if (value >= 10) return `${value.toFixed(1)}%`;
    return `${value.toFixed(2)}%`;
  }

  const USAGE_HIGH = 80;
  const USAGE_MED = 60;
  function formatTemperature(value) {
    const temp = Number(value);
    if (!Number.isFinite(temp)) {
      return '—';
    }
    return `${temp.toFixed(1)}°C`;
  }

  function applyUsageColor(bar, value) {
    if (!bar) return;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      bar.classList.remove('usage-progress--green', 'usage-progress--orange', 'usage-progress--red');
      return;
    }
    bar.classList.remove('usage-progress--green', 'usage-progress--orange', 'usage-progress--red');
    if (numeric >= USAGE_HIGH) {
      bar.classList.add('usage-progress--red');
    } else if (numeric >= USAGE_MED) {
      bar.classList.add('usage-progress--orange');
    } else {
      bar.classList.add('usage-progress--green');
    }
  }

  if (logoutBtn) {
    logoutBtn.addEventListener('click', () => {
      const logoutUrl = logoutBtn.dataset.logoutUrl || '/logout';
      logoutBtn.disabled = true;
      fetch(logoutUrl, {method: 'GET'})
        .then(() => {
          location.reload();
        })
        .catch(() => {
          logoutBtn.disabled = false;
        });
    });
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

  const automationKindLabels = {
    auto_restart: 'Auto Restart',
    chain_restore: 'Chain Recovery',
    auto_snapshot: 'Auto Snapshot',
  };

  function automationKindLabel(kind) {
    if (!kind) return 'Automation';
    return automationKindLabels[kind] || 'Automation';
  }

  function automationStatusLabel(status) {
    if (!status) return null;
    const mapping = {
      started: 'Started',
      success: 'Success',
      failed: 'Failed',
      completed: 'Completed',
      skipped: 'Skipped',
      error: 'Error',
    };
    return mapping[status] || status;
  }

  function automationLogMatchesFilter(entry) {
    if (!entry) return true;
    const filter = (state.automationLogs.filter || 'all').toLowerCase();
    if (!filter || filter === 'all') return true;
    const kind = String(entry.kind || '').toLowerCase();
    return kind === filter;
  }

  function getFilteredAutomationLogs(items) {
    if (!Array.isArray(items)) {
      return [];
    }
    if (!state.automationLogs || !state.automationLogs.filter) {
      return items;
    }
    return items.filter((entry) => automationLogMatchesFilter(entry));
  }

  function formatAutomationTimestamp(entry) {
    if (!entry) return '';
    const ts = Number(entry.ts);
    if (Number.isFinite(ts)) {
      return fmtShortDateTime.format(new Date(ts * 1000));
    }
    if (entry.ts_iso) {
      const date = new Date(entry.ts_iso);
      if (!Number.isNaN(Number(date))) {
        return fmtShortDateTime.format(date);
      }
    }
    return '';
  }

  function updateAutomationStatus(text, options = {}) {
    if (!automationLogStatus) return;
    automationLogStatus.textContent = text || '';
    automationLogStatus.classList.toggle('is-error', !!options.error);
  }

  function renderAutomationLogs() {
    if (!automationLogList || !automationLogEmpty) return;
    const items = Array.isArray(state.automationLogs.items) ? state.automationLogs.items : [];
    const filteredItems = getFilteredAutomationLogs(items);
    automationLogList.innerHTML = '';
    if (!filteredItems.length) {
      automationLogEmpty.hidden = false;
      automationLogEmpty.textContent = items.length
        ? 'No automation events match the selected filter.'
        : 'No automation activity yet.';
    } else {
      automationLogEmpty.hidden = true;
      const fragment = document.createDocumentFragment();
      filteredItems.forEach((entry) => {
        const kind = String(entry?.kind || 'automation');
        const li = document.createElement('li');
        li.className = 'automation-log-entry';

        const header = document.createElement('div');
        header.className = 'automation-log-entry__header';
        const kindChip = document.createElement('span');
        kindChip.className = `automation-log-entry__kind automation-log-entry__kind--${kind}`;
        kindChip.textContent = automationKindLabel(kind);
        const timeLabel = document.createElement('span');
        timeLabel.textContent = formatAutomationTimestamp(entry);
        header.append(kindChip, timeLabel);

        const title = document.createElement('div');
        title.className = 'automation-log-entry__title';
        title.textContent = entry?.message || automationKindLabel(kind);

        const details = [];
        if (entry?.node) {
          details.push(`Node ${entry.node}`);
        }
        if (entry?.meta && typeof entry.meta.reason === 'string' && entry.meta.reason.trim()) {
          details.push(entry.meta.reason.trim());
        }
        const statusLabel = automationStatusLabel(entry?.status);
        if (statusLabel) {
          details.push(statusLabel);
        }
        if (entry?.meta && typeof entry.meta.path === 'string') {
          details.push(entry.meta.path);
        }

        li.append(header, title);
        if (details.length) {
          const detail = document.createElement('div');
          detail.className = 'automation-log-entry__detail';
          detail.textContent = details.join(' • ');
          li.append(detail);
        }
        fragment.append(li);
      });
      automationLogList.append(fragment);
    }
    if (automationLogCount) {
      if (filteredItems.length === items.length) {
        automationLogCount.textContent = String(filteredItems.length);
      } else {
        automationLogCount.textContent = `${filteredItems.length}/${items.length}`;
      }
    }
  }

  function stopAutomationLogPolling() {
    if (automationLogTimer) {
      clearInterval(automationLogTimer);
      automationLogTimer = null;
    }
  }

  function startAutomationLogPolling() {
    if (!state.automationLogs.expanded) return;
    stopAutomationLogPolling();
    automationLogTimer = window.setInterval(() => {
      void loadAutomationLogs({ silent: true });
    }, AUTOMATION_LOG_POLL_MS);
  }

  async function loadAutomationLogs(options = {}) {
    if (!automationLogPanel) return;
    const { force = false, silent = false } = options;
    if (state.automationLogs.loading && !force) return;
    state.automationLogs.loading = true;
    if (!silent) {
      updateAutomationStatus('Loading…');
    }
    try {
      const res = await fetch(`/api/node-manager/automation/logs?limit=${AUTOMATION_LOG_LIMIT}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      state.automationLogs.items = Array.isArray(payload.logs) ? payload.logs : [];
      state.automationLogs.lastFetched = Date.now();
      state.automationLogs.error = null;
      renderAutomationLogs();
      const updatedLabel = fmtTime.format(new Date(state.automationLogs.lastFetched));
      updateAutomationStatus(`Updated ${updatedLabel}`);
    } catch (err) {
      state.automationLogs.error = err;
      updateAutomationStatus('Failed to load logs', { error: true });
    } finally {
      state.automationLogs.loading = false;
    }
  }

  function setAutomationLogsExpanded(expanded) {
    if (!automationLogBody || !automationLogToggle) return;
    state.automationLogs.expanded = expanded;
    automationLogBody.hidden = !expanded;
    automationLogToggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    if (expanded) {
      if (!state.automationLogs.items.length) {
        void loadAutomationLogs({ force: true });
      }
      startAutomationLogPolling();
    } else {
      stopAutomationLogPolling();
    }
  }

  function updateAutoRestartCooldownState(settings = state.settings) {
    if (!autoRestartHoursInput) return;
    const enabled = Boolean(settings?.auto_restart_enabled);
    autoRestartHoursInput.disabled = enabled;
    alignSnapshotSpinnerStyle();
  }

  function updateAutoSnapshotState(settings = state.settings) {
    if (!autoSnapshotHoursInput) return;
    const enabled = Boolean(settings?.auto_snapshot_enabled);
    autoSnapshotHoursInput.disabled = enabled;
    alignSnapshotSpinnerStyle();
  }

  function updateMemoryRestartState(settings = state.settings) {
    if (!memoryRestartToggle || !memoryRestartThresholdInput) return;
    const enabled = Boolean(settings?.auto_restart_mem_enabled);
    memoryRestartThresholdInput.disabled = enabled;
  }

  function alignSnapshotSpinnerStyle() {
    if (!autoRestartHoursInput || !autoSnapshotHoursInput) return;
    window.requestAnimationFrame(() => {
      const restartStyles = window.getComputedStyle(autoRestartHoursInput);
      const cssProps = [
        'width',
        'min-width',
        'max-width',
        'padding-top',
        'padding-right',
        'padding-bottom',
        'padding-left',
        'border-top-width',
        'border-right-width',
        'border-bottom-width',
        'border-left-width',
        'border-top-style',
        'border-right-style',
        'border-bottom-style',
        'border-left-style',
        'border-top-left-radius',
        'border-top-right-radius',
        'border-bottom-right-radius',
        'border-bottom-left-radius',
        'font-size',
        'font-weight',
        'line-height',
        'margin-left',
        'margin-right',
      ];
      cssProps.forEach((prop) => {
        const value = restartStyles.getPropertyValue(prop);
        if (value) {
          autoSnapshotHoursInput.style.setProperty(prop, value);
        }
      });
      const borderColor = restartStyles.getPropertyValue('border-top-color');
      if (borderColor) {
        autoSnapshotHoursInput.style.setProperty('border-color', borderColor);
      }
      const backgroundColor = restartStyles.getPropertyValue('background-color');
      if (backgroundColor) {
        autoSnapshotHoursInput.style.setProperty('background-color', backgroundColor);
      }
      const textColor = restartStyles.getPropertyValue('color');
      if (textColor) {
        autoSnapshotHoursInput.style.setProperty('color', textColor);
      }
    });
  }

  let walletChart = null;
  const walletFallbackCtx = walletChartCanvas ? walletChartCanvas.getContext('2d') : null;

  function ensureWalletChart() {
    if (!walletChartCanvas || typeof Chart === 'undefined') {
      return null;
    }
    if (!walletChart) {
      const ctx = walletChartCanvas.getContext('2d');
      walletChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            {
              label: 'Balance (BDAG)',
              data: [],
              borderColor: '#44f2a8',
              backgroundColor: 'rgba(68,242,168,0.18)',
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
              display: false,
            },
            tooltip: {
              callbacks: {
                title: (items) => items.map((item) => item.label).join(', '),
                label: (context) => {
                  const dataset = context.dataset;
                  const meta = Array.isArray(dataset?.meta) ? dataset.meta[context.dataIndex] : null;
                  if (meta && typeof meta.formatted === 'string') {
                    return meta.formatted;
                  }
                  const value = Number(context.parsed.y);
                  if (!Number.isFinite(value)) return '';
                  return `${value.toLocaleString(undefined, { maximumFractionDigits: 4 })} BDAG`;
                },
              },
            },
          },
          scales: {
            x: {
              ticks: { color: '#7681a8', maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
              grid: { color: 'rgba(255,255,255,0.06)' },
            },
            y: {
              ticks: { color: '#7681a8', maxTicksLimit: 6 },
              grid: { color: 'rgba(255,255,255,0.06)' },
              border: { display: false },
              position: 'right',
            },
          },
        },
      });
    }
    return walletChart;
  }

  function updateWalletChart(history = state.walletBalanceHistory, { enabled = false } = {}) {
    if (!walletChartWrapper || !walletChartCanvas) return;
    walletChartWrapper.classList.toggle('is-disabled', !enabled);
    const chart = ensureWalletChart();
    const processed = Array.isArray(history)
      ? history
          .map((entry) => {
            const ts = Number(entry.timestamp);
            const balance = Number(entry.balance);
            if (!Number.isFinite(ts) || !Number.isFinite(balance) || balance === 0) {
              return null;
            }
            return {
              label: fmtShortDateTime.format(new Date(ts)),
              value: balance,
              formatted: typeof entry.formatted === 'string'
                ? entry.formatted
                : `${balance.toLocaleString(undefined, { maximumFractionDigits: 4 })} BDAG`,
            };
          })
          .filter(Boolean)
      : [];
    if (!processed.length) {
      if (chart) {
        chart.data.labels = [];
        chart.data.datasets[0].data = [];
        chart.data.datasets[0].meta = [];
        chart.update('none');
      }
      walletChartWrapper.classList.add('is-empty');
      if (walletChartEmpty) {
        walletChartEmpty.textContent = enabled
          ? 'Balance history will appear here once collected.'
          : 'Wallet monitoring disabled in settings.';
        walletChartEmpty.hidden = false;
      }
      drawWalletFallback(processed);
      return;
    }
    walletChartWrapper.classList.remove('is-empty');
    if (walletChartEmpty) {
      walletChartEmpty.hidden = true;
    }
    if (chart) {
      chart.data.labels = processed.map((entry) => entry.label);
      chart.data.datasets[0].data = processed.map((entry) => entry.value);
      chart.data.datasets[0].meta = processed;
      chart.update('none');
    } else {
      walletChartWrapper.classList.remove('is-empty');
      drawWalletFallback(processed);
    }
  }

  function drawWalletFallback(processed) {
    if (!walletFallbackCtx || !walletChartCanvas) return;
    const canvas = walletChartCanvas;
    const ctx = walletFallbackCtx;
    const width = canvas.width = canvas.clientWidth || 420;
    const height = canvas.height = canvas.clientHeight || 160;
    ctx.clearRect(0, 0, width, height);
    const padL = 40;
    const padR = 18;
    const padT = 20;
    const padB = 28;
    const plotW = Math.max(10, width - padL - padR);
    const plotH = Math.max(10, height - padT - padB);
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i <= 4; i++) {
      const y = padT + (plotH * i) / 4;
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
    }
    ctx.stroke();
    const points = processed && processed.length ? processed : [
      { label: 'Now', value: 0, formatted: '0 BDAG' },
    ];
    const values = points.map((p) => Number(p.value || 0));
    const maxVal = Math.max(...values, 1);
    ctx.strokeStyle = '#44f2a8';
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((point, idx) => {
      const fracX = points.length === 1 ? 1 : idx / (points.length - 1);
      const fracY = Number(point.value || 0) / maxVal;
      const x = padL + fracX * plotW;
      const y = padT + (1 - fracY) * plotH;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
      ctx.fillStyle = '#44f2a8';
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.stroke();
    ctx.fillStyle = '#e7eaf6';
    ctx.font = '11px "Segoe UI",sans-serif';
    const last = points[points.length - 1];
    ctx.fillText(last.formatted || `${Number(last.value || 0).toFixed(4)} BDAG`, padL + plotW - 6, padT + 12);
    ctx.textAlign = 'left';
    ctx.fillText('Balance (fallback)', padL, padT - 8);
  }

  function syncLinkedSettingInputs(key, source) {
    if (!settingsForm) return;
    const inputs = settingsForm.querySelectorAll(`[data-setting-key="${key}"]`);
    const rawValue = state.settings[key];
    inputs.forEach((input) => {
      if (!input || input === source) return;
      const type = input.dataset.settingType || input.type;
      if (type === 'number') {
        const value = Number(rawValue);
        input.value = Number.isFinite(value) && value >= 0 ? value : '';
      } else if (type === 'text') {
        input.value = typeof rawValue === 'string' ? rawValue : rawValue == null ? '' : String(rawValue);
      } else {
        input.checked = !!rawValue;
      }
    });
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
      const delta = Math.abs(remoteVal - localVal);
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
      label: 'Sync rate',
      color: '#44f2a8',
      background: 'rgba(68,242,168,0.18)',
      data: (metrics, length) => normalizeSeries(metrics.block_rate_series, length),
      tooltip: formatRateValue,
      tick: formatRateTick,
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
    const isIconButton = btn.classList && btn.classList.contains('icon-btn');
    if (busy) {
      if (!isIconButton && btn.dataset.originalHtml === undefined) {
        btn.dataset.originalHtml = btn.innerHTML;
      }
      if (!isIconButton && text) {
        btn.textContent = text;
      }
      if (isIconButton) {
        btn.classList.add('is-busy');
      }
      btn.disabled = true;
      btn.dataset.busy = '1';
    } else {
      if (!isIconButton && btn.dataset.originalHtml !== undefined) {
        btn.innerHTML = btn.dataset.originalHtml;
        delete btn.dataset.originalHtml;
      }
      if (isIconButton) {
        btn.classList.remove('is-busy');
      }
      btn.disabled = false;
      delete btn.dataset.busy;
    }
  }

  function getSnapshotCountdown() {
    const countdown = state.snapshotCountdown;
    return countdown && countdown.active ? countdown : null;
  }

  function clearSnapshotCountdown(options = {}) {
    const countdown = state.snapshotCountdown;
    if (!countdown) return;
    if (countdown.timer) {
      window.clearTimeout(countdown.timer);
    }
    state.snapshotCountdown = null;
    if (!options.preserveButton && countdown.btn) {
      setBusy(countdown.btn, false);
      countdown.btn.classList.remove('is-busy');
    }
    updateSnapshotButtons();
  }

  function startSnapshotCountdown({ nodeId, label, waitSeconds, reason, btn }) {
    const duration = Math.max(1, Math.ceil(Number(waitSeconds) || 1));
    clearSnapshotCountdown({ preserveButton: true });
    const entry = {
      nodeId,
      label,
      remaining: duration,
      total: duration,
      reason: reason || '',
      btn,
      timer: null,
      active: true,
    };
    state.snapshotCountdown = entry;
    if (btn) {
      setBusy(btn, true, 'Waiting…');
      btn.classList.add('is-busy');
    }
    updateSnapshotButtons();
    const tick = () => {
      const current = getSnapshotCountdown();
      if (!current || current.nodeId !== entry.nodeId) {
        return;
      }
      const remaining = current.remaining;
      const explanation =
        current.reason || 'Node uptime must reach the snapshot guardrail before capturing a clean snapshot.';
      const waitMsg = `Waiting ${remaining}s before snapshot for ${label}. ${explanation}`;
      setSnapshotStatus(waitMsg, { level: 'warn' });
      if (remaining <= 0) {
        clearSnapshotCountdown({ preserveButton: true });
        if (current.btn) {
          setBusy(current.btn, true, 'Starting…');
        }
        setSnapshotStatus(`Required uptime reached. Starting snapshot for ${label}…`, { level: 'warn' });
        void createNodeSnapshot(nodeId, current.btn, { resumeFromCountdown: true });
        return;
      }
      current.remaining -= 1;
      current.timer = window.setTimeout(tick, 1000);
    };
    tick();
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

    if (Array.isArray(wallet?.balance_history) && wallet.balance_history.length) {
      state.walletBalanceHistory = wallet.balance_history
        .map((entry) => {
          const ts = Number(entry.timestamp);
          const balance = Number(entry.balance);
          if (!Number.isFinite(ts) || !Number.isFinite(balance)) {
            return null;
          }
          return {
            timestamp: ts * 1000,
            balance,
            formatted: entry.formatted || `${balance.toLocaleString(undefined, { maximumFractionDigits: 4 })} BDAG`,
          };
        })
        .filter(Boolean)
        .sort((a, b) => a.timestamp - b.timestamp);
    } else {
      state.walletBalanceHistory = [];
    }
    updateWalletChart(state.walletBalanceHistory, { enabled });

    let total24h = null;
    if (hasWallet && state.walletBalanceHistory.length) {
      const dayMs = 24 * 60 * 60 * 1000;
      const cutoffMs = Date.now() - dayMs;
      const windowSamples = state.walletBalanceHistory.filter((entry) => entry.timestamp >= cutoffMs);
      if (windowSamples.length >= 2) {
        const first = windowSamples[0];
        const last = windowSamples[windowSamples.length - 1];
        total24h = last.balance - first.balance;
      } else if (windowSamples.length === 1) {
        total24h = 0;
      }
    }

    if (walletTotalValue) {
      if (Number.isFinite(total24h)) {
        const absVal = Math.abs(total24h);
        const formatted = absVal.toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        });
        const sign = total24h > 0 ? '+' : total24h < 0 ? '-' : '';
        walletTotalValue.textContent = `${sign}${formatted} BDAG`;
      } else {
        walletTotalValue.textContent = '—';
      }
    }

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
    const countdown = getSnapshotCountdown();
    const jobActive = !!(job && job.active) || Boolean(countdown);
    const countdownNode = countdown ? countdown.nodeId : null;
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
      const manualBusy = btn.dataset.busy === '1';
      if (manualBusy) {
        btn.classList.add('is-busy');
        disabled = true;
      } else {
        btn.classList.remove('is-busy');
      }
      const manualRestoreBusy = restoreBtn && restoreBtn.dataset.busy === '1';
      if (restoreBtn) {
        if (manualRestoreBusy) {
          restoreBtn.classList.add('is-busy');
        } else {
          restoreBtn.classList.remove('is-busy');
        }
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
      if (countdown) {
        disabled = true;
        if (countdownNode && countdownNode === nodeId) {
          const waitText = countdown.remaining > 0 ? `${countdown.remaining}s` : 'a moment';
          const reason = countdown.reason || 'Waiting for node uptime guardrail';
          title = `Snapshot pending (${waitText}) — ${reason}`;
          btn.classList.add('is-busy');
        } else if (!jobActive || (job && job.active && jobNode === nodeId)) {
          title = 'Snapshot waiting for guardrail';
        }
      }
      btn.disabled = disabled;
      btn.title = title;
      btn.setAttribute('aria-label', title);
      if (restoreBtn) {
        if (countdown) {
          restoreBtn.disabled = true;
        }
        const pendingTs = Number(restoreBtn.dataset.restorePending || 0);
        const pendingActive =
          Number.isFinite(pendingTs) && pendingTs > 0 && Date.now() - pendingTs < 8000;
        const isRestoreJob =
          jobActive &&
          jobNode &&
          jobNode === nodeId &&
          (jobDetails.mode === 'restore' || restoreBtn.dataset.restoreBusy === '1');
        restoreBtn.disabled = !!jobActive || pendingActive;
        if (isRestoreJob) {
          if (restoreBtn.dataset.busy !== '1') {
            setBusy(restoreBtn, true);
          }
          restoreBtn.dataset.restoreBusy = '1';
          restoreBtn.classList.add('is-busy');
          delete restoreBtn.dataset.restorePending;
        } else if (pendingActive) {
          if (restoreBtn.dataset.busy !== '1') {
            setBusy(restoreBtn, true);
          }
          restoreBtn.classList.add('is-busy');
        } else if (restoreBtn.dataset.restoreBusy === '1') {
          setBusy(restoreBtn, false);
          delete restoreBtn.dataset.restoreBusy;
          delete restoreBtn.dataset.restorePending;
          restoreBtn.classList.remove('is-busy');
        } else if (!jobActive) {
          restoreBtn.classList.remove('is-busy');
          if (restoreBtn.dataset.busy === '1') {
            setBusy(restoreBtn, false);
          }
          delete restoreBtn.dataset.restoreBusy;
          delete restoreBtn.dataset.restorePending;
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
    const countdown = getSnapshotCountdown();
    const jobActive = (job && job.active) || Boolean(countdown);
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

  function ensureSnapshotState() {
    if (!state.snapshots) {
      state.snapshots = { items: [], locations: [], dir: '', job: null };
    }
    return state.snapshots;
  }

  function primeSnapshotJob(job, fallback = {}) {
    const snapshotsState = ensureSnapshotState();
    const fallbackNode = fallback.nodeId || fallback.node || null;
    const mode = fallback.mode || 'snapshot';
    const incoming = job && typeof job === 'object'
      ? { ...job, details: { ...(job.details || {}) } }
      : null;
    if (incoming) {
      snapshotsState.job = incoming;
    }
    const isActive = incoming ? incoming.active !== false : true;
    if (!isActive) {
      return incoming;
    }
    const prepared = incoming || {
      active: true,
      status: 'running',
      message: mode === 'restore' ? 'Snapshot restore running…' : 'Snapshot running…',
      details: {},
    };
    if (fallbackNode && !prepared.details.node) {
      prepared.details.node = fallbackNode;
    }
    if (!prepared.details.mode) {
      prepared.details.mode = mode;
    }
    snapshotsState.job = prepared;
    updateSnapshotButtons();
    return prepared;
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
        automation: payload.automation || null,
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

  async function createNodeSnapshot(nodeId, btn, options = {}) {
    if (!nodeId) return;
    const { resumeFromCountdown = false } = options;
    if (!resumeFromCountdown) {
      clearSnapshotCountdown();
    }
    const activeJob = state.snapshots && state.snapshots.job && state.snapshots.job.active;
    if (activeJob) {
      setSnapshotStatus('A snapshot job is already in progress.', { level: 'warn' });
      updateSnapshotButtons();
      return;
    }
    const alreadyBusy = btn && btn.dataset.busy === '1';
    if (btn && !alreadyBusy) {
      setBusy(btn, true, 'Starting…');
    }
    let countdownStarted = false;
    try {
      const entry = state.nodes.get(nodeId);
      const label = entry && entry.meta ? (entry.meta.label || entry.meta.id || nodeId) : nodeId;
      let overlayNote = '';
      const overlayState = state.overlayStatus;
      if (overlayState && overlayState.byNode) {
        let overlayMap = overlayState.byNode;
        if (overlayMap && typeof overlayMap.get !== 'function') {
          overlayMap = new Map(Object.entries(overlayMap || {}));
        }
        if (overlayMap && typeof overlayMap.get === 'function') {
          const overlaysForNode = overlayMap.get(nodeId);
          if (Array.isArray(overlaysForNode) && overlaysForNode.length) {
            const overlayParts = overlaysForNode.map((info) => {
              const used = Number.isFinite(info.upperBytes) ? formatBytes(info.upperBytes) : 'active';
              const limitSuffix = Number.isFinite(info.limitBytes) && info.limitBytes > 0
                ? ` / ${formatBytes(info.limitBytes)} limit`
                : '';
              return `${info.name} (${used}${limitSuffix})`;
            });
            overlayNote = ` — flushing overlays: ${overlayParts.join(', ')}`;
          }
        }
      }
      setSnapshotStatus(`Snapshot requested for ${label}${overlayNote}`);
      const res = await fetch('/api/snapshots/create', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ node: nodeId, quiesce_overlay: true }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        const error = new Error(payload && payload.error ? payload.error : `HTTP ${res.status}`);
        if (payload && payload.failure) {
          error.responseFailure = payload.failure;
        }
        if (payload && Object.prototype.hasOwnProperty.call(payload, 'wait_seconds')) {
          error.waitSeconds = payload.wait_seconds;
        }
        throw error;
      }
      if (payload.job) {
        ensureSnapshotState().job = payload.job;
      }
      if (!payload.job || (payload.job && payload.job.active !== false)) {
        primeSnapshotJob(payload.job, { nodeId, mode: 'snapshot' });
      }
      clearSnapshotCountdown({ preserveButton: true });
      const message = payload.message || `Snapshot started for ${label}`;
      setSnapshotStatus(message, { level: 'warn' });
      await loadSnapshots({ silent: true });
    } catch (err) {
      const failure = err && err.responseFailure ? err.responseFailure : null;
      const waitCandidate = err && err.waitSeconds !== undefined ? Number(err.waitSeconds) : null;
      const waitSeconds = Number.isFinite(waitCandidate) ? Math.max(0, waitCandidate) : null;
      if (failure === 'uptime-too-low' && waitSeconds !== null) {
        const entry = state.nodes.get(nodeId);
        const label = entry && entry.meta ? (entry.meta.label || entry.meta.id || nodeId) : nodeId;
        countdownStarted = true;
        startSnapshotCountdown({
          nodeId,
          label,
          waitSeconds,
          reason: err && err.message ? err.message : 'Waiting for node uptime guardrail.',
          btn,
        });
        return;
      }
      setSnapshotStatus(err && err.message ? err.message : 'Failed to start snapshot', { level: 'error' });
    } finally {
      if (btn && !countdownStarted) {
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
    let keepBusy = false;
    if (btn) {
      setBusy(btn, true);
      btn.dataset.restorePending = Date.now().toString();
    }
    try {
      const entry = state.nodes.get(nodeId);
      const label = entry && entry.meta ? (entry.meta.label || entry.meta.id || nodeId) : nodeId;
      setSnapshotStatus(`Restore requested for ${label}`);
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
        ensureSnapshotState().job = payload.job;
        if (payload.job.active !== false) {
          keepBusy = true;
        }
      }
      const shouldPrime = !payload.job || (payload.job && payload.job.active !== false);
      if (shouldPrime) {
        primeSnapshotJob(payload.job, { nodeId, mode: 'restore' });
        keepBusy = true;
      }
      const message = payload.message || `Snapshot restore started for ${label}`;
      setSnapshotStatus(message, { level: 'warn' });
      await loadSnapshots({ silent: true });
    } catch (err) {
      setSnapshotStatus(err && err.message ? err.message : 'Failed to start restore', { level: 'error' });
    } finally {
      if (btn) {
        if (keepBusy) {
          btn.dataset.restoreBusy = '1';
          delete btn.dataset.restorePending;
        } else {
          delete btn.dataset.restoreBusy;
          delete btn.dataset.restorePending;
          setBusy(btn, false);
        }
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
    const outcome = { forced: false, reason: '' };
    if (!running) {
      return outcome;
    }
    const localSeries = Array.isArray(stats.local) ? stats.local.map(numberOrZero) : [];
    const remoteSeries = Array.isArray(stats.remote) ? stats.remote.map(numberOrZero) : [];
    const recentLocal = recentWindow(localSeries, stats.local_height);
    const recentRemote = recentWindow(remoteSeries, stats.remote_height);
    const zeroedRecentLocal = recentLocal.every((value) => value <= 0);
    const localHeight = numberOrZero(stats.local_height);
    const remoteHeight = numberOrZero(stats.remote_height);
    const remotePositive = recentRemote.some((value) => value > 0) || remoteHeight > 0;
    const peers = numberOrZero(stats.peers);
    const uptime = numberOrZero(stats.uptime_seconds);
    const blockRate = numberOrZero(stats.block_rate_per_sec);
    const prevProgressValue = numberOrZero(previousProgress);
    const syncProgressValue = numberOrZero(stats.sync_progress);
    const hadSeriesProgress = localSeries.some((value) => value > 0);
    const hadProgress = hadSeriesProgress || prevProgressValue > 0 || syncProgressValue > 0;
    const zeroLocalNow = localHeight <= 0;
    const zeroTrend = zeroedRecentLocal || zeroLocalNow;

    const stallByReset = hadProgress && zeroLocalNow && remotePositive && peers <= 0;
    if (stallByReset) {
      return { forced: true, reason: 'Container running but height reset and no peers detected.' };
    }

    const STALL_UPTIME = 180;
    if (zeroLocalNow && remotePositive && peers <= 0 && uptime >= STALL_UPTIME) {
      return { forced: true, reason: 'No peers and zero height for several minutes.' };
    }

    const QUICK_STALL = 90;
    const stallByRate = remotePositive && peers <= 0 && zeroTrend && blockRate <= 0 && uptime >= QUICK_STALL;
    if (stallByRate) {
      return { forced: true, reason: 'No block production while remote height is advancing.' };
    }
    return outcome;
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
  const { forceOffline = false, reason = '' } = options;
  let detail = (stats.health_detail || stats.health_text || '').toString().trim();
  let display = 'Offline';
  let code = 'offline';
  if (running) {
    if (forceOffline) {
      display = 'Stalled';
      code = 'warn';
      if (!detail && reason) {
        detail = reason;
      }
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
      system: {
        title: 'System Resources',
        desc: 'CPU, RAM, and disk usage on the local host running the manager.',
      },
      settings: {
        title: 'Settings',
        desc: 'Configure automatic recovery and display preferences for the fleet.',
      },
      overclock: {
        title: 'Overclock',
        desc: 'Apply NVMe/CPU/filesystem tweaks to speed up sync on bare metal.',
      },
      snapshots: {
        title: 'Snapshots',
        desc: 'Latest archived snapshots for quick recovery.',
      },
      wallet: {
        title: 'Wallet',
        desc: 'Wallet address, balance, and recent history collected from rpc.awakening.bdagscan.com.',
      },
      launchpad: {
        title: 'Launchpad',
        desc: 'Launch new nodes with guided steps and automatic port assignment.',
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
  if (activeView === 'settings') {
    if (state.automationLogs.expanded) {
      startAutomationLogPolling();
    }
  } else {
    stopAutomationLogPolling();
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
    updateAutoSnapshotState(merged);
    updateMemoryRestartState(merged);
  }
  if (saveSettingsBtn) {
    saveSettingsBtn.disabled = true;
  }
  updateSettingsStatus('');
  // Also apply Overclock preferences to Overclock form
  try {
    if (typeof merged.overclock_cpu === 'boolean' && ocCpu) ocCpu.checked = !!merged.overclock_cpu;
    if (typeof merged.overclock_nvme_latency === 'boolean' && ocNvmeLatency) ocNvmeLatency.checked = !!merged.overclock_nvme_latency;
    if (typeof merged.overclock_scheduler === 'boolean' && ocScheduler) ocScheduler.checked = !!merged.overclock_scheduler;
    if (typeof merged.overclock_remount === 'boolean' && ocRemount) ocRemount.checked = !!merged.overclock_remount;
    // VWC removed
    // WAL tmpfs setting removed
    // VM‑Mode removed
    if (typeof merged.overclock_overlay_bdagchain === 'boolean' && ocOverlayBdagChain) ocOverlayBdagChain.checked = !!merged.overclock_overlay_bdagchain;
  } catch (_) {}
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

  async function loadSystem() {
    if (!systemPane) return;
    try {
      const res = await fetch('/api/system', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      renderSystemStats(payload || {});
    } catch (err) {
      console.error('[system] failed to load', err);
    }
  }

  function renderSystemStats(payload) {
    if (!systemPane) return;
    const cpu = Number(payload?.cpu_percent) || 0;
    if (systemCpuValue) {
      systemCpuValue.textContent = `${cpu.toFixed(1)}%`;
    }
    if (systemCpuBar) {
      systemCpuBar.value = Math.min(100, Math.max(0, cpu));
      applyUsageColor(systemCpuBar, cpu);
    }
    const mem = payload?.memory || {};
    const memPercent = Number(mem.percent) || 0;
    if (systemMemoryValue) {
      const used = typeof mem.used === 'number' ? formatBytes(mem.used) : '—';
      const total = typeof mem.total === 'number' ? formatBytes(mem.total) : '—';
      systemMemoryValue.textContent = `${used} / ${total} (${memPercent.toFixed(1)}%)`;
    }
    if (systemMemoryBar) {
      systemMemoryBar.value = Math.min(100, Math.max(0, memPercent));
      applyUsageColor(systemMemoryBar, memPercent);
    }
    const disk = payload?.disk || {};
    const diskPercent = Number(disk.percent) || 0;
    if (systemDiskValue) {
      const used = typeof disk.used === 'number' ? formatBytes(disk.used) : '—';
      const total = typeof disk.total === 'number' ? formatBytes(disk.total) : '—';
      systemDiskValue.textContent = `${used} / ${total} (${diskPercent.toFixed(1)}%)`;
    }
    if (systemDiskBar) {
      systemDiskBar.value = Math.min(100, Math.max(0, diskPercent));
      applyUsageColor(systemDiskBar, diskPercent);
    }
    const temp = payload?.temperature;
    if (systemCpuTempValue) {
      systemCpuTempValue.textContent = formatTemperature(temp?.current);
    }
    if (systemCpuTempBar) {
      const tempValue = Number(temp?.current);
      if (Number.isFinite(tempValue)) {
        const clamped = Math.min(100, Math.max(0, tempValue));
        systemCpuTempBar.value = clamped;
        systemCpuTempBar.classList.remove('temp-progress--green', 'temp-progress--orange', 'temp-progress--red');
        if (tempValue >= 80) {
          systemCpuTempBar.classList.add('temp-progress--red');
        } else if (tempValue >= 60) {
          systemCpuTempBar.classList.add('temp-progress--orange');
        } else {
          systemCpuTempBar.classList.add('temp-progress--green');
        }
      } else {
        systemCpuTempBar.value = 0;
        systemCpuTempBar.classList.remove('temp-progress--green', 'temp-progress--orange', 'temp-progress--red');
      }
    }
  }

  function startSystemPolling() {
    stopSystemPolling();
    systemPollTimer = window.setInterval(() => {
      void loadSystem();
    }, SYSTEM_POLL_INTERVAL_MS);
  }

  function stopSystemPolling() {
    if (systemPollTimer) {
      clearInterval(systemPollTimer);
      systemPollTimer = null;
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
    void loadNodes();
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
      // Auto-fill Overclock data path from first discovered node with chain_data_dir
      try {
        const nodes = payload.nodes || [];
        const withData = nodes.find((n) => n && n.status && n.status.chain_data_dir);
        if (withData && withData.status && withData.status.chain_data_dir && ocDataPath && (!ocDataPath.value || ocDataPath.value === '/home/node/blockdag')) {
          ocDataPath.value = withData.status.chain_data_dir;
        }
      } catch (e) {
        // ignore
      }
      const nodes = Array.isArray(payload.nodes) ? payload.nodes : [];
      const stalled = nodes.reduce((count, node) => {
        if (!node || !node.id) return count;
        const stats = node.status || {};
        const rawRunning = isRunningFlag(stats.running);
        const forced = shouldForceOffline(stats, rawRunning, state.lastProgress.get(node.id));
        return forced.forced ? count + 1 : count;
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

    state.summary = summary && Object.keys(summary).length ? { ...summary } : null;

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
    let hideBadge = false;
    if (walletEnabled) {
      if (wallet.error) {
        if (typeof wallet.error === 'string' && wallet.error.toLowerCase().includes('wallet not found')) {
          hideBadge = true;
        } else {
          badgeText = wallet.error;
        }
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
    if (!hideBadge && !badgeText) {
      badgeText = 'updated';
    }
    if (!hideBadge && ts) {
      const timeText = fmtTime.format(ts);
      badgeText = badgeText ? `${badgeText} · ${timeText}` : timeText;
    }
    if (summaryBadge) {
      summaryBadge.hidden = hideBadge;
      if (hideBadge) {
        summaryBadge.textContent = '';
        summaryBadge.removeAttribute('title');
      } else {
        summaryBadge.textContent = badgeText;
        if (badgeTitle) {
          summaryBadge.title = badgeTitle;
        } else if (wallet && wallet.address) {
          summaryBadge.title = wallet.address;
        } else {
          summaryBadge.removeAttribute('title');
        }
      }
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
    const cardCount = cardsContainer ? cardsContainer.querySelectorAll('.fleet-card').length : 0;
    if (cardCount > 0) {
      state.nodesDiscovering = false;
      if (nodeDiscoveryMessage && !nodeDiscoveryMessage.dataset.removed) {
        nodeDiscoveryMessage.dataset.removed = '1';
        nodeDiscoveryMessage.remove();
      }
    }
  }

  function toggleEmptyState() {
    const hasCards = state.nodes.size > 0;
    if (emptyStateCard) {
      emptyStateCard.style.display = hasCards ? 'none' : 'block';
    }
    updateDiscoveryMessage();
  }

  function updateDiscoveryMessage() {
    if (!nodeDiscoveryMessage || nodeDiscoveryMessage.dataset.removed === '1') return;
    const cardCount = cardsContainer ? cardsContainer.querySelectorAll('.fleet-card').length : 0;
    const show = state.nodesDiscovering && cardCount === 0;
    nodeDiscoveryMessage.hidden = !show;
  }

  function getLaunchpadData() {
    const autoPorts = launchpadFields.autoPorts?.checked ?? false;
    return {
      label: launchpadFields.label?.value?.trim() || '',
      installPath: launchpadFields.installPath?.value?.trim() || '',
      p2pPort: Number(launchpadFields.p2pPort?.value) || 38130,
      rpcPort: Number(launchpadFields.rpcPort?.value) || 18545,
      autoPorts,
      walletAddress: launchpadFields.walletAddress?.value?.trim() || '',
      externalP2PPort: Number(launchpadFields.externalP2PPort?.value) || null,
      wsPort: Number(launchpadFields.wsPort?.value) || 18546,
      peerPort: Number(launchpadFields.peerPort?.value) || 18150,
      externalPeerPort: Number(launchpadFields.externalPeerPort?.value) || null,
    };
  }

  function isLaunchpadComplete(data = getLaunchpadData()) {
    const manualPortsValid =
      Number.isFinite(data.p2pPort) &&
      data.p2pPort > 0 &&
      Number.isFinite(data.rpcPort) &&
      data.rpcPort > 0 &&
      Number.isFinite(data.wsPort) &&
      data.wsPort > 0 &&
      Number.isFinite(data.peerPort) &&
      data.peerPort > 0;
    const externalValid = data.autoPorts || (Number.isFinite(data.externalP2PPort) && data.externalP2PPort > 0);
    const externalPeerValid = data.autoPorts || (Number.isFinite(data.externalPeerPort) && data.externalPeerPort > 0);
    return Boolean(data.label && data.installPath && data.walletAddress && manualPortsValid && externalValid && externalPeerValid);
  }

  function updateLaunchpadLaunchState() {
    if (!launchpadNextBtn) return;
    const data = getLaunchpadData();
    const complete = isLaunchpadComplete();
    const onFinalStep = state.launchpad.step === 3;
    const waitingForPreview = onFinalStep && data.autoPorts && state.launchpad.previewLoading;
    launchpadNextBtn.dataset.mode = onFinalStep ? 'launch' : 'next';
    if (launchpadNextLabel) {
      launchpadNextLabel.textContent = onFinalStep ? 'Launch node' : 'Next';
    }
    if (launchpadNextIcon) {
      launchpadNextIcon.hidden = !onFinalStep;
    }
    launchpadNextBtn.classList.toggle('launch-mode', onFinalStep);
    launchpadNextBtn.disabled = onFinalStep ? !complete || waitingForPreview : false;
  }

  function syncLaunchpadPortInputs(auto = launchpadFields.autoPorts?.checked ?? false) {
    const manualFields = [
      launchpadFields.p2pPort,
      launchpadFields.rpcPort,
      launchpadFields.externalP2PPort,
      launchpadFields.wsPort,
      launchpadFields.peerPort,
      launchpadFields.externalPeerPort,
    ];
    manualFields.forEach((field) => {
      if (!field) return;
      field.disabled = auto;
    });
  }

  function setSummaryField(ref, value, missingText = 'Missing') {
    if (!ref) return;
    const isMissing = !value;
    ref.textContent = isMissing ? missingText : value;
    ref.classList.toggle('missing', isMissing);
  }

  function clearLaunchpadPreviewState() {
    if (!state.launchpad) return;
    state.launchpad.previewPorts = null;
    state.launchpad.previewLoading = false;
    state.launchpad.previewError = null;
    state.launchpad.previewRequestId = 0;
  }

  function renderLaunchpadSummary(data = getLaunchpadData()) {
    if (!launchpadSummaryRefs.label) return;
    const onReviewStep = state.launchpad?.step === 3;
    const usePreview = Boolean(onReviewStep && data.autoPorts);
    const preview = usePreview ? state.launchpad?.previewPorts : null;
    const previewLoading = usePreview ? !!state.launchpad?.previewLoading : false;
    const previewError = usePreview ? state.launchpad?.previewError : null;
    const pendingText = previewError || (previewLoading ? 'Calculating…' : null);
    const resolvedP2P = usePreview ? preview?.p2pPort ?? (pendingText || data.p2pPort) : data.p2pPort;
    const resolvedRpc = usePreview ? preview?.rpcPort ?? (pendingText || data.rpcPort) : data.rpcPort;
    const resolvedWs = usePreview ? preview?.wsPort ?? (pendingText || data.wsPort) : data.wsPort;
    const resolvedPeer = usePreview ? preview?.peerPort ?? (pendingText || data.peerPort) : data.peerPort;
    const resolvedExternalP2P = data.autoPorts
      ? (usePreview ? preview?.p2pPort ?? (pendingText || data.p2pPort) : data.p2pPort)
      : data.externalP2PPort || '—';
    const resolvedExternalPeer = data.autoPorts
      ? (usePreview ? preview?.peerPort ?? (pendingText || data.peerPort) : data.peerPort)
      : data.externalPeerPort || '—';
    setSummaryField(launchpadSummaryRefs.label, data.label);
    setSummaryField(launchpadSummaryRefs.path, data.installPath);
    if (launchpadSummaryRefs.p2pPort) {
      launchpadSummaryRefs.p2pPort.textContent = resolvedP2P;
    }
    if (launchpadSummaryRefs.ws) {
      launchpadSummaryRefs.ws.textContent = resolvedWs;
    }
    if (launchpadSummaryRefs.rpcPort) {
      launchpadSummaryRefs.rpcPort.textContent = resolvedRpc;
    }
    if (launchpadSummaryRefs.wallet) {
      setSummaryField(launchpadSummaryRefs.wallet, data.walletAddress);
    }
    if (launchpadSummaryRefs.externalP2P) {
      launchpadSummaryRefs.externalP2P.textContent = resolvedExternalP2P;
    }
    if (launchpadSummaryRefs.peer) {
      launchpadSummaryRefs.peer.textContent = resolvedPeer;
    }
    if (launchpadSummaryRefs.externalPeer) {
      launchpadSummaryRefs.externalPeer.textContent = resolvedExternalPeer;
    }
    updateLaunchpadLaunchState();
  }

  async function requestLaunchpadPreview(payload) {
    const res = await fetch('/api/node-manager/launch/preview', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let errText = await res.text();
      if (errText) {
        try {
          const parsed = JSON.parse(errText);
          if (parsed && parsed.error) {
            errText = parsed.error;
          }
        } catch (err) {
          // ignore parse errors; fall back to original text
        }
      }
      throw new Error(errText || res.statusText);
    }
    return res.json();
  }

  async function requestLaunch(payload) {
    const res = await fetch('/api/node-manager/launch', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(errText || res.statusText);
    }
    return res.json();
  }

  async function refreshLaunchpadPreview() {
    if (!state.launchpad) return;
    const data = getLaunchpadData();
    if (!data.autoPorts) {
      clearLaunchpadPreviewState();
      updateLaunchpadLaunchState();
      return;
    }
    const requestId = ++launchpadPreviewSeq;
    state.launchpad.previewRequestId = requestId;
    state.launchpad.previewLoading = true;
    state.launchpad.previewPorts = null;
    state.launchpad.previewError = null;
    if (launchpadStatus && state.launchpad.step === 3) {
      launchpadStatus.textContent = 'Calculating ports…';
      launchpadStatus.classList.remove('error');
    }
    renderLaunchpadSummary(data);
    try {
      const preview = await requestLaunchpadPreview(data);
      if (state.launchpad.previewRequestId !== requestId) return;
      state.launchpad.previewPorts = preview;
      state.launchpad.previewLoading = false;
      if (launchpadStatus && launchpadStatus.textContent === 'Calculating ports…') {
        launchpadStatus.textContent = '';
      }
      renderLaunchpadSummary();
    } catch (err) {
      if (state.launchpad.previewRequestId !== requestId) return;
      state.launchpad.previewPorts = null;
      state.launchpad.previewLoading = false;
      state.launchpad.previewError = err?.message || 'Preview failed';
      if (launchpadStatus && state.launchpad.step === 3) {
        launchpadStatus.textContent = state.launchpad.previewError;
        launchpadStatus.classList.add('error');
      }
      renderLaunchpadSummary();
    }
  }

  async function handleLaunch() {
    if (!launchpadNextBtn) return;
    if (!launchpadStatus) return;
    launchpadStatus.classList.remove('error');
    launchpadStatus.textContent = 'Launching node…';
    launchpadNextBtn.disabled = true;
    try {
      const data = await requestLaunch(getLaunchpadData());
      launchpadStatus.textContent = `Started ${data.label} (P2P ${data.p2pPort}, RPC ${data.rpcPort}).`;
      const updateField = (field, value) => {
        if (field && Number.isFinite(+value)) {
          field.value = value;
        }
      };
      updateField(launchpadFields.p2pPort, data.p2pPort);
      updateField(launchpadFields.rpcPort, data.rpcPort);
      updateField(launchpadFields.wsPort, data.wsPort);
      updateField(launchpadFields.peerPort, data.peerPort);
      if (launchpadFields.externalP2PPort && launchpadFields.autoPorts?.checked) {
        launchpadFields.externalP2PPort.value = data.p2pPort;
      }
      if (launchpadFields.externalP2PPort && launchpadFields.autoPorts?.checked) {
        launchpadFields.externalP2PPort.value = data.p2pPort;
      }
      if (launchpadFields.externalPeerPort && launchpadFields.autoPorts?.checked) {
        launchpadFields.externalPeerPort.value = data.peerPort;
      }
      await discoverNodes({ auto: true });
    } catch (err) {
      launchpadStatus.classList.add('error');
      launchpadStatus.textContent = err?.message || 'Launch failed';
    } finally {
      updateLaunchpadLaunchState();
    }
  }

  function getLaunchpadFieldValue(field) {
    if (!field) return '';
    return field.value?.trim?.() || '';
  }

  function canAdvanceStep(step) {
    if (step === 1) {
      return Boolean(
        getLaunchpadFieldValue(launchpadFields.label) &&
          getLaunchpadFieldValue(launchpadFields.installPath) &&
          getLaunchpadFieldValue(launchpadFields.walletAddress) &&
          getLaunchpadFieldValue(launchpadFields.externalP2PPort) &&
          getLaunchpadFieldValue(launchpadFields.wsPort) &&
          getLaunchpadFieldValue(launchpadFields.peerPort) &&
          getLaunchpadFieldValue(launchpadFields.externalPeerPort)
      );
    }
    if (step === 2) {
      const data = getLaunchpadData();
      const manualPortsValid = Number.isFinite(data.p2pPort) && data.p2pPort > 0 && Number.isFinite(data.rpcPort) && data.rpcPort > 0;
      return Boolean(data.autoPorts || manualPortsValid);
    }
    return true;
  }

  function setLaunchpadStep(step) {
    if (!launchpadSections[step]) return;
    const previousStep = state.launchpad.step;
    state.launchpad.step = step;
    Object.entries(launchpadSections).forEach(([key, section]) => {
      if (!section) return;
      section.hidden = Number(key) !== step;
    });
    launchpadStepsContainer?.querySelectorAll('[data-step-chip]')?.forEach((chip) => {
      const chipStep = Number(chip.dataset.stepChip);
      chip.classList.toggle('is-active', chipStep === step);
    });
    if (launchpadBackBtn) launchpadBackBtn.disabled = step === 1;
    if (step !== 3) {
      clearLaunchpadPreviewState();
    }
    if (launchpadStatus && (step === 3 || previousStep === 3)) {
      launchpadStatus.textContent = '';
      launchpadStatus.classList.remove('error');
    }
    if (step === 3) {
      renderLaunchpadSummary();
      refreshLaunchpadPreview();
    } else {
      renderLaunchpadSummary();
    }
    updateLaunchpadLaunchState();
  }

  function changeLaunchpadStep(direction) {
    const next = state.launchpad.step + direction;
    if (next < 1 || next > 3) return;
    if (direction > 0 && !canAdvanceStep(state.launchpad.step)) {
      renderLaunchpadSummary();
      return;
    }
    setLaunchpadStep(next);
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
    if (nameEl) {
      nameEl.textContent = node.id || 'Node';
    }

    const summary = details.querySelector('summary.fleet-summary');
    if (summary) {
      summary.setAttribute('title', 'Click to expand');
      details.addEventListener('toggle', () => {
        summary.setAttribute('title', details.open ? 'Click to collapse' : 'Click to expand');
        const entry = state.nodes.get(node.id);
        if (entry) {
          entry.state = entry.state || {};
          entry.state.cardOpen = details.open;
        }
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
    state.nodes.set(node.id, { card: details, meta: node, state: {} });
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
    const previousState = entry.state || {};
    entry.meta = node;
    entry.state = previousState;

    const card = entry.card;
    if (card && !previousState.cardOpen) {
      card.open = false;
      card.removeAttribute('open');
    }
    const panelEl = card?.querySelector?.('[data-role="logs-panel"]');
    const toggleEl = card?.querySelector?.('[data-role="logs-toggle"]');
    const wrapperEl = card?.querySelector?.('.node-logs');

    const nameEl = card?.querySelector?.('.node-name');

    if (previousState.cardOpen && card) {
      card.open = true;
      card.setAttribute('open', '');
    } else if (card && !previousState.cardOpen) {
      card.open = false;
      card.removeAttribute('open');
    }

    if (panelEl) {
      if (previousState.logsOpen) {
        if (panelEl.hasAttribute('hidden')) {
          panelEl.removeAttribute('hidden');
        }
        toggleEl?.setAttribute('aria-expanded', 'true');
        wrapperEl?.classList.add('is-open');
        startLogPolling(node.id, card);
        void loadNodeLogs(node.id, card, { force: true, silent: true });
      } else {
        if (!panelEl.hasAttribute('hidden')) {
          panelEl.setAttribute('hidden', '');
        }
        toggleEl?.setAttribute('aria-expanded', 'false');
        wrapperEl?.classList.remove('is-open');
        stopLogPolling(node.id);
      }
    }

    entry.state.cardOpen = Boolean(card && card.open);
    entry.state.logsOpen = Boolean(panelEl && !panelEl.hasAttribute('hidden'));
    if (nameEl) {
      nameEl.textContent = node.id || 'Node';
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
    const forcedHeader = shouldForceOffline(
      stats,
      containerRunning,
      state.lastProgress.get(node.id)
    );
    const health = resolveHealth(stats, containerRunning, {
      forceOffline: forcedHeader.forced,
      reason: forcedHeader.reason,
    });
    const displayHealth = health.display;
    const code = health.code;
    const healthDetail = health.detail;
    if (statusEl) {
      statusEl.textContent = '';
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
    setPeerId(card, stats.peer_id, stats.peer_ports);
    updateUptime(card, stats.uptime_seconds);
    updateStartStopButton(card.querySelector('[data-role="toggle"]'), containerRunning, {
      effectiveRunning,
      forcedOffline: forcedHeader.forced,
    });
    if (entry.meta && entry.meta.status) {
      entry.meta.status.container_running = containerRunning;
      entry.meta.status.forced_offline = forcedHeader.forced;
      entry.meta.status.stalled_reason = forcedHeader.reason || '';
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

  function setPeerId(card, value, ports) {
    const el = card.querySelector('.stat-peer-id');
    if (!el) return;
    const internalPort = ports && (Number.isFinite(Number(ports.internal)) ? Number(ports.internal) : null);
    const externalPort = ports && (ports.external !== undefined && ports.external !== null && ports.external !== ''
      ? Number(ports.external)
      : null);
    el.dataset.peerInternal = internalPort && Number.isFinite(internalPort) ? String(internalPort) : '';
    el.dataset.peerExternal = Number.isFinite(externalPort) ? String(externalPort) : '';
    const text = typeof value === 'string' ? value.trim() : '';
    if (!text) {
      el.textContent = '—';
      const tooltipParts = [];
      if (el.dataset.peerInternal) {
        tooltipParts.push(`Internal peer port: ${el.dataset.peerInternal}`);
      }
      if (el.dataset.peerExternal) {
        tooltipParts.push(`External peer port: ${el.dataset.peerExternal}`);
      }
      if (tooltipParts.length) {
        el.title = tooltipParts.join('\n');
      } else {
        el.removeAttribute('title');
      }
      return;
    }
    const clean = text.replace(/[^0-9a-z]/gi, '').toLowerCase();
    const tooltipParts = [clean];
    if (el.dataset.peerInternal) {
      tooltipParts.push(`Internal peer port: ${el.dataset.peerInternal}`);
    }
    if (el.dataset.peerExternal) {
      tooltipParts.push(`External peer port: ${el.dataset.peerExternal}`);
    }
    if (clean.length >= 14) {
      el.textContent = `${clean.slice(0, 6)}…${clean.slice(-6)}`;
      el.title = tooltipParts.join('\n');
      return;
    }
    const short = text.length > 14 ? `${text.slice(0, 6)}…${text.slice(-6)}` : text;
    el.textContent = short;
    el.title = tooltipParts.join('\n');
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
    const entry = state.nodes.get(nodeId);
    if (entry) {
      entry.state = entry.state || {};
      entry.state.logsOpen = !panel.hasAttribute('hidden');
      entry.state.cardOpen = card?.open || false;
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
    const forcedState = shouldForceOffline(status, containerRunning, previousProgress);
    const forcedOffline = forcedState.forced;
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
    state.nodesDiscovering = true;
    updateDiscoveryMessage();
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
      state.nodesDiscovering = false;
      updateDiscoveryMessage();
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
    let forcedCount = 0;
    let onlineCount = 0;
    let runningCount = 0;
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
      if (containerRunning) {
        runningCount += 1;
      }
      const effectiveRunning = isRunningFlag(metrics.running);
      const summaryHealthChip = card.querySelector('.summary-health-chip');
      const previousProgress = state.lastProgress.get(nodeId);
      const forcedState = shouldForceOffline(metrics, containerRunning, previousProgress);
      const health = resolveHealth(metrics, containerRunning, {
        forceOffline: forcedState.forced,
        reason: forcedState.reason,
      });
      if (forcedState.forced) {
        forcedCount += 1;
      }
      const displayHealth = health.display;
      const healthDetail = health.detail;
      const code = health.code;
      if (code === 'online') {
        onlineCount += 1;
      }
      const nodeStatusEl = card.querySelector('.node-status');
      if (nodeStatusEl) {
        nodeStatusEl.classList.toggle('is-ok', code === 'online');
        nodeStatusEl.classList.toggle('is-warn', code !== 'online');
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
        forcedOffline: forcedState.forced,
      });
      setPeerId(card, metrics.peer_id, metrics.peer_ports);
      entry.meta.status = {
        ...(entry.meta.status || {}),
        ...metrics,
        container_running: containerRunning,
        forced_offline: forcedState.forced,
        stalled_reason: forcedState.reason || '',
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
    updateSummaryCounters({
      forcedCount,
      onlineCount,
      runningCount,
      totalCount: state.nodes.size,
    });
  }

  function updateSummaryCounters({
    forcedCount = 0,
    onlineCount = 0,
    runningCount = Number.isFinite(onlineCount) ? onlineCount : 0,
    totalCount = state.nodes.size,
  }) {
    const stalled = Number.isFinite(forcedCount) ? Math.max(0, forcedCount) : 0;
    const count = Number.isFinite(totalCount) ? Math.max(0, totalCount) : state.nodes.size;
    const online = Number.isFinite(onlineCount) ? Math.min(count, Math.max(0, onlineCount)) : 0;
    const running = Number.isFinite(runningCount) ? Math.min(count, Math.max(0, runningCount)) : 0;
    const summary = state.summary ? { ...state.summary } : {};
    summary.count = count;
    summary.running = online;
    summary.running_count = running;
    summary.offline = Math.max(0, count - running);
    summary.stalled = stalled;
    if (!('timestamp' in summary) || summary.timestamp == null) {
      summary.timestamp = Date.now() / 1000;
    }
    renderSummary(summary);
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
    Object.entries(launchpadFields).forEach(([key, field]) => {
      if (!field) return;
      const handler = () => {
        if (key === 'autoPorts') {
          syncLaunchpadPortInputs();
        }
        renderLaunchpadSummary();
      };
      const events = key === 'autoPorts' ? ['change'] : ['input', 'change'];
      events.forEach((eventName) => field.addEventListener(eventName, handler));
    });
    syncLaunchpadPortInputs();
    if (launchpadBackBtn) {
      launchpadBackBtn.addEventListener('click', () => changeLaunchpadStep(-1));
    }
    if (launchpadNextBtn) {
      launchpadNextBtn.addEventListener('click', async () => {
        const mode = launchpadNextBtn.dataset.mode || 'next';
        if (mode === 'launch') {
          if (!isLaunchpadComplete()) {
            renderLaunchpadSummary();
            return;
          }
          await handleLaunch();
        } else {
          changeLaunchpadStep(1);
        }
      });
    }
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
          syncLinkedSettingInputs(key, target);
          markSettingsDirty();
          updateAutoRestartCooldownState(state.settings);
          updateAutoSnapshotState(state.settings);
          updateMemoryRestartState(state.settings);
        }
      });
    }
    if (saveSettingsBtn) {
      saveSettingsBtn.addEventListener('click', () => {
        void saveSettings();
      });
    }
    if (overclockForm) {
      const setOCStatus = (text, kind='') => {
        if (!overclockStatus) return;
        overclockStatus.textContent = text || '';
        overclockStatus.classList.remove('is-error','is-success');
        if (kind === 'error') overclockStatus.classList.add('is-error');
        if (kind === 'success') overclockStatus.classList.add('is-success');
      };
      // VWC UI removed
      const parseIops = (value) => {
        if (!value) return null;
        const v = String(value).trim().toLowerCase().replace('iops','').trim();
        const m = v.match(/([0-9]+(?:\.[0-9]+)?)\s*([kmg])?/);
        if (!m) return Number.parseFloat(v) || null;
        let num = Number.parseFloat(m[1]);
        const unit = m[2];
        if (unit === 'k') num *= 1e3; else if (unit === 'm') num *= 1e6; else if (unit === 'g') num *= 1e9;
        return num;
      };
      const ensureOcChart = () => {
        if (ocChart) return ocChart;
        const canvas = document.getElementById('overclockVerifyChart');
        if (!canvas) return null;
        if (typeof Chart !== 'function') {
          if (ocChartEmpty) ocChartEmpty.textContent = 'Chart unavailable (library not loaded)';
          return null;
        }
        ocChart = new Chart(canvas.getContext('2d'), {
          type: 'line',
          data: { labels: [], datasets: [
            { label: 'IOPS', data: [], borderColor: '#44f2a8', tension: 0.2, yAxisID: 'y' },
            { label: 'p50 (us)', data: [], borderColor: '#55aaff', tension: 0.2, yAxisID: 'y1' },
          ]},
          options: { responsive: true, animation: false, scales: { y: { position: 'left' }, y1: { position: 'right' } } }
        });
        return ocChart;
      };
      const updateOcChart = (metrics) => {
        const chart = ensureOcChart();
        const ts = new Date();
        const iops = parseIops(metrics.iops);
        const p50 = metrics.p50 ? Number((metrics.p50.match(/([0-9.]+)/)||[])[1]) : null;
        ocHistory.push({ ts, iops, p50 });
        const labels = ocHistory.map(x => fmtTime.format(x.ts));
        const iopsData = ocHistory.map(x => x.iops);
        const p50Data = ocHistory.map(x => x.p50);
        if (chart && typeof chart.update === 'function' && (Number.isFinite(iops) || Number.isFinite(p50))) {
          chart.data.labels = labels;
          chart.data.datasets[0].data = iopsData;
          chart.data.datasets[1].data = p50Data;
          chart.update();
        } else {
          drawFallbackChart(labels, iopsData, p50Data);
        }
        if (ocChartEmpty && (Number.isFinite(iops) || Number.isFinite(p50))) ocChartEmpty.hidden = true;
      };
      // Expose to init() so auto-prime can push a point
      ocAppendMetric = updateOcChart;
      // Preflight VWC removed
      const collectOverclockPayload = () => ({
        cpu: !!ocCpu?.checked,
        nvme_latency: !!ocNvmeLatency?.checked,
        scheduler: !!ocScheduler?.checked,
        remount: !!ocRemount?.checked,
      });

      async function applyOverclockTweaks() {
        try {
          setOCStatus('Applying…');
          openOcLogs();
          updateOcLayout();
          const payload = collectOverclockPayload();
          const res = await fetch('/api/overclock/apply', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(payload),
          });
          const json = await res.json().catch(() => ({}));
          if (!res.ok || json.ok === false) {
            const msg = json.error || `HTTP ${res.status}`;
            setOCStatus(msg, 'error');
            return;
          }
          try {
            const pref = {
              overclock_cpu: payload.cpu,
              overclock_nvme_latency: payload.nvme_latency,
              overclock_scheduler: payload.scheduler,
              overclock_remount: payload.remount,
            };
            void fetch('/api/settings', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify(pref),
            }).catch(() => {});
          } catch (_) {}
          let msg = 'Tweaks applied';
          if (json.needs_root) msg = 'Needs root: ' + (json.hint || 'run via sudo');
          setOCStatus(msg, json.needs_root ? '' : 'success');
          try {
            const sres = await fetch('/api/overclock/status', { cache: 'no-store' });
            if (sres.ok) {
              const st = await sres.json();
              if (st && st.status_line) setOCStatus(st.status_line);
            }
          } catch (_) {}
        } catch (err) {
          console.error('[overclock] failed', err);
          setOCStatus(err.message || 'Failed to apply', 'error');
        }
      }

      let autoApplyTimer = null;
      function scheduleAutoApply() {
        if (autoApplyTimer) {
          clearTimeout(autoApplyTimer);
        }
        autoApplyTimer = window.setTimeout(() => {
          autoApplyTimer = null;
          void applyOverclockTweaks();
        }, 200);
      }

      if (ocCpu) ocCpu.addEventListener('change', scheduleAutoApply);
      if (ocNvmeLatency) ocNvmeLatency.addEventListener('change', scheduleAutoApply);
      if (ocScheduler) ocScheduler.addEventListener('change', scheduleAutoApply);
      if (ocRemount) ocRemount.addEventListener('change', scheduleAutoApply);
      const verifyBtn = document.getElementById('btnVerifyOverclock');
      if (verifyBtn) {
        verifyBtn.addEventListener('click', async () => {
          try {
            setOCStatus('Running DB tests (10s)…');
            openOcLogs();
            updateOcLayout();
            const res = await fetch('/api/overclock/verify-dbs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ runtime: 10 }) });
            const json = await res.json().catch(() => ({}));
            if (!res.ok || json.ok === false) {
              // Fallback to single test
              const r2 = await fetch('/api/overclock/verify', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({}) });
              const j2 = await r2.json().catch(() => ({}));
              if (!r2.ok || j2.ok === false) {
                setOCStatus((json.error || j2.error || `Test failed (HTTP ${res.status})`), 'error');
                return;
              }
              const m = j2.metrics || {};
              const parts = [];
              if (m.iops) parts.push(`${m.iops} iops`);
              if (m.bw) parts.push(`${m.bw}`);
              if (m.fsync_p50 || m.fsync_p99) parts.push(`fsync p50 ${m.fsync_p50 || '—'} p99 ${m.fsync_p99 || '—'}`);
              if (m.write_p50 || m.write_p99) parts.push(`write p50 ${m.write_p50 || '—'} p99 ${m.write_p99 || '—'}`);
              setOCStatus(parts.length ? `Test: ${parts.join(' | ')}` : 'Test complete', 'success');
              return;
            }
            const resmap = json.results || {};
            const parts = [];
            for (const [name, m] of Object.entries(resmap)) {
              const seg = [];
              if (m.iops) seg.push(`${m.iops} iops`);
              if (m.bw) seg.push(`${m.bw}`);
              if (m.fsync_p50 || m.fsync_p99) seg.push(`fsync p50 ${m.fsync_p50 || '—'} p99 ${m.fsync_p99 || '—'}`);
              if (m.write_p50 || m.write_p99) seg.push(`write p50 ${m.write_p50 || '—'} p99 ${m.write_p99 || '—'}`);
              parts.push(`${name}: ${seg.join(' | ')}`);
            }
            setOCStatus(parts.length ? `DB Tests: ${parts.join(' ; ')}` : 'Test complete', 'success');
            // Also show current status
            try {
              const sres = await fetch('/api/overclock/status', { cache: 'no-store' });
              if (sres.ok) {
                const st = await sres.json();
                if (st && st.status_line) setOCStatus(st.status_line);
              }
            } catch (_) {}
          } catch (err) {
            console.error('[overclock test] failed', err);
            setOCStatus(err.message || 'Test failed', 'error');
          }
        });
      }
      const revertBtn = document.getElementById('btnRevertOverclock');
      if (revertBtn) {
        revertBtn.addEventListener('click', async () => {
          try {
            setOCStatus('Reverting…');
            openOcLogs();
            updateOcLayout();
            const payload = { };
            const res = await fetch('/api/overclock/revert', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
            const json = await res.json().catch(() => ({}));
            if (!res.ok || json.ok === false) {
              setOCStatus(json.error || `HTTP ${res.status}`, 'error');
              return;
            }
            let msg = 'Reverted to defaults';
            if (json.needs_root) msg = 'Needs root: ' + (json.hint || 'run via sudo');
            setOCStatus(msg, json.needs_root ? '' : 'success');
            // Refresh preflight after revert
            setTimeout(() => { void preflight(); }, 500);
            // Refresh status
            try {
              const sres = await fetch('/api/overclock/status', { cache: 'no-store' });
              if (sres.ok) {
                const st = await sres.json();
                if (st && st.status_line) setOCStatus(st.status_line);
              }
            } catch (_) {}
          } catch (err) {
            console.error('[overclock revert] failed', err);
            setOCStatus(err.message || 'Revert failed', 'error');
          }
        });
      }
      // Overclock logs auto-refresh; manual refresh removed
      // WAL checkpoint UI removed

      // VM‑Mode removed

      // WAL config UI removed

      // Redeploy helper
      const btnWalRedeploy = document.getElementById('btnWalRedeploy');
      const ocWalImageTag = document.getElementById('ocWalImageTag');
      // Redeploy helper removed

      // OverlayFS toggle (BdagChain)
      if (ocOverlayBdagChain) {
        ocOverlayBdagChain.addEventListener('change', async () => {
          try {
            if (ocOverlayBdagChain.checked) {
              setOCStatus('Applying OverlayFS to all BdagChain…');
              openOcLogs();
              updateOcLayout();
              try { await fetch('/api/settings', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ overclock_overlay_bdagchain: true })}); } catch(_) {}
              const interval = Number(ocOverlayIntervalBdagChain?.value || 30);
              const limitGiB = Number(ocOverlayLimitBdagChain?.value || 3);
              const res = await fetch('/api/overclock/overlay/align', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ interval_bdagchain: interval, limit_bdagchain_gib: limitGiB }) });
              const j = await res.json().catch(()=>({}));
              if (!res.ok || j.ok === false) throw new Error(j.error || `HTTP ${res.status}`);
              setOCStatus(`OverlayFS active across ${j.count||0} targets for BdagChain`, 'success');
            } else {
              setOCStatus('Reverting OverlayFS for all BdagChain…');
              openOcLogs();
              updateOcLayout();
              try { await fetch('/api/settings', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ overclock_overlay_bdagchain: false })}); } catch(_) {}
              const res = await fetch('/api/overclock/overlay/disable', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ target: 'BdagChain', commit: true }) });
              const j = await res.json().catch(()=>({}));
              if (!res.ok || j.ok === false) throw new Error(j.error || `HTTP ${res.status}`);
              setOCStatus(`OverlayFS reverted for BdagChain (reverted ${j.reverted||0})`, 'success');
            }
          } catch (err) {
            setOCStatus(err.message || 'OverlayFS action failed', 'error');
          }
        });
      }

      // OverlayFS toggle (bdageth/chaindata)
      if (ocOverlayBdagEth) {
        ocOverlayBdagEth.addEventListener('change', async () => {
          try {
            if (ocOverlayBdagEth.checked) {
              setOCStatus('Applying OverlayFS to all bdageth…');
              openOcLogs();
              updateOcLayout();
              const interval = Number(ocOverlayIntervalBdagEth?.value || 30);
              const limitGiB = Number(ocOverlayLimitBdagEth?.value || 4);
              const res = await fetch('/api/overclock/overlay/align', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ interval_bdageth: interval, limit_bdageth_gib: limitGiB }) });
              const j = await res.json().catch(()=>({}));
              if (!res.ok || j.ok === false) throw new Error(j.error || `HTTP ${res.status}`);
              try { await fetch('/api/settings', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ overclock_overlay_bdageth: true })}); } catch(_) {}
              setOCStatus(`OverlayFS active across ${j.count||0} targets for bdageth`, 'success');
            } else {
              setOCStatus('Reverting OverlayFS for all bdageth…');
              openOcLogs();
              updateOcLayout();
              try { await fetch('/api/settings', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ overclock_overlay_bdageth: false })}); } catch(_) {}
              const res = await fetch('/api/overclock/overlay/disable', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({ target: 'bdageth-chaindata', commit: true }) });
              const j = await res.json().catch(()=>({}));
              if (!res.ok || j.ok === false) throw new Error(j.error || `HTTP ${res.status}`);
              setOCStatus(`OverlayFS reverted for bdageth (reverted ${j.reverted||0})`, 'success');
            }
            void refreshOverlayStatus();
          } catch (err) {
            setOCStatus(err.message || 'OverlayFS action failed', 'error');
          }
        });
      }

      // Overlay commit interval selector
      const applyOverlayParams = async (target) => {
        const isBdagChain = target === 'BdagChain';
        const intervalEl = isBdagChain ? ocOverlayIntervalBdagChain : ocOverlayIntervalBdagEth;
        const limitEl = isBdagChain ? ocOverlayLimitBdagChain : ocOverlayLimitBdagEth;
        const interval = Number(intervalEl?.value || 15);
        const limitGiB = Number(limitEl?.value || 3);
        await fetch('/api/overclock/overlay/apply', {
          method:'POST', headers:{'content-type':'application/json'},
          body: JSON.stringify({ target, interval_sec: interval, limit_gib: limitGiB })
        });
      };
      if (ocOverlayIntervalBdagChain) {
        ocOverlayIntervalBdagChain.addEventListener('change', async () => {
          try { if (ocOverlayBdagChain?.checked) { await applyOverlayParams('BdagChain'); setOCStatus(`BdagChain interval set`);} } catch(_) {}
        });
      }
      if (ocOverlayLimitBdagChain) {
        ocOverlayLimitBdagChain.addEventListener('change', async () => {
          try { if (ocOverlayBdagChain?.checked) { await applyOverlayParams('BdagChain'); setOCStatus(`BdagChain RAM limit set`);} } catch(_) {}
        });
      }
      if (ocOverlayIntervalBdagEth) {
        ocOverlayIntervalBdagEth.addEventListener('change', async () => {
          try { if (ocOverlayBdagEth?.checked) { await applyOverlayParams('bdageth-chaindata'); setOCStatus(`bdageth interval set`);} } catch(_) {}
        });
      }
      if (ocOverlayLimitBdagEth) {
        ocOverlayLimitBdagEth.addEventListener('change', async () => {
          try { if (ocOverlayBdagEth?.checked) { await applyOverlayParams('bdageth-chaindata'); setOCStatus(`bdageth RAM limit set`);} } catch(_) {}
        });
      }

      function overlayTypeFromName(raw) {
        if (!raw) return 'overlay';
        const base = String(raw);
        const part = base.includes('@') ? base.split('@')[0] : base;
        if (part === 'bdageth-chaindata') return 'bdageth';
        return part;
      }

      function findOverlayNodeLabel(item) {
        const lower = String(item?.lower || '');
        let label = '';
        if (lower) {
          state.nodes.forEach((entry) => {
            if (label) return;
            const meta = entry?.meta;
            if (!meta) return;
            const chainDir = meta.status?.chain_data_dir || meta.status?.data_dir || meta.status?.data_path || meta.chain_data_dir;
            if (chainDir && lower.startsWith(chainDir)) {
              label = meta.label || meta.id || '';
            }
          });
        }
        if (lower && (!label || label === item?.node)) {
          const match = lower.match(/blockdag-scripts\/(bin[^/]+)/i);
          if (match && match[1]) label = match[1];
        }
        if (!label && typeof item?.node === 'string' && item.node) {
          label = item.node;
        }
        if (!label && item?.overlay) {
          label = overlayTypeFromName(item.overlay);
        }
        if (!label && item?.name) {
          label = overlayTypeFromName(item.name);
        }
      return label || 'overlay';
      }

      function canonicalOverlayNodeId(nodeId, label) {
        const match = typeof label === 'string' ? label.match(/^bin(\d*)$/i) : null;
        if (match) {
          const suffix = match[1] || '';
          if (suffix) {
            const base = typeof nodeId === 'string' && nodeId ? nodeId.replace(/-\d+$/, '') : 'blockdag-testnet-network';
            return `${base}-${suffix}`;
          }
          const base = typeof nodeId === 'string' && nodeId ? nodeId.replace(/-\d+$/, '') : 'blockdag-testnet-network';
          return base;
        }
        return nodeId || label || 'overlay';
      }

      function displayOverlayNodeLabel(nodeId, label) {
        const canonicalId = canonicalOverlayNodeId(nodeId, label);
        const nodeEntry = state.nodes.get(canonicalId);
        if (nodeEntry?.meta?.label) return nodeEntry.meta.label;
        if (nodeEntry?.meta?.id) return nodeEntry.meta.id;
        if (typeof label === 'string' && /^bin(\d*)$/i.test(label)) {
          const match = label.match(/^bin(\d*)$/i);
          const suffix = match?.[1] || '';
          const base = typeof nodeId === 'string' && nodeId ? nodeId.replace(/-\d+$/, '') : 'blockdag-testnet-network';
          return suffix ? `${base}-${suffix}` : base;
        }
        if (typeof nodeId === 'string' && nodeId) return nodeId;
        return label || 'overlay';
      }

      function ensureOverlayChart() {
        if (!ocOverlayChartCanvas || typeof Chart !== 'function') {
          return null;
        }
        if (ocOverlayChart) {
          if (ocOverlayChart.options?.indexAxis !== 'y') {
            ocOverlayChart.options.indexAxis = 'y';
            try { ocOverlayChart.update(); } catch (_) {}
          }
          return ocOverlayChart;
        }
        const ctx = ocOverlayChartCanvas.getContext('2d');
        ocOverlayChart = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: [],
            datasets: [{
              label: 'Used (GiB)',
              data: [],
              backgroundColor: 'rgba(255,99,132,0.65)',
              borderColor: 'rgba(255,99,132,1)',
              borderWidth: 1,
              maxBarThickness: 32,
              parsing: false,
            }],
          },
          options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            layout: {
              padding: { top: 8, right: 16, bottom: 12, left: 24 },
            },
            scales: {
              x: {
                beginAtZero: true,
                grid: { color: 'rgba(255,255,255,0.08)' },
                ticks: {
                  color: '#e7eaf6',
                  callback: (value) => `${value} GiB`,
                },
                stacked: true,
              },
              y: {
                grid: { color: 'rgba(255,255,255,0.08)' },
                ticks: {
                  color: '#e7eaf6',
                  maxRotation: 0,
                  minRotation: 0,
                  autoSkip: false,
                  crossAlign: 'far',
                },
                stacked: true,
              },
            },
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  title: (contexts) => {
                    const ctx = contexts[0];
                    return ctx.label || '';
                  },
                  label: (context) => {
                    const datasetLabel = context.dataset?.label || 'Value';
                    const value = Number(context.raw || context.parsed?.x || 0);
                    return `${datasetLabel}: ${value.toFixed(2)} GiB`;
                  },
                  afterBody: (items) => {
                    const ctx = items[0];
                    const chart = ctx?.chart;
                    const idx = ctx?.dataIndex ?? -1;
                    const totals = chart?.$overlayTotals && idx >= 0 ? chart.$overlayTotals[idx] : null;
                    const segments = chart?.$overlaySegments && idx >= 0 ? chart.$overlaySegments[idx] : null;
                    if (!totals) return undefined;
                    const lines = [];
                    const limitLine = totals.limit ? `Limit: ${totals.limit.toFixed(2)} GiB` : null;
                    if (limitLine) lines.push(limitLine);
                    if (segments && segments.length) {
                      lines.push('Breakdown:');
                      segments.forEach((seg) => {
                        lines.push(`  ${seg.type}: ${seg.value.toFixed(2)} GiB`);
                      });
                    }
                    return lines.length ? lines : undefined;
                  },
                },
              },
            },
          },
        });
        return ocOverlayChart;
      }

      function updateOverlayChartItems(items, aggregatedMap) {
        if (!ocOverlayChartWrapper) return;
        const hasData = aggregatedMap && aggregatedMap.size > 0;
        const emptyMessage = 'No overlay activity yet.';
        if (!hasData) {
          ocOverlayChartWrapper.classList.add('is-empty');
          if (ocOverlayChart) {
            ocOverlayChart.data.labels = [];
            ocOverlayChart.data.datasets[0].data = [];
            ocOverlayChart.$overlaySegments = [];
            ocOverlayChart.update();
          }
          if (ocOverlayChartEmpty) ocOverlayChartEmpty.textContent = emptyMessage;
          return;
        }
        const entries = Array.from(aggregatedMap.entries());
        const sortedEntries = entries
          .map(([key, info]) => [key, info])
          .sort(([, aInfo], [, bInfo]) => Number(bInfo.total || 0) - Number(aInfo.total || 0));
        const chart = ensureOverlayChart();
        if (!chart) return;
        const hasLimits = sortedEntries.some(([, info]) => Number(info.limitGiB || 0) > 0);
        if (!chart.data.datasets || chart.data.datasets.length !== (hasLimits ? 2 : 1)) {
          chart.data.datasets = hasLimits
            ? [
                {
                  label: 'Used (GiB)',
                  data: [],
                  backgroundColor: 'rgba(255,99,132,0.65)',
                  borderColor: 'rgba(255,99,132,1)',
                  borderWidth: 1,
                  maxBarThickness: 32,
                },
                {
                  label: 'Remaining (GiB)',
                  data: [],
                  backgroundColor: 'rgba(68,242,168,0.35)',
                  borderColor: '#44f2a8',
                  borderWidth: 1,
                  maxBarThickness: 32,
                },
              ]
            : [
                {
                  label: 'Used (GiB)',
                  data: [],
                  backgroundColor: 'rgba(68,242,168,0.35)',
                  borderColor: '#44f2a8',
                  borderWidth: 1,
                  maxBarThickness: 32,
                },
              ];
        }
        if (ocOverlayChartCanvas) {
          const desiredHeight = Math.max(180, sortedEntries.length * 28);
          ocOverlayChartCanvas.style.height = `${desiredHeight}px`;
          ocOverlayChartCanvas.height = desiredHeight;
        }
        const allZero = sortedEntries.every(([, info]) => Number(info.total || 0) === 0);
        if (allZero) {
          ocOverlayChartWrapper.classList.add('is-empty');
          if (ocOverlayChartEmpty) ocOverlayChartEmpty.textContent = emptyMessage;
        } else {
          ocOverlayChartWrapper.classList.remove('is-empty');
          if (ocOverlayChartEmpty) ocOverlayChartEmpty.textContent = emptyMessage;
        }
        chart.data.labels = sortedEntries.map(([, info]) => info.label || 'overlay');
        const usedData = sortedEntries.map(([, info]) => Number(info.usedGiB?.toFixed(2) || 0));
        const remainingData = sortedEntries.map(([, info]) => Number(info.remainingGiB?.toFixed(2) || 0));
        chart.data.datasets[0].data = usedData;
        if (chart.data.datasets[1]) {
          chart.data.datasets[1].data = remainingData;
        }
        chart.$overlaySegments = sortedEntries.map(([, info]) => info.segments);
        chart.$overlayTotals = sortedEntries.map(([, info]) => ({
          used: Number(info.usedGiB || 0),
          remaining: Number(info.remainingGiB || 0),
          limit: Number(info.limitGiB || 0),
        }));
        chart.update();
      }

      async function refreshOverlayStatus() {
        try {
          const res = await fetch('/api/overclock/overlay/status', { cache: 'no-store' });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const j = await res.json();
          const items = Array.isArray(j.items) ? j.items : [];
          const overlaysByNode = new Map();
          let autoReverted = false;
          const aggregated = new Map();
          items.forEach((it) => {
            const controlTypeRaw = String(it.overlay || it.name || '');
            const controlType = controlTypeRaw.includes('@') ? controlTypeRaw.split('@')[0] : controlTypeRaw;
            const typeLabel = overlayTypeFromName(controlTypeRaw);
            const active = !!it.mounted;
            const upperBytes = Number(it.upper_bytes || 0);
            const upperGiB = upperBytes / (1024 * 1024 * 1024);
            const valueRounded = Number(upperGiB.toFixed(2));
            const nodeLabel = findOverlayNodeLabel(it);
            const rawNodeId = typeof it.node === 'string' && it.node ? it.node : null;
            const canonicalId = canonicalOverlayNodeId(rawNodeId, nodeLabel);
            const displayLabel = displayOverlayNodeLabel(rawNodeId, nodeLabel);
            if (!aggregated.has(canonicalId)) {
              aggregated.set(canonicalId, {
                total: 0,
                segments: new Map(),
                label: displayLabel,
                $nodeId: canonicalId,
                limitGiB: 0,
              });
            }
            const entry = aggregated.get(canonicalId);
            entry.label = displayLabel;
            entry.total += valueRounded;
            const limitBytes = Number(it.limit_bytes || 0);
            if (limitBytes > 0) {
              const limitGiB = limitBytes / (1024 * 1024 * 1024);
              entry.limitGiB += limitGiB;
            }
            if (!entry.segments.has(typeLabel)) {
              entry.segments.set(typeLabel, 0);
            }
            entry.segments.set(typeLabel, entry.segments.get(typeLabel) + valueRounded);
            if (rawNodeId) {
              if (!overlaysByNode.has(rawNodeId)) {
                overlaysByNode.set(rawNodeId, []);
              }
              overlaysByNode.get(rawNodeId).push({
                name: overlayTypeFromName(controlTypeRaw),
                raw: controlTypeRaw,
                mounted: active,
                upperBytes,
                limitBytes: Number(it.limit_bytes || 0) || null,
              });
            }

            const limit = Number(it.limit_bytes || (3 * 1024 * 1024 * 1024));
            if (active && upperBytes > limit) {
              autoReverted = true;
              fetch('/api/overclock/overlay/revert', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ target: controlType, commit: true }),
              }).catch(() => {});
              if (controlType === 'BdagChain' && ocOverlayBdagChain) ocOverlayBdagChain.checked = false;
              if (controlType === 'bdageth-chaindata' && ocOverlayBdagEth) ocOverlayBdagEth.checked = false;
            }

            if (controlType === 'BdagChain') {
              if (ocOverlayIntervalBdagChain && it.interval_sec) ocOverlayIntervalBdagChain.value = String(it.interval_sec);
              if (ocOverlayLimitBdagChain && it.limit_bytes) ocOverlayLimitBdagChain.value = (Number(it.limit_bytes) / (1024 * 1024 * 1024)).toFixed(1);
            } else if (controlType === 'bdageth-chaindata') {
              if (ocOverlayIntervalBdagEth && it.interval_sec) ocOverlayIntervalBdagEth.value = String(it.interval_sec);
              if (ocOverlayLimitBdagEth && it.limit_bytes) ocOverlayLimitBdagEth.value = (Number(it.limit_bytes) / (1024 * 1024 * 1024)).toFixed(1);
            }
          });

          aggregated.forEach((info, key) => {
            if (info.segments instanceof Map) {
              info.segments = Array.from(info.segments.entries()).map(([segType, segValue]) => ({
                type: segType,
                value: segValue,
              }));
            }
            const limitGiB = Number(info.limitGiB || 0);
            info.usedGiB = Number(info.total.toFixed(2));
            info.limitGiB = limitGiB ? Number(limitGiB.toFixed(2)) : null;
            const remaining = limitGiB ? Math.max(0, limitGiB - info.usedGiB) : null;
            info.remainingGiB = remaining != null ? Number(remaining.toFixed(2)) : null;
            info.hasLimit = Number(info.limitGiB || 0) > 0;
          });
          const summaryParts = Array.from(aggregated.values())
            .sort((a, b) => Number(b.total || 0) - Number(a.total || 0))
            .map((info) => {
              const label = info.label || 'overlay';
              const used = info.usedGiB?.toFixed(2) ?? '0.00';
              if (info.hasLimit && info.limitGiB) {
                const remain = info.remainingGiB != null ? info.remainingGiB.toFixed(2) : '0.00';
                return `${label}: ${used} GiB used / ${remain} GiB free (limit ${info.limitGiB.toFixed(2)} GiB)`;
              }
              return `${label}: ${used} GiB used`;
            });
          state.overlayStatus = { items, byNode: overlaysByNode };
          if (ocOverlayStatus) {
            ocOverlayStatus.textContent = summaryParts.length ? summaryParts.join(' | ') : 'No overlays active';
          }
          updateOverlayChartItems(items, aggregated);
          const hasOverlayData = aggregated.size && Array.from(aggregated.values()).some((info) => Number(info.total || 0) > 0);
          if (ocOverlayBdagChain) {
            const hasBdag = Array.from(aggregated.values()).some((info) => (info.segments || []).some((seg) => seg.type === 'BdagChain' && Number(seg.value || 0) > 0));
            if (!hasBdag) ocOverlayBdagChain.checked = false;
          }
          if (ocOverlayBdagEth) {
            const hasEth = Array.from(aggregated.values()).some((info) => (info.segments || []).some((seg) => seg.type === 'bdageth' && Number(seg.value || 0) > 0));
            if (!hasEth) ocOverlayBdagEth.checked = false;
          }
          if (autoReverted) setOCStatus('Overlay auto-reverted due to RAM limit', 'error');
        } catch (err) {
          if (ocOverlayStatus) ocOverlayStatus.textContent = 'Overlay status unavailable';
          updateOverlayChartItems([], new Map());
          state.overlayStatus = { items: [], byNode: new Map() };
        }
      }

      // Initial status refresh
      void refreshOverlayStatus();
      // Manual removed
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
    if (ocLogsToggle) {
      ocLogsToggle.addEventListener('click', () => {
        const expanded = ocLogsToggle.getAttribute('aria-expanded') === 'true';
        if (expanded) {
          setOcLogsExpanded(false);
        } else {
          openOcLogs();
        }
      });
    }
    if (ocLogsRefreshBtn) {
      ocLogsRefreshBtn.addEventListener('click', () => {
        const expanded = ocLogsToggle && ocLogsToggle.getAttribute('aria-expanded') === 'true';
        if (!expanded) {
          openOcLogs();
        } else {
          void loadOcLogs({ force: true });
        }
      });
    }
    if (automationLogToggle) {
      automationLogToggle.addEventListener('click', () => {
        setAutomationLogsExpanded(!state.automationLogs.expanded);
      });
    }
    if (automationLogRefreshBtn) {
      automationLogRefreshBtn.addEventListener('click', () => {
        void loadAutomationLogs({ force: true });
      });
    }
    if (automationLogFilter) {
      state.automationLogs.filter = automationLogFilter.value || 'all';
      automationLogFilter.addEventListener('change', (event) => {
        const target = event.target;
        if (!(target instanceof HTMLSelectElement)) return;
        state.automationLogs.filter = target.value || 'all';
        renderAutomationLogs();
      });
    }
  }

  async function init() {
    attachEventHandlers();
    const initialTab = summaryTabButtons.find((button) => button.classList.contains('is-active')) || summaryTabButtons[0] || null;
    if (initialTab) {
      switchSummaryTab(initialTab);
    }
    setLaunchpadStep(1);
    await loadSettings();
    // No manual data dir field; backend auto-detects on actions
    await loadNodes();
    await refreshMetrics();
    await loadSystem();
    startSystemPolling();
    await loadSnapshots({ silent: true });
    await discoverNodes({ auto: true });
    await refreshMetrics();
    // Chart removed per request; skipping auto-prime visualization
    setInterval(refreshMetrics, 5000);
  }

  async function loadOcLogs() {
    try {
      const res = await fetch('/api/overclock/logs?limit=200', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      const lines = payload.lines || [];
      if (ocLogsOutput) {
        ocLogsOutput.textContent = lines.length ? lines.join('\n') + '\n' : 'No log lines yet.';
      }
      if (ocLogsMeta) {
        ocLogsMeta.textContent = `Updated ${fmtShortDateTime.format(new Date())}`;
      }
    } catch (err) {
      if (ocLogsOutput) ocLogsOutput.textContent = 'Failed to load logs';
      if (ocLogsMeta) ocLogsMeta.textContent = 'Failed to load logs';
    }
  }

  function startOcLogPolling() {
    stopOcLogPolling();
    ocLogPollTimer = window.setInterval(() => { void loadOcLogs(); }, 4000);
  }

  function stopOcLogPolling() {
    if (ocLogPollTimer) {
      clearInterval(ocLogPollTimer);
      ocLogPollTimer = null;
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshMetrics();
      if (state.snapshotsLoaded) {
        void loadSnapshots({ silent: true });
      }
      void loadSystem();
      const ocExpanded = ocLogsToggle && ocLogsToggle.getAttribute('aria-expanded') === 'true';
      if (ocExpanded) {
        startOcLogPolling();
        void loadOcLogs({ force: true });
      }
      if (state.automationLogs.expanded) {
        void loadAutomationLogs({ force: true, silent: true });
        startAutomationLogPolling();
      }
    } else {
      stopOcLogPolling();
      stopAutomationLogPolling();
    }
  });

  window.addEventListener('load', init);
})();
