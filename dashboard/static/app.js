function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatDateTime(value) {
  if (!value) {
    return 'Unavailable';
  }

  const date = new Date(String(value).replace(' ', 'T'));
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }

  return new Intl.DateTimeFormat('en-US', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

function formatInteger(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }

  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(Number(value));
}

function formatDecimal(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }

  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }

  return `${formatDecimal(value)}%`;
}

function formatPrice(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }

  return `$${formatDecimal(value)}`;
}

const RECOMMEND_THRESHOLDS = [
  { min: 0.5, label: 'Strong Buy', className: 'recommendation-strong-buy' },
  { min: 0.1, label: 'Buy', className: 'recommendation-buy' },
  { min: -0.1, label: 'Neutral', className: 'recommendation-neutral' },
  { min: -0.5, label: 'Sell', className: 'recommendation-sell' },
  { min: Number.NEGATIVE_INFINITY, label: 'Strong Sell', className: 'recommendation-strong-sell' },
];

function formatSignedPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }

  const numericValue = Number(value);
  return `${numericValue > 0 ? '+' : ''}${formatPercent(numericValue)}`;
}

function formatRecommendation(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }

  const numericValue = Number(value);
  const recommendation = RECOMMEND_THRESHOLDS.find((threshold) => numericValue >= threshold.min) || RECOMMEND_THRESHOLDS.at(-1);
  return `<span class="result-badge ${recommendation.className}" title="Recommend.All ${escapeHtml(formatDecimal(numericValue, 3))}">${escapeHtml(recommendation.label)}</span>`;
}

function normalizeHeadlines(row) {
  if (!Array.isArray(row?.headlines)) {
    return [];
  }

  return [...new Set(
    row.headlines
      .map((headline) => String(headline ?? '').trim())
      .filter(Boolean)
  )];
}

function normalizeNewsItems(row) {
  if (!Array.isArray(row?.newsItems)) {
    return [];
  }

  const seen = new Set();

  return row.newsItems
    .map((item) => {
      if (!item || typeof item !== 'object') {
        return null;
      }

      const title = String(item.title ?? '').trim();
      const body = String(item.body ?? '').trim();
      const source = String(item.source ?? '').trim();
      const url = String(item.url ?? '').trim();
      const storyUrl = String(item.story_url ?? item.storyUrl ?? '').trim();
      const published = String(item.published ?? '').trim();
      const storyPath = String(item.story_path ?? item.storyPath ?? '').trim();

      if (!title && !body) {
        return null;
      }

      const key = [title, body, source, url, storyUrl, published, storyPath].join('||').toLowerCase();
      if (seen.has(key)) {
        return null;
      }

      seen.add(key);
      return {
        title,
        body,
        source,
        url,
        storyUrl,
        published,
        storyPath,
      };
    })
    .filter(Boolean);
}

function shortDiagnostic(error) {
  if (!error) {
    return '';
  }

  const message = String(error).replace(/^Error:\s*/i, '').trim();
  const httpStatusMatch = message.match(/HTTP Error\s+(\d{3})/i) || message.match(/\b(\d{3})\b/);

  if (/groq/i.test(message) && httpStatusMatch) {
    return `Groq ${httpStatusMatch[1]}`;
  }

  if (/forbidden/i.test(message)) {
    return 'Groq 403';
  }

  if (/timeout|timed out/i.test(message)) {
    return 'Groq timeout';
  }

  if (/rate limit|too many requests/i.test(message)) {
    return 'Groq rate limit';
  }

  const compactMessage = message.replace(/^[A-Za-z\s]+failed:\s*/i, '').trim();
  return compactMessage ? compactMessage.slice(0, 44) : 'Groq issue';
}

function renderCatalystCell(row) {
  const catalyst = String(row?.catalyst ?? '').trim();
  const headlines = normalizeHeadlines(row);
  const newsItems = normalizeNewsItems(row);
  const rawError = row?.catalystError ? String(row.catalystError).trim() : '';
  const diagnostic = shortDiagnostic(rawError);
  const diagnosticLine = rawError
    ? `<span class="catalyst-diagnostic" title="${escapeHtml(rawError)}">${escapeHtml(diagnostic || 'Groq issue')}</span>`
    : '';
  const rowId = row?.id === null || row?.id === undefined ? '' : String(row.id);
  const hasNews = newsItems.length || headlines.length;
  const newsButton = hasNews
    ? `<button class="action-button action-button-sm catalyst-news-button" type="button" data-news-row-id="${escapeHtml(rowId)}">News</button>`
    : '';

  if (catalyst) {
    return `
      <div class="catalyst-stack">
        <span class="result-badge catalyst-badge">AI summary</span>
        <span class="catalyst-primary">${escapeHtml(catalyst)}</span>
        ${newsButton || diagnosticLine ? `<span class="catalyst-meta">${newsButton}${diagnosticLine}</span>` : ''}
      </div>
    `;
  }

  if (hasNews) {
    return `
      <div class="catalyst-stack catalyst-stack-neutral">
        <span class="catalyst-note">No AI summary</span>
        <span class="catalyst-meta">${newsButton}${diagnosticLine}</span>
      </div>
    `;
  }

  return `
    <div class="catalyst-stack">
      <span class="catalyst-empty">No catalyst or news stored</span>
      ${diagnosticLine}
    </div>
  `;
}

function badgeClass(status) {
  const normalized = String(status || 'missing').toLowerCase();
  if (normalized === 'success') return 'status-pill status-success';
  if (normalized === 'error') return 'status-pill status-error';
  if (normalized === 'empty') return 'status-pill status-empty';
  return 'status-pill status-missing';
}

function resultBadge(result) {
  if (!result) {
    return '<span class="result-badge">No result</span>';
  }

  const normalized = String(result).toUpperCase();
  const className = normalized === 'PASS'
    ? 'result-badge result-pass'
    : `result-badge result-fail result-${normalized.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-')}`;
  return `<span class="${className}">${escapeHtml(normalized)}</span>`;
}

function shortenText(value, maxLength = 120) {
  const text = String(value || '').trim().replace(/\s+/g, ' ');
  if (!text) {
    return '';
  }

  return text.length > maxLength ? `${text.slice(0, maxLength - 1).trimEnd()}…` : text;
}

function createLevelMarkers(levels) {
  return levels
    .map((level) => {
      const value = Number(level.value);
      return Number.isNaN(value) ? null : { ...level, value };
    })
    .filter(Boolean);
}

function renderMarkerLines(markers, toY, plotWidth, labelX) {
  return markers
    .map((marker) => {
      const y = toY(marker.value).toFixed(1);
      return `
        <g class="mini-chart-marker ${escapeHtml(marker.className || '')}">
          <line class="mini-chart-marker-line" x1="0" y1="${y}" x2="${plotWidth}" y2="${y}" />
          <text class="mini-chart-marker-label" x="${labelX}" y="${Math.max(Number(y) - 4, 10).toFixed(1)}">${escapeHtml(marker.label)}</text>
        </g>
      `;
    })
    .join('');
}

function renderSparkline(values, options = {}) {
  const {
    width = 320,
    height = 104,
    className = 'sparkline',
    lineClassName = 'sparkline-path',
    dotClassName = 'sparkline-dot',
    areaClassName = 'sparkline-area',
    markers = [],
  } = options;

  if (!Array.isArray(values) || values.length < 2) {
    return '<div class="sparkline-empty">Not enough history</div>';
  }

  const numericValues = values.map((value) => Number(value)).filter((value) => !Number.isNaN(value));
  if (numericValues.length < 2) {
    return '<div class="sparkline-empty">Not enough history</div>';
  }

  const validMarkers = createLevelMarkers(markers);
  const referenceValues = [...numericValues, ...validMarkers.map((marker) => marker.value)];
  const min = Math.min(...referenceValues);
  const max = Math.max(...referenceValues);
  const range = max - min || 1;
  const plotWidth = width - 42;
  const stepX = plotWidth / (numericValues.length - 1);

  const toY = (value) => height - ((Number(value) - min) / range) * (height - 14) - 7;
  const path = numericValues
    .map((value, index) => `${index === 0 ? 'M' : 'L'} ${(index * stepX).toFixed(1)} ${toY(value).toFixed(1)}`)
    .join(' ');
  const areaPath = `${path} L ${plotWidth.toFixed(1)} ${(height - 3).toFixed(1)} L 0 ${(height - 3).toFixed(1)} Z`;
  const lastX = ((numericValues.length - 1) * stepX).toFixed(1);
  const lastY = toY(numericValues[numericValues.length - 1]).toFixed(1);
  const gridLines = [0.2, 0.5, 0.8]
    .map((ratio) => {
      const y = (height * ratio).toFixed(1);
      return `<line class="mini-chart-grid" x1="0" y1="${y}" x2="${plotWidth}" y2="${y}" />`;
    })
    .join('');

  return `
    <svg class="${className}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      ${gridLines}
      ${renderMarkerLines(validMarkers, toY, plotWidth, plotWidth + 4)}
      <path class="${areaClassName}" d="${areaPath}" />
      <path class="${lineClassName}" d="${path}" />
      <circle class="${dotClassName}" cx="${lastX}" cy="${lastY}" r="3.2" />
    </svg>
  `;
}

function renderCandlestickChart(candles, options = {}) {
  const {
    width = 320,
    height = 104,
    markers = [],
  } = options;

  if (!Array.isArray(candles) || candles.length < 2) {
    return '<div class="sparkline-empty">Not enough history</div>';
  }

  const normalizedCandles = candles
    .map((candle) => ({
      open: Number(candle?.open),
      high: Number(candle?.high),
      low: Number(candle?.low),
      close: Number(candle?.close),
    }))
    .filter((candle) => [candle.open, candle.high, candle.low, candle.close].every((value) => !Number.isNaN(value)));

  if (normalizedCandles.length < 2) {
    return '<div class="sparkline-empty">Not enough history</div>';
  }

  const validMarkers = createLevelMarkers(markers);
  const referenceValues = [
    ...normalizedCandles.flatMap((candle) => [candle.high, candle.low]),
    ...validMarkers.map((marker) => marker.value),
  ];
  const min = Math.min(...referenceValues);
  const max = Math.max(...referenceValues);
  const range = max - min || 1;
  const plotWidth = width - 42;
  const slotWidth = plotWidth / normalizedCandles.length;
  const candleWidth = Math.max(3, Math.min(slotWidth * 0.62, 8));
  const toY = (value) => height - ((Number(value) - min) / range) * (height - 14) - 7;
  const gridLines = [0.2, 0.5, 0.8]
    .map((ratio) => {
      const y = (height * ratio).toFixed(1);
      return `<line class="mini-chart-grid" x1="0" y1="${y}" x2="${plotWidth}" y2="${y}" />`;
    })
    .join('');
  const candleMarkup = normalizedCandles
    .map((candle, index) => {
      const x = index * slotWidth + slotWidth / 2;
      const wickTop = toY(candle.high).toFixed(1);
      const wickBottom = toY(candle.low).toFixed(1);
      const openY = toY(candle.open);
      const closeY = toY(candle.close);
      const bodyY = Math.min(openY, closeY).toFixed(1);
      const bodyHeight = Math.max(Math.abs(openY - closeY), 1.8).toFixed(1);
      const bodyX = (x - candleWidth / 2).toFixed(1);
      const bodyClass = candle.close >= candle.open ? 'mini-candle-up' : 'mini-candle-down';

      return `
        <g class="mini-candle ${bodyClass}">
          <line class="mini-candle-wick" x1="${x.toFixed(1)}" y1="${wickTop}" x2="${x.toFixed(1)}" y2="${wickBottom}" />
          <rect class="mini-candle-body" x="${bodyX}" y="${bodyY}" width="${candleWidth.toFixed(1)}" height="${bodyHeight}" rx="1.2" />
        </g>
      `;
    })
    .join('');

  return `
    <svg class="sparkline sparkline-candles" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      ${gridLines}
      ${renderMarkerLines(validMarkers, toY, plotWidth, plotWidth + 4)}
      ${candleMarkup}
    </svg>
  `;
}

const selectedSymbols = new Set();
let currentJob = null;
let pollTimer = null;
let interactionsAttached = false;
let tjlSelectionHint = '';
let learnExpanded = false;
let selectedDate = null;
let currentDashboardData = null;
let activeNewsRowId = null;

function isHistoricalView() {
  return selectedDate !== null;
}

function formatDashboardDate(value) {
  if (!value) {
    return 'Unknown date';
  }

  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }

  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  }).format(date);
}

function getPremarketRows() {
  return Array.isArray(currentDashboardData?.premarket?.rows) ? currentDashboardData.premarket.rows : [];
}

function findPremarketRow(rowId) {
  const normalizedRowId = String(rowId ?? '').trim();
  if (!normalizedRowId) {
    return null;
  }

  return getPremarketRows().find((row) => String(row?.id ?? '') === normalizedRowId) || null;
}

function renderNewsModal() {
  const row = findPremarketRow(activeNewsRowId);
  const headlines = normalizeHeadlines(row);
  const newsItems = normalizeNewsItems(row);

  if (!row || (!newsItems.length && !headlines.length)) {
    return '';
  }

  const title = `${String(row.symbol || 'Unknown').trim() || 'Unknown'} · TradingView news`;
  const bodyMarkup = newsItems.length
    ? `
      <p class="news-modal-hint">Scroll this panel for the full article text, then use the source links at the bottom of each card when you want the publisher page.</p>
      <div class="news-article-list">
        ${newsItems.map((item) => {
          const metadata = [
            item.source ? `<span>${escapeHtml(item.source)}</span>` : '',
            item.published ? `<span>${escapeHtml(formatDateTime(item.published))}</span>` : '',
            item.storyPath && !item.url ? `<span>${escapeHtml(item.storyPath)}</span>` : '',
          ].filter(Boolean).join('<span aria-hidden="true">•</span>');

          return `
            <article class="news-article-card">
              <div class="news-article-copy">
                ${item.title ? `<h3 class="news-article-title">${escapeHtml(item.title)}</h3>` : ''}
                ${metadata ? `<p class="news-article-meta">${metadata}</p>` : ''}
                ${item.body ? `<p class="news-article-body">${escapeHtml(item.body)}</p>` : ''}
              </div>
              <div class="news-article-actions">
                ${item.url ? `<a class="news-article-link" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer noopener">Open source</a>` : ''}
                ${item.storyUrl && item.storyUrl !== item.url ? `<a class="news-article-link news-article-link-secondary" href="${escapeHtml(item.storyUrl)}" target="_blank" rel="noreferrer noopener">Open on TradingView</a>` : ''}
              </div>
            </article>
          `;
        }).join('')}
      </div>
    `
    : `
      <ol class="news-headline-list">
        ${headlines.map((headline) => `<li class="news-headline-item">${escapeHtml(headline)}</li>`).join('')}
      </ol>
    `;

  return `
    <div class="news-modal-backdrop" data-news-modal-backdrop>
      <section class="news-modal panel" role="dialog" aria-modal="true" aria-labelledby="news-modal-title">
        <div class="news-modal-header">
          <div class="news-modal-title-block">
            <p class="eyebrow">Source news</p>
            <h2 id="news-modal-title">${escapeHtml(title)}</h2>
          </div>
          <button class="action-button action-button-sm news-modal-close" type="button" data-close-news-modal>Close</button>
        </div>
        <div class="news-modal-body">
          ${bodyMarkup}
        </div>
      </section>
    </div>
  `;
}

function refreshNewsModal() {
  const mount = document.getElementById('news-modal-mount');
  if (!mount) {
    return;
  }

  const markup = renderNewsModal();
  if (!markup) {
    activeNewsRowId = null;
  }

  mount.innerHTML = markup;
  document.body.classList.toggle('news-modal-open', Boolean(markup));
}

function openNewsModal(rowId) {
  const row = findPremarketRow(rowId);
  if (!row || (!normalizeNewsItems(row).length && !normalizeHeadlines(row).length)) {
    return;
  }

  activeNewsRowId = String(rowId);
  refreshNewsModal();
}

function closeNewsModal() {
  if (!activeNewsRowId) {
    return;
  }

  activeNewsRowId = null;
  refreshNewsModal();
}

async function startJob(endpoint, body, label) {
  if (currentJob && currentJob.status === 'running') {
    return;
  }

  tjlSelectionHint = '';

  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      throw new Error(`request failed with ${response.status}`);
    }
    const data = await response.json();
    currentJob = { id: data.jobId, label, status: 'running', output: '', error: null };
    pollJob(data.jobId);
    refreshJobUi();
  } catch (error) {
    currentJob = {
      id: null,
      label,
      status: 'error',
      output: '',
      error: error instanceof Error ? error.message : String(error),
      completedAt: new Date().toISOString(),
    };
    refreshJobUi();
  }
}

function runPremarket() {
  startJob('/api/run/premarket', null, 'Premarket gap scan');
}

function runTjl() {
  const symbols = [...selectedSymbols];
  if (symbols.length === 0) {
    tjlSelectionHint = 'Select premarket rows to enable Trend Join Long.';
    refreshJobUi();
    return;
  }

  tjlSelectionHint = '';
  const select = document.getElementById('tjl-timeframe');
  const intradayTimeframe = select ? select.value : '15';
  startJob('/api/run/tjl', { symbols, intradayTimeframe }, `TJL scan (${intradayTimeframe}m): ${symbols.join(', ')}`);
}

async function pollJob(jobId) {
  if (pollTimer) {
    clearTimeout(pollTimer);
  }

  try {
    const response = await fetch(`/api/jobs/${jobId}`, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`job status request failed with ${response.status}`);
    }
    const data = await response.json();

    currentJob = { ...currentJob, ...data };
    refreshJobUi();

    if (data.status === 'running') {
      pollTimer = setTimeout(() => pollJob(jobId), 3000);
    } else {
      pollTimer = null;
      loadDashboard();
    }
  } catch (error) {
    currentJob = {
      ...currentJob,
      status: 'error',
      error: error instanceof Error ? error.message : String(error),
    };
    refreshJobUi();
  }
}

function renderJobBanner() {
  if (!currentJob) {
    return '';
  }

  const statusClass = `job-banner-${currentJob.status}`;
  const spinner = currentJob.status === 'running'
    ? '<span class="job-spinner"></span>'
    : '';
  const outputBlock = currentJob.output
    ? `<pre class="job-output">${escapeHtml(currentJob.output.slice(-2000))}</pre>`
    : '';
  const errorBlock = currentJob.error
    ? `<p class="error-copy">${escapeHtml(currentJob.error)}</p>`
    : '';

  return `
    <section class="job-banner ${statusClass} span-12">
      <div class="job-banner-header">
        ${spinner}
        <div class="job-banner-title">
          <p class="eyebrow">${escapeHtml(currentJob.status)}</p>
          <h2>${escapeHtml(currentJob.label)}</h2>
        </div>
        ${currentJob.status !== 'running' ? '<button class="action-button action-button-sm" id="dismiss-job">Dismiss</button>' : ''}
      </div>
      ${errorBlock}
      ${outputBlock}
    </section>
  `;
}

function renderActionBar() {
  const count = selectedSymbols.size;
  const jobRunning = currentJob && currentJob.status === 'running';
  const historical = isHistoricalView();
  const disabled = jobRunning || historical ? 'disabled' : '';
  const jobLabel = jobRunning ? `Running: ${currentJob.label}` : '';
  const hint = historical
    ? 'Trend Join Long is disabled while viewing historical premarket data.'
    : (tjlSelectionHint || (count === 0 ? 'Select premarket rows to enable Trend Join Long.' : ''));

  return `
    <div class="action-bar ${count > 0 ? 'action-bar-visible' : ''}" id="action-bar">
      <div class="action-bar-info">
        <strong>${count}</strong> symbol${count === 1 ? '' : 's'} selected
        ${hint ? `<p class="action-bar-hint">${escapeHtml(hint)}</p>` : ''}
      </div>
      <div class="action-bar-controls">
        <label class="timeframe-label">
          Intraday
          <select id="tjl-timeframe" class="timeframe-select" ${disabled}>
            <option value="1">1m</option>
            <option value="5">5m</option>
            <option value="10">10m</option>
            <option value="15" selected>15m</option>
            <option value="30">30m</option>
            <option value="60">60m</option>
          </select>
        </label>
        <button class="action-button" id="run-tjl-btn" ${disabled || count === 0 ? 'disabled' : ''}>
          ${jobRunning ? jobLabel : historical ? 'Run Trend Join Long (historical disabled)' : 'Run Trend Join Long'}
        </button>
      </div>
    </div>
  `;
}

function refreshJobUi() {
  const jobRunning = currentJob && currentJob.status === 'running';
  const historical = isHistoricalView();
  const existing = document.getElementById('job-banner-mount');
  if (existing) {
    existing.innerHTML = renderJobBanner();
  }
  const bar = document.getElementById('action-bar-mount');
  if (bar) {
    bar.innerHTML = renderActionBar();
  }
  const premarketBtn = document.getElementById('run-premarket-btn');
  if (premarketBtn) {
    premarketBtn.disabled = jobRunning || historical;
    premarketBtn.textContent = historical
      ? 'Scan disabled (historical view)'
      : (jobRunning && currentJob && currentJob.label.includes('Premarket')) ? 'Running...' : 'Run premarket scan';
  }
  document.querySelectorAll('.row-checkbox').forEach((cb) => {
    cb.disabled = jobRunning || historical;
  });
}

function recheckSelectedRows() {
  document.querySelectorAll('.row-checkbox').forEach((checkbox) => {
    checkbox.checked = selectedSymbols.has(checkbox.dataset.symbol);
  });
}

function attachInteractionListeners() {
  if (interactionsAttached) {
    return;
  }

  document.addEventListener('click', (event) => {
    const target = event.target;

    if (!(target instanceof Element)) {
      return;
    }

    const newsButton = target.closest('[data-news-row-id]');
    if (newsButton) {
      openNewsModal(newsButton.dataset.newsRowId);
      return;
    }

    if (target.closest('[data-close-news-modal]')) {
      closeNewsModal();
      return;
    }

    if (target.matches('[data-news-modal-backdrop]')) {
      closeNewsModal();
      return;
    }

    if (target.id === 'run-premarket-btn') {
      if (isHistoricalView()) {
        return;
      }
      runPremarket();
      return;
    }

    if (target.id === 'run-tjl-btn') {
      if (isHistoricalView()) {
        return;
      }
      runTjl();
      return;
    }

    if (target.id === 'back-to-live-btn') {
      selectedDate = null;
      loadDashboard();
      return;
    }

    if (target.id === 'dismiss-job') {
      currentJob = null;
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      refreshJobUi();
      return;
    }

    if (target.id === 'learn-toggle-btn') {
      learnExpanded = !learnExpanded;
      const learnSectionMount = document.getElementById('learn-section-mount');
      if (learnSectionMount) {
        learnSectionMount.innerHTML = renderLearnSection();
      }
      return;
    }
  });

  document.addEventListener('change', (event) => {
    if (event.target.id === 'premarket-date-select') {
      selectedDate = event.target.value || null;
      loadDashboard();
      return;
    }

    if (event.target.classList && event.target.classList.contains('row-checkbox')) {
      const symbol = event.target.dataset.symbol;
      if (!symbol) {
        return;
      }
      if (event.target.checked) {
        selectedSymbols.add(symbol);
        tjlSelectionHint = '';
      } else {
        selectedSymbols.delete(symbol);
        if (selectedSymbols.size === 0) {
          tjlSelectionHint = 'Select premarket rows to enable Trend Join Long.';
        }
      }
      const bar = document.getElementById('action-bar-mount');
      if (bar) {
        bar.innerHTML = renderActionBar();
      }
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && activeNewsRowId) {
      closeNewsModal();
    }
  });

  interactionsAttached = true;
}

function renderHeroMeta(data) {
  const heroMeta = document.getElementById('hero-meta');
  heroMeta.innerHTML = [
    {
      label: 'SQLite',
      value: data.db.exists ? 'Connected' : 'Missing',
      note: data.db.path,
    },
    {
      label: 'Collector artifact',
      value: !data.collector.exists ? 'Waiting' : data.collector.isToday ? 'Today' : 'Stale',
      note: data.collector.path,
    },
    {
      label: 'Snapshot generated',
      value: formatDateTime(data.generatedAt),
      note: 'Localhost only · read-only view',
    },
  ]
    .map(
      (item) => `
        <article class="metric-card">
          <span class="metric-card-label">${escapeHtml(item.label)}</span>
          <strong class="metric-card-value">${escapeHtml(item.value)}</strong>
          <p class="detail-line">${escapeHtml(item.note)}</p>
        </article>
      `
    )
    .join('');
}

function createPanel({ span = 'span-12', title, eyebrow, copy, status, metrics, body }) {
  return `
    <section class="panel section-panel ${span}">
      <div class="section-header">
        <div class="section-title-block">
          <p class="eyebrow">${escapeHtml(eyebrow)}</p>
          <h2>${escapeHtml(title)}</h2>
          <p class="section-copy">${escapeHtml(copy)}</p>
        </div>
        <span class="${badgeClass(status)}">${escapeHtml(status || 'missing')}</span>
      </div>
      ${metrics}
      ${body}
    </section>
  `;
}

function createSummaryMetrics(items) {
  return `
    <div class="summary-metrics">
      ${items
        .map(
          (item) => `
            <article class="summary-chip">
              <span class="summary-chip-label">${escapeHtml(item.label)}</span>
              <strong class="summary-chip-value">${escapeHtml(item.value)}</strong>
              <p class="detail-line">${escapeHtml(item.note || '')}</p>
            </article>
          `
        )
        .join('')}
    </div>
  `;
}

function renderLearnDefinitionList(items) {
  return `
    <dl class="learn-definition-list">
      ${items
        .map(
          (item) => `
            <div class="learn-definition-item">
              <dt>${escapeHtml(item.term)}</dt>
              <dd>${escapeHtml(item.definition)}</dd>
            </div>
          `
        )
        .join('')}
    </dl>
  `;
}

function renderLearnWorkflow(items) {
  return `
    <ol class="learn-workflow-list">
      ${items
        .map(
          (item) => `
            <li>
              <strong>${escapeHtml(item.label)}</strong>
              <span>${escapeHtml(item.text)}</span>
            </li>
          `
        )
        .join('')}
    </ol>
  `;
}

function renderLearnSection() {
  const cards = [
    {
      title: 'Why Premarket Gappers?',
      body: `<p>${escapeHtml("Before the market opens, some stocks start trading at prices significantly different from yesterday's close. These are called 'gappers' — stocks that gap up (or down) because overnight news created a surge of buy or sell orders. A large gap up signals that something changed: earnings beat, FDA approval, analyst upgrade, contract win, or a short squeeze. The premarket scanner finds the top 10 stocks with the biggest upward gaps and highest trading volume, giving you a starting list of the day's most interesting movers.")}</p>`,
    },
    {
      title: 'Reading the Premarket Table',
      body: renderLearnDefinitionList([
        {
          term: 'Gap %',
          definition: "How far the stock jumped from yesterday's close to the current price. A 15% gap means the stock is trading 15% higher than where it closed. Bigger gaps attract more attention.",
        },
        {
          term: 'Price',
          definition: 'The current trading price per share. Useful for position sizing — a $5 stock and a $500 stock need very different capital.',
        },
        {
          term: 'Volume',
          definition: 'Total shares traded during premarket. High volume (millions) confirms genuine institutional interest. Low volume gaps are unreliable and often fade at the open.',
        },
        {
          term: 'Sector',
          definition: 'The industry the stock belongs to. When multiple gappers come from the same sector, it signals a sector-wide move (e.g., biotech news lifting the whole sector).',
        },
        {
          term: 'RSI',
          definition: 'Relative Strength Index (0–100). A momentum gauge. Above 70 means the stock is overbought (extended, due for a pullback). Below 30 means oversold. Most gappers sit above 60.',
        },
        {
          term: 'Rating',
          definition: 'TradingView\'s aggregate recommendation — Strong Buy, Buy, Neutral, Sell, or Strong Sell. A Strong Buy rating combined with a positive catalyst strengthens the setup.',
        },
        {
          term: 'Δ Open',
          definition: 'How much the stock moved from the market open price to now. Positive means the gap held or extended after the bell rang. Negative means the gap is fading — early buyers are selling.',
        },
        {
          term: 'Catalyst',
          definition: 'An AI-generated summary of WHY the stock is moving today, based on recent news headlines fetched for the symbol. This is your fastest way to understand the story behind the gap, but it should be confirmed by reading the full articles with the News button.',
        },
      ]),
    },
    {
      title: 'What Is Trend Join Long?',
      body: `
        <p>${escapeHtml('Trend Join Long (TJL) is a breakout strategy. Instead of guessing where a stock will go, it waits for confirmation that the stock is already breaking out, then joins the move. The strategy checks two layers of confirmation:')}</p>
        <p>${escapeHtml("First, the daily layer: the stock must be trading above yesterday's high, AND yesterday's close must have been above the 200-day moving average. This confirms the stock is in a long-term uptrend — not just a random spike.")}</p>
        <p>${escapeHtml("Second, the intraday layer: the stock must be trading above both the premarket high and the highest completed regular-hours high so far. This confirms that buyers are actively pushing the price to new highs right now.")}</p>
        <p>${escapeHtml('Only when BOTH layers pass does the strategy signal a PASS. This filters out stocks that gap up but then stall or reverse — a common trap for traders who buy the gap without waiting for confirmation.')}</p>
      `,
    },
    {
      title: 'Reading TJL Results',
      body: renderLearnDefinitionList([
        {
          term: 'PASS',
          definition: 'Both daily and intraday breakouts confirmed. The stock is in a long-term uptrend and currently breaking to new highs. This is the green light.',
        },
        {
          term: 'fail_daily',
          definition: "The daily trend check didn't pass — either the stock hasn't broken above yesterday's high, or it's trading below the 200-day moving average. The long-term trend isn't supporting the move.",
        },
        {
          term: 'fail_intraday',
          definition: "The daily trend is fine, but today's price action hasn't confirmed — the stock hasn't exceeded its premarket high or the highest completed high of day. Momentum is stalling.",
        },
        {
          term: 'PMH (Premarket High)',
          definition: 'The highest price the stock reached during the premarket session (4:00–9:30 AM ET). Breaking above this level during regular hours signals that premarket sellers have been overwhelmed.',
        },
        {
          term: 'HOD (High of Day)',
          definition: 'The highest price reached today during regular trading hours. For TJL, the scanner compares against completed intraday bars, excluding the current still-forming bar. A stock making new highs of day is showing sustained buying pressure.',
        },
        {
          term: 'Prev High',
          definition: "Yesterday's highest price. Breaking above this means the stock is stronger today than it was at its best yesterday.",
        },
        {
          term: 'SMA200',
          definition: 'The 200-day Simple Moving Average — a widely watched long-term trend line. Institutions tend to buy near this level, and trading above it is considered a long-term bullish signal.',
        },
      ]),
    },
    {
      title: 'Your Workflow',
      body: renderLearnWorkflow([
        {
          label: 'Before market open (7:00–9:15 AM ET):',
          text: 'Run the premarket scan. Review the top 10 gappers — look at gap size, volume, and especially the catalyst summaries. Click News to read full articles for stocks that catch your interest.',
        },
        {
          label: 'Filter your list:',
          text: 'Eliminate stocks with weak catalysts, low volume, or fading momentum (negative Δ Open). Focus on stocks with strong sector tailwinds and good ratings.',
        },
        {
          label: 'Select candidates:',
          text: 'Check the boxes next to 2–5 stocks you want to monitor. These become inputs for the Trend Join Long strategy.',
        },
        {
          label: 'After market open (9:30 AM ET):',
          text: 'Run Trend Join Long on your selected stocks. The scanner evaluates each one against the daily and intraday breakout criteria.',
        },
        {
          label: 'Act on PASS results:',
          text: 'Stocks that PASS both layers are showing confirmed breakout momentum. Stocks that fail may need more time or may be false starts. Use the date selector to review past scans and see which patterns worked.',
        },
      ]),
    },
  ];

  const cardsMarkup = learnExpanded
    ? `
      <div class="learn-cards-grid">
        ${cards
          .map(
            (card) => `
              <article class="learn-card">
                <h3>${escapeHtml(card.title)}</h3>
                <div class="learn-card-body">${card.body}</div>
              </article>
            `
          )
          .join('')}
      </div>
    `
    : '<p class="learn-collapsed-hint">Click to expand educational content.</p>';

  return `
    <section class="panel section-panel learn-section">
      <div class="section-header learn-section-header">
        <div class="section-title-block">
          <p class="eyebrow">Education center</p>
          <h2>Learn</h2>
          <p class="section-copy">Plain-English guidance for how to read gaps, validate breakouts, and turn headlines into better watchlists.</p>
        </div>
        <button class="action-button learn-toggle-button" id="learn-toggle-btn" type="button" aria-expanded="${learnExpanded ? 'true' : 'false'}">
          ${learnExpanded ? 'Hide' : 'Show'}
        </button>
      </div>
      ${cardsMarkup}
    </section>
  `;
}

function renderPremarketRunAction() {
  const jobRunning = currentJob && currentJob.status === 'running';
  const historical = isHistoricalView();
  const availableDates = Array.isArray(currentDashboardData?.availableDates) ? currentDashboardData.availableDates : [];
  const selectionHint = historical
    ? ''
    : selectedSymbols.size === 0
      ? '<p class="selection-hint">Select premarket rows to enable Trend Join Long.</p>'
      : '<p class="selection-hint selection-hint-ready">Premarket rows selected — Trend Join Long is ready in the action bar.</p>';
  const dateOptions = availableDates
    .map((dateValue) => `<option value="${escapeHtml(dateValue)}" ${selectedDate === dateValue ? 'selected' : ''}>${escapeHtml(formatDashboardDate(dateValue))}</option>`)
    .join('');

  return `
    <div class="section-actions">
      <div class="section-actions-top">
        <button class="action-button" id="run-premarket-btn" ${jobRunning || historical ? 'disabled' : ''}>
          ${historical ? 'Scan disabled (historical view)' : jobRunning && currentJob.label.includes('Premarket') ? 'Running...' : 'Run premarket scan'}
        </button>
        <label class="historical-select-wrap" for="premarket-date-select">
          <span class="historical-select-label">Session</span>
          <select id="premarket-date-select" class="historical-select">
            <option value="">Live (latest)</option>
            ${dateOptions}
          </select>
        </label>
      </div>
      ${selectionHint}
    </div>
  `;
}

function renderPremarketSection(premarket) {
  const run = premarket.latestRun;
  const rowRun = premarket.latestRowRun;
  const historicalBanner = isHistoricalView()
    ? `
      <div class="historical-banner" role="status">
        <div class="historical-banner-copy">
          <span class="historical-banner-eyebrow">Historical mode</span>
          <strong>Viewing historical data — ${escapeHtml(formatDashboardDate(selectedDate))}</strong>
        </div>
        <button class="action-button action-button-sm historical-banner-button" id="back-to-live-btn">Back to live</button>
      </div>
    `
    : '';

  if (!run) {
    return createPanel({
      span: 'span-12',
      eyebrow: 'Scanner pulse',
      title: 'Premarket gap scan',
      copy: 'Top movers from the latest SQLite-backed premarket run, including stored catalyst snippets.',
      status: 'missing',
      metrics: '',
      body: `${historicalBanner}${renderPremarketRunAction()}<div class="callout"><p class="error-copy">${escapeHtml(premarket.emptyMessage)}</p></div>`,
    });
  }

  const metrics = createSummaryMetrics([
    {
      label: 'Latest run',
      value: formatDateTime(run.runAt),
      note: `Run #${run.id}`,
    },
    {
      label: 'Candidates',
      value: formatInteger(run.rowCount),
      note: `Max gap ${formatPercent(run.maxGapPct)}`,
    },
    {
      label: 'Premarket volume',
      value: formatInteger(run.totalPremarketVolume),
      note: `Average gap ${formatPercent(run.averageGapPct)}`,
    },
  ]);

  const rowsMarkup = rowRun && premarket.rows.length
    ? `
      <div class="table-wrap">
        <table class="premarket-table">
          <thead>
            <tr>
              <th></th>
              <th>Symbol</th>
              <th>Gap</th>
              <th>Price</th>
              <th>Volume</th>
              <th>Sector</th>
              <th class="metric-nowrap">RSI</th>
              <th class="metric-nowrap">Rating</th>
              <th class="metric-nowrap">Δ Open</th>
              <th class="catalyst-col">Catalyst</th>
            </tr>
          </thead>
          <tbody>
            ${premarket.rows
              .map(
                (row) => {
                  const tvEnrichment = row.tvEnrichment || null;
                  const sector = tvEnrichment?.sector || '—';
                  const rsiValue = tvEnrichment?.RSI;
                  const changeFromOpen = tvEnrichment?.change_from_open;
                  const rsiClass = rsiValue === null || rsiValue === undefined || Number.isNaN(Number(rsiValue))
                    ? ''
                    : Number(rsiValue) >= 70
                      ? ' metric-rsi-hot'
                      : Number(rsiValue) <= 30
                        ? ' metric-rsi-cool'
                        : '';
                  const changeClass = changeFromOpen === null || changeFromOpen === undefined || Number.isNaN(Number(changeFromOpen))
                    ? ''
                    : Number(changeFromOpen) > 0
                      ? ' metric-positive'
                      : Number(changeFromOpen) < 0
                        ? ' metric-negative'
                        : '';

                   return `
                   <tr>
                    <td><input type="checkbox" class="row-checkbox" data-symbol="${escapeHtml(row.symbol)}" ${selectedSymbols.has(row.symbol) ? 'checked' : ''} ${(currentJob && currentJob.status === 'running') || isHistoricalView() ? 'disabled' : ''} /></td>
                    <td>
                      <div class="symbol-stack">
                        <span class="symbol">${escapeHtml(row.symbol)}</span>
                        <span class="detail-line">Rank ${escapeHtml(row.rank ?? '—')}</span>
                      </div>
                    </td>
                    <td>${escapeHtml(formatPercent(row.gapPct))}</td>
                    <td>${escapeHtml(formatPrice(row.price))}</td>
                    <td>${escapeHtml(formatInteger(row.premarketVolume))}</td>
                    <td class="premarket-sector">${escapeHtml(sector)}</td>
                    <td class="metric-compact metric-nowrap${rsiClass}">${escapeHtml(rsiValue === null || rsiValue === undefined || Number.isNaN(Number(rsiValue)) ? '—' : formatInteger(Math.round(Number(rsiValue))))}</td>
                    <td class="metric-compact metric-nowrap">${formatRecommendation(tvEnrichment?.['Recommend.All'])}</td>
                    <td class="metric-compact metric-nowrap${changeClass}">${escapeHtml(formatSignedPercent(changeFromOpen))}</td>
                    <td class="catalyst-col">${renderCatalystCell(row)}</td>
                  </tr>
                `;
                }
              )
              .join('')}
          </tbody>
        </table>
      </div>
      ${run.id !== rowRun.id ? `<p class="list-note">Latest run has no rows; table is showing the most recent row-bearing run from ${escapeHtml(formatDateTime(rowRun.runAt))}.</p>` : ''
    }
  `
    : `<div class="callout"><p class="error-copy">${escapeHtml(premarket.emptyMessage)}</p></div>`;

  const errorBlock = run.errorMessage
    ? `<div class="callout"><p class="error-copy">${escapeHtml(run.errorMessage)}</p></div>`
    : '';

  return createPanel({
    span: 'span-12',
    eyebrow: 'Scanner pulse',
    title: 'Premarket gap scan',
    copy: 'Top movers from the latest SQLite-backed premarket run, including stored catalyst snippets.',
    status: run.status,
    metrics,
    body: `${historicalBanner}${renderPremarketRunAction()}${rowsMarkup}${errorBlock}`,
  });
}

function renderCollectorSection(collector) {
  if (!collector.exists || collector.error) {
    return createPanel({
      span: 'span-4',
      eyebrow: 'Handoff snapshot',
      title: 'Collector artifact',
      copy: 'Live TradingView payload details appear here when the collector writes today’s JSON artifact.',
      status: collector.error ? 'error' : 'missing',
      metrics: '',
      body: `<div class="callout"><p class="error-copy">${escapeHtml(collector.error || collector.emptyMessage || 'No collector artifact found.')}</p></div>`,
    });
  }

  if (!collector.isToday) {
    const lastRun = collector.collectedAt
      ? formatDateTime(collector.collectedAt)
      : 'No timestamp in artifact';
    return createPanel({
      span: 'span-4',
      eyebrow: 'Handoff snapshot',
      title: 'Collector artifact',
      copy: 'No TradingView data has been collected today. Run a Trend Join Long scan to fetch fresh bars for selected symbols.',
      status: 'empty',
      metrics: '',
      body: `<div class="callout"><p class="eyebrow">Last collection</p><p class="muted">${escapeHtml(lastRun)}</p></div>`,
    });
  }

  const metrics = createSummaryMetrics([
    {
      label: 'Requested',
      value: formatInteger(collector.requestedCount || 0),
      note: collector.intradayTimeframe ? `${collector.intradayTimeframe}-minute bars` : 'No timeframe present',
    },
    {
      label: 'Successful',
      value: formatInteger(collector.successfulCount || 0),
      note: collector.source || 'No source declared',
    },
    {
      label: 'Failed',
      value: formatInteger(collector.failedCount || 0),
      note: collector.collectedAt ? formatDateTime(collector.collectedAt) : 'No collection timestamp',
    },
  ]);

  const failures = collector.failedSymbols?.length
    ? `
      <div class="callout">
        <p class="eyebrow">Collector failures</p>
        <ul class="failure-list">
          ${collector.failedSymbols
            .map(
              (item) => `
                <li>
                  <strong>${escapeHtml(item.symbol || 'Unknown')}</strong>
                  <p class="detail-line">${escapeHtml(item.stage || 'unknown stage')} · ${escapeHtml(item.error || 'No error message')}</p>
                </li>
              `
            )
            .join('')}
        </ul>
      </div>
    `
    : '<p class="list-note">No failed symbols recorded in the current artifact.</p>';

  const summaryCards = Array.isArray(collector.symbolSummaries) && collector.symbolSummaries.length
    ? collector.symbolSummaries.map((entry) => {
      const currentPrice = Number(entry.currentPrice);
      const prevDailyHigh = Number(entry.prevDailyHigh);
      const prevDailyClose = Number(entry.prevDailyClose);
      const sma200 = Number(entry.sma200);
      const pmh = Number(entry.pmh);
      const todayHod = Number(entry.todayHod);
      const hasPriceVsPrevHigh = !Number.isNaN(currentPrice) && !Number.isNaN(prevDailyHigh);
      const hasCloseVsSma = !Number.isNaN(prevDailyClose) && !Number.isNaN(sma200);
      const priceAbovePrevHigh = hasPriceVsPrevHigh && currentPrice > prevDailyHigh;
      const closeAboveSma = hasCloseVsSma && prevDailyClose > sma200;
      const verdictMarkup = entry.tjlResult
        ? `
          <div class="collector-verdict-block">
            ${resultBadge(entry.tjlResult)}
            ${entry.tjlReason ? `<p class="collector-verdict-reason">${escapeHtml(shortenText(entry.tjlReason, 110))}</p>` : ''}
          </div>
        `
        : '<div class="collector-verdict-block collector-verdict-pending"><span class="collector-verdict-label">Awaiting TJL verdict</span></div>';
      const markers = [
        { label: 'Cur', value: currentPrice, className: 'marker-current' },
        { label: 'PMH', value: pmh, className: 'marker-pmh' },
        { label: 'HOD', value: todayHod, className: 'marker-hod' },
      ];
      const breakoutPills = [
        {
          label: entry.dailyBreakout === null || entry.dailyBreakout === undefined
            ? 'Daily breakout unavailable'
            : `Daily breakout ${entry.dailyBreakout ? 'on' : 'off'}`,
          className: entry.dailyBreakout === null || entry.dailyBreakout === undefined
            ? 'collector-level-neutral'
            : entry.dailyBreakout ? 'collector-level-pass' : 'collector-level-fail',
        },
        {
          label: entry.intradayBreakout === null || entry.intradayBreakout === undefined
            ? 'Intraday breakout unavailable'
            : `Intraday breakout ${entry.intradayBreakout ? 'on' : 'off'}`,
          className: entry.intradayBreakout === null || entry.intradayBreakout === undefined
            ? 'collector-level-neutral'
            : entry.intradayBreakout ? 'collector-level-pass' : 'collector-level-fail',
        },
      ];

      return `
        <article class="collector-symbol-card">
          <div class="collector-symbol-head">
            <div class="collector-symbol-title">
              <div class="symbol-stack">
                <span class="symbol">${escapeHtml(entry.symbol)}</span>
                <span class="detail-line">${escapeHtml(entry.description || entry.exchange || 'No description')}</span>
              </div>
            </div>
            <div class="collector-price-stack">
              ${verdictMarkup}
              <strong class="collector-price">${escapeHtml(formatPrice(entry.currentPrice))}</strong>
              <span class="detail-line">${escapeHtml(entry.exchange || 'Unknown exchange')}</span>
            </div>
          </div>
          <div class="collector-chart-grid">
            <section class="collector-chart-shell collector-chart-panel">
              <div class="collector-chart-header">
                <span class="collector-chart-title">Daily candles</span>
                <span class="collector-chart-note">24 sessions</span>
              </div>
              ${renderCandlestickChart(entry.dailyCandles, { markers })}
            </section>
            <section class="collector-chart-shell collector-chart-panel">
              <div class="collector-chart-header">
                <span class="collector-chart-title">Intraday pulse</span>
                <span class="collector-chart-note">${escapeHtml(String(collector.intradayTimeframe || '—'))}-minute · 48 closes</span>
              </div>
              ${renderSparkline(entry.intradayCloses, {
                markers,
                lineClassName: 'sparkline-path sparkline-path-intraday',
                dotClassName: 'sparkline-dot sparkline-dot-intraday',
                areaClassName: 'sparkline-area sparkline-area-intraday',
              })}
            </section>
          </div>
          <div class="collector-chart-legend">
            <span><span class="legend-swatch legend-candle"></span>Daily body</span>
            <span><span class="legend-swatch legend-intraday"></span>Intraday line</span>
            <span><span class="legend-swatch legend-current"></span>Current</span>
            <span><span class="legend-swatch legend-pmh"></span>PMH</span>
            <span><span class="legend-swatch legend-hod"></span>HOD</span>
          </div>
          <div class="collector-criteria-row">
            ${breakoutPills.map((pill) => `<span class="collector-criteria-pill ${pill.className}">${escapeHtml(pill.label)}</span>`).join('')}
            <span class="collector-criteria-pill ${hasCloseVsSma ? (closeAboveSma ? 'collector-level-pass' : 'collector-level-fail') : 'collector-level-neutral'}">
              ${hasCloseVsSma ? `Prev close ${closeAboveSma ? '>' : '<='} SMA200` : 'Prev close vs SMA200 unavailable'}
            </span>
            <span class="collector-criteria-pill ${hasPriceVsPrevHigh ? (priceAbovePrevHigh ? 'collector-level-pass' : 'collector-level-fail') : 'collector-level-neutral'}">
              ${hasPriceVsPrevHigh ? `Price ${priceAbovePrevHigh ? '>' : '<='} Prev high` : 'Price vs prev high unavailable'}
            </span>
          </div>
          <div class="collector-level-grid collector-level-grid-rich">
            <div class="collector-level-chip collector-level-chip-emphasis">
              <span class="collector-level-label">Current</span>
              <strong>${escapeHtml(formatPrice(entry.currentPrice))}</strong>
            </div>
            <div class="collector-level-chip ${Number.isNaN(pmh) || Number.isNaN(currentPrice) ? 'collector-level-neutral' : currentPrice >= pmh ? 'collector-level-pass' : 'collector-level-fail'}">
              <span class="collector-level-label">PMH</span>
              <strong>${escapeHtml(formatPrice(entry.pmh))}</strong>
            </div>
            <div class="collector-level-chip ${Number.isNaN(todayHod) || Number.isNaN(currentPrice) ? 'collector-level-neutral' : currentPrice >= todayHod ? 'collector-level-pass' : 'collector-level-fail'}">
              <span class="collector-level-label">HOD</span>
              <strong>${escapeHtml(formatPrice(entry.todayHod))}</strong>
            </div>
            <div class="collector-level-chip ${hasPriceVsPrevHigh ? (priceAbovePrevHigh ? 'collector-level-pass' : 'collector-level-fail') : 'collector-level-neutral'}">
              <span class="collector-level-label">Prev high</span>
              <strong>${escapeHtml(formatPrice(entry.prevDailyHigh))}</strong>
            </div>
            <div class="collector-level-chip ${hasCloseVsSma ? (closeAboveSma ? 'collector-level-pass' : 'collector-level-fail') : 'collector-level-neutral'}">
              <span class="collector-level-label">SMA200</span>
              <strong>${escapeHtml(formatPrice(entry.sma200))}</strong>
            </div>
            <div class="collector-level-chip">
              <span class="collector-level-label">Prev close</span>
              <strong>${escapeHtml(formatPrice(entry.prevDailyClose))}</strong>
            </div>
          </div>
        </article>
      `;
    }).join('')
    : '<p class="list-note">No symbol summaries available for today\'s collection.</p>';

  const errorBlock = collector.error
    ? `<div class="callout"><p class="error-copy">${escapeHtml(collector.error)}</p></div>`
    : '';

  return createPanel({
    span: 'span-12',
    eyebrow: 'Collected today',
    title: 'TradingView data snapshot',
    copy: 'Daily price history, current quote, and breakout context extracted from the latest TradingView collection.',
    status: 'success',
    metrics,
    body: `
      <div class="collector-meta-strip meta-list">
        <div class="meta-row"><span class="meta-label">Artifact path</span><span>${escapeHtml(collector.path)}</span></div>
        <div class="meta-row"><span class="meta-label">Collected at</span><span>${escapeHtml(formatDateTime(collector.collectedAt))}</span></div>
        <div class="meta-row"><span class="meta-label">Successful symbols</span><span>${escapeHtml((collector.successfulSymbols || []).join(', ') || 'None')}</span></div>
      </div>
      <div class="collector-summary-grid">${summaryCards}</div>
      ${failures}
      ${errorBlock}
    `,
  });
}

function renderTjlSection(tjl) {
  const run = tjl.latestRun;
  const rowRun = tjl.latestRowRun;

  if (!run) {
    return emptyState('Trend Join Long run not available', tjl.emptyMessage);
  }

  const metrics = createSummaryMetrics([
    {
      label: 'Latest run',
      value: formatDateTime(run.runAt),
      note: `Run #${run.id}`,
    },
    {
      label: 'Passes',
      value: formatInteger(tjl.breakdown.passCount),
      note: rowRun ? `Rows from run #${rowRun.id}` : 'No row-bearing run yet',
    },
    {
      label: 'Non-pass',
      value: formatInteger(tjl.breakdown.nonPassCount),
      note: run.rowCount ? `${formatInteger(run.rowCount)} latest row(s)` : 'Latest run stored no rows',
    },
  ]);

  const rowsMarkup = rowRun && tjl.rows.length
    ? `
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Result</th>
              <th>Price</th>
              <th>Breakout state</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            ${tjl.rows
              .map(
                (row) => `
                  <tr>
                    <td>
                      <div class="symbol-stack">
                        <span class="symbol">${escapeHtml(row.symbol)}</span>
                        <span class="detail-line">${escapeHtml(row.timeframe || 'No timeframe stored')}</span>
                      </div>
                    </td>
                    <td>${resultBadge(row.result)}</td>
                    <td>${escapeHtml(formatPrice(row.price))}</td>
                    <td>
                      <div class="symbol-stack">
                        <span class="detail-line">Daily breakout: ${escapeHtml(String(row.dailyBreakout))}</span>
                        <span class="detail-line">Intraday breakout: ${escapeHtml(String(row.intradayBreakout))}</span>
                      </div>
                    </td>
                    <td>
                      <div class="symbol-stack">
                        <span>${escapeHtml(row.reason || 'No reason stored')}</span>
                        <span class="detail-line">Prev high ${escapeHtml(formatDecimal(row.prevDailyHigh))} · PMH ${escapeHtml(formatDecimal(row.pmh))} · HOD ${escapeHtml(formatDecimal(row.todayHod))}</span>
                      </div>
                    </td>
                  </tr>
                `
              )
              .join('')}
          </tbody>
        </table>
      </div>
      ${run.id !== rowRun.id ? `<p class="list-note">Latest TJL run has no persisted rows; table is showing the most recent row-bearing run from ${escapeHtml(formatDateTime(rowRun.runAt))}.</p>` : ''}
    `
    : `<div class="callout"><p class="error-copy">${escapeHtml(tjl.emptyMessage)}</p></div>`;

  const errorBlock = run.errorMessage
    ? `<div class="callout"><p class="error-copy">${escapeHtml(run.errorMessage)}</p></div>`
    : '';

  return createPanel({
    span: 'span-7',
    eyebrow: 'Strategy verdict',
    title: 'Trend Join Long',
    copy: 'The most recent TJL summary plus the freshest row-bearing evaluation snapshot for symbol-level inspection.',
    status: run.status,
    metrics,
    body: `${rowsMarkup}${errorBlock}`,
  });
}

function emptyState(title, message) {
  return `
    <section class="panel empty-state span-12">
      <p class="eyebrow">Missing data</p>
      <h2>${escapeHtml(title)}</h2>
      <p class="muted">${escapeHtml(message)}</p>
    </section>
  `;
}

async function loadDashboard() {
  const app = document.getElementById('app');

  const loadingPanel = document.getElementById('app-loading');
  if (loadingPanel) {
    loadingPanel.remove();
  }

  try {
    const url = '/api/dashboard' + (selectedDate ? `?date=${encodeURIComponent(selectedDate)}` : '');
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`Dashboard request failed with ${response.status}`);
    }

    const data = await response.json();
    selectedDate = data.dateOverride || null;
    currentDashboardData = data;
    renderHeroMeta(data);
    app.innerHTML = `
      <div id="job-banner-mount" class="span-12">${renderJobBanner()}</div>
      ${renderPremarketSection(data.premarket)}
      ${renderTjlSection(data.tjl)}
      ${renderCollectorSection(data.collector)}
      <div id="learn-section-mount" class="span-12">${renderLearnSection()}</div>
      <div id="news-modal-mount"></div>
    `;
    recheckSelectedRows();
    const actionBarMount = document.getElementById('action-bar-mount') || document.createElement('div');
    actionBarMount.id = 'action-bar-mount';
    actionBarMount.innerHTML = renderActionBar();
    if (!actionBarMount.parentElement) {
      document.body.appendChild(actionBarMount);
    }
    attachInteractionListeners();
    refreshNewsModal();
  } catch (error) {
    currentDashboardData = null;
    activeNewsRowId = null;
    document.body.classList.remove('news-modal-open');
    document.getElementById('hero-meta').innerHTML = '';
    app.innerHTML = emptyState('Dashboard failed to load', error instanceof Error ? error.message : String(error));
  }
}

attachInteractionListeners();
loadDashboard();
