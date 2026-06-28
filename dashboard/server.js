const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { URL } = require('node:url');
const { exec } = require('node:child_process');
const crypto = require('node:crypto');
const Database = require('better-sqlite3');

const HOST = '127.0.0.1';
const PORT = Number.parseInt(process.env.DASHBOARD_PORT || '3020', 10);
const ROOT_DIR = path.resolve(__dirname, '..');
const STATIC_DIR = path.join(__dirname, 'static');
const DB_PATH = path.join(ROOT_DIR, 'data', 'bot-trader.db');
const COLLECTOR_ARTIFACT_PATH = path.join(ROOT_DIR, '.cache', 'tjl-live-input-today.json');
const TRADINGVIEW_CLI_PATH = path.join(ROOT_DIR, 'tradingview-mcp', 'src', 'cli', 'index.js');
const TRADINGVIEW_CLI_CWD = path.join(ROOT_DIR, 'tradingview-mcp');
const TRADINGVIEW_MAC_LAUNCH_SCRIPT = path.join(ROOT_DIR, 'scripts', 'launch_tv_debug_mac.sh');

const MIME_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
};

function sendJson(response, statusCode, payload) {
  let body;
  try {
    body = JSON.stringify(payload);
  } catch (error) {
    response.writeHead(500, {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    response.end('Internal Server Error: unable to serialize response');
    return;
  }

  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  response.end(body);
}

function sendText(response, statusCode, body, contentType) {
  response.writeHead(statusCode, {
    'Content-Type': contentType,
    'Cache-Control': 'no-store',
  });
  response.end(body);
}

function safeReadJson(filePath) {
  if (!fs.existsSync(filePath)) {
    return { exists: false, data: null, error: null };
  }

  try {
    return {
      exists: true,
      data: JSON.parse(fs.readFileSync(filePath, 'utf8')),
      error: null,
    };
  } catch (error) {
    return {
      exists: true,
      data: null,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

function safeParseMetadata(value) {
  if (!value || typeof value !== 'string') {
    return {};
  }

  try {
    return JSON.parse(value);
  } catch {
    return {};
  }
}

const SCRIPTS_DIR = path.join(ROOT_DIR, 'scripts');
const COLLECTOR_ARTIFACT_PATH_FOR_RUNS = path.join(ROOT_DIR, '.cache', 'tjl-live-input-today.json');
const PREMARKET_TIMEOUT_MS = 90_000;
const TJL_COLLECT_TIMEOUT_MS = 600_000;

const jobs = new Map();
const MAX_RECENT_JOBS = 20;

function createJob(type, label) {
  const job = {
    id: crypto.randomUUID().slice(0, 8),
    type,
    label,
    status: 'running',
    output: '',
    error: null,
    startedAt: new Date().toISOString(),
    completedAt: null,
  };
  jobs.set(job.id, job);
  trimRecentJobs();
  return job;
}

function trimRecentJobs() {
  if (jobs.size <= MAX_RECENT_JOBS) {
    return;
  }
  const oldest = [...jobs.values()]
    .sort((a, b) => new Date(a.startedAt) - new Date(b.startedAt))
    .slice(0, jobs.size - MAX_RECENT_JOBS);
  for (const job of oldest) {
    jobs.delete(job.id);
  }
}

function completeJob(jobId, { output = '', error = null }) {
  const job = jobs.get(jobId);
  if (!job) {
    return;
  }
  job.status = error ? 'error' : 'success';
  job.output = output;
  job.error = error;
  job.completedAt = new Date().toISOString();
}

function runScript(command, { cwd = ROOT_DIR, timeoutMs = 60_000 }) {
  return new Promise((resolve) => {
    exec(command, { cwd, timeout: timeoutMs, maxBuffer: 1024 * 1024 * 2 }, (error, stdout, stderr) => {
      const output = [stdout, stderr].filter(Boolean).join('\n').trim();
      if (error) {
        resolve({ output, error: error.killed ? `timed out after ${timeoutMs / 1000}s` : (error.message || String(error)) });
      } else {
        resolve({ output, error: null });
      }
    });
  });
}

function summarizeFailedCommand(result, prefix = '') {
  const lines = String(result.output || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const meaningful = [...lines].reverse().find((line) => !line.startsWith('Command failed:')) || result.error;
  const error = prefix ? `${prefix}: ${meaningful}` : meaningful;
  const output = lines.length <= 1 && meaningful === lines[0] ? '' : result.output;
  return { error, output };
}

function tradingViewConnectionHelp(detail) {
  const suffix = detail ? `\n\nDetails: ${detail}` : '';
  return [
    'TradingView Desktop is not connected to CDP on port 9222.',
    'Launch or restart TradingView with remote debugging enabled, open a real chart tab, then retry Trend Join Long.',
    'OpenCode option: run the TradingView `tv_launch` tool, then `tv_health_check`.',
    'Manual macOS option: /Applications/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=9222',
  ].join('\n') + suffix;
}

function shouldAttemptTradingViewLaunch(detail) {
  const text = String(detail || '').toLowerCase();
  return (
    process.platform === 'darwin'
    && fs.existsSync(TRADINGVIEW_MAC_LAUNCH_SCRIPT)
    && (
      text.includes('cdp connection failed')
      || text.includes('fetch failed')
      || text.includes('econnrefused')
      || text.includes('tradingview cli reported an unhealthy connection')
    )
  );
}

async function launchTradingViewForTjl() {
  const command = `bash "${TRADINGVIEW_MAC_LAUNCH_SCRIPT}" 9222`;
  return runScript(command, { cwd: ROOT_DIR, timeoutMs: 30_000 });
}

function parseTradingViewStatus(result) {
  try {
    return JSON.parse(result.output || '{}');
  } catch (error) {
    throw new Error(`status returned non-JSON output: ${error instanceof Error ? error.message : String(error)}`);
  }
}

async function checkTradingViewBeforeTjl() {
  const command = `node "${TRADINGVIEW_CLI_PATH}" status`;
  let result = await runScript(command, { cwd: TRADINGVIEW_CLI_CWD, timeoutMs: 20_000 });
  if (result.error && shouldAttemptTradingViewLaunch(result.error)) {
    const launchResult = await launchTradingViewForTjl();
    if (launchResult.error) {
      const launchDetail = launchResult.output || launchResult.error;
      return { ok: false, output: launchResult.output, error: tradingViewConnectionHelp(`auto-launch failed: ${launchDetail}`) };
    }
    result = await runScript(command, { cwd: TRADINGVIEW_CLI_CWD, timeoutMs: 20_000 });
  }
  if (result.error) {
    return { ok: false, output: result.output, error: tradingViewConnectionHelp(result.error) };
  }

  let status;
  try {
    status = parseTradingViewStatus(result);
  } catch (error) {
    return { ok: false, output: result.output, error: tradingViewConnectionHelp(error instanceof Error ? error.message : String(error)) };
  }

  if (!status.cdp_connected || !status.api_available) {
    const reason = status.error || `cdp_connected=${status.cdp_connected}, api_available=${status.api_available}`;
    return { ok: false, output: result.output, error: tradingViewConnectionHelp(reason) };
  }

  return { ok: true, output: result.output };
}

async function runPremarketJob(jobId) {
  const result = await runScript('python3 scripts/scanner-premarket.py', { timeoutMs: PREMARKET_TIMEOUT_MS });
  completeJob(jobId, result);
}

function resolveTvSymbols(symbols) {
  if (!fs.existsSync(DB_PATH)) {
    return symbols;
  }

  let db;
  try {
    db = new Database(DB_PATH, { readonly: true, fileMustExist: true });
    const latestRun = db
      .prepare(`SELECT id FROM scan_runs WHERE scanner_name = 'premarket-gap-scan' AND status = 'success' ORDER BY id DESC LIMIT 1`)
      .get();

    if (!latestRun) {
      return symbols;
    }

    const rows = db
      .prepare(`SELECT symbol, metadata FROM scan_results WHERE scan_run_id = ?`)
      .all(latestRun.id);

    const tvMap = {};
    for (const row of rows) {
      const meta = safeParseMetadata(row.metadata);
      if (meta.tv_symbol) {
        tvMap[row.symbol.toUpperCase()] = meta.tv_symbol;
      }
    }

    return symbols.map((s) => tvMap[s.toUpperCase()] || s);
  } catch {
    return symbols;
  } finally {
    if (db) {
      db.close();
    }
  }
}

async function runTjlJob(jobId, symbols, intradayTimeframe) {
  const cleanSymbols = symbols.filter(Boolean);
  const qualifiedSymbols = resolveTvSymbols(cleanSymbols);
  const symbolsArg = qualifiedSymbols.join(',');
  const preflight = await checkTradingViewBeforeTjl();
  if (!preflight.ok) {
    completeJob(jobId, {
      output: preflight.output,
      error: preflight.error,
    });
    return;
  }

  const collectCommand = `python3 scripts/collect-tjl-input.py --symbols "${symbolsArg}" --intraday-timeframe ${intradayTimeframe} --output "${COLLECTOR_ARTIFACT_PATH_FOR_RUNS}"`;
  const collectResult = await runScript(collectCommand, { timeoutMs: TJL_COLLECT_TIMEOUT_MS });
  if (collectResult.error) {
    const summarized = summarizeFailedCommand(collectResult, 'collector failed');
    completeJob(jobId, {
      output: summarized.output,
      error: summarized.error,
    });
    return;
  }

  const scanCommand = `python3 scripts/scanner-trend-join-long.py --input "${COLLECTOR_ARTIFACT_PATH_FOR_RUNS}" --intraday-timeframe ${intradayTimeframe}`;
  const scanResult = await runScript(scanCommand, { timeoutMs: PREMARKET_TIMEOUT_MS });
  if (scanResult.error) {
    completeJob(jobId, summarizeFailedCommand(scanResult));
    return;
  }
  completeJob(jobId, scanResult);
}

function readJsonBody(request) {
  return new Promise((resolve, reject) => {
    let body = '';
    request.on('data', (chunk) => {
      body += chunk;
      if (body.length > 256 * 1024) {
        reject(new Error('request body too large'));
      }
    });
    request.on('end', () => {
      if (!body) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(body));
      } catch (error) {
        reject(new Error('invalid JSON body'));
      }
    });
    request.on('error', reject);
  });
}

function serializeJob(job) {
  if (!job) {
    return null;
  }
  const output = job.output || '';
  return {
    id: job.id,
    type: job.type,
    label: job.label,
    status: job.status,
    output: output.slice(-4000),
    error: job.error,
    startedAt: job.startedAt,
    completedAt: job.completedAt,
  };
}

function buildRecentJobsSection() {
  const recent = [...jobs.values()]
    .sort((a, b) => new Date(b.startedAt) - new Date(a.startedAt))
    .slice(0, 5);
  return recent.map(serializeJob);
}

function openDatabase() {
  if (!fs.existsSync(DB_PATH)) {
    return null;
  }

  return new Database(DB_PATH, { readonly: true, fileMustExist: true });
}

function fetchLatestRun(db, scannerName, dateOverride) {
  const dateClause = dateOverride ? 'AND DATE(run_at) = DATE(?)' : '';
  const params = dateOverride ? [scannerName, dateOverride] : [scannerName];
  return db
    .prepare(
      `
        SELECT id, scanner_name, run_at, status, error_message, watchlist, criteria
        FROM scan_runs
        WHERE scanner_name = ?
          ${dateClause}
        ORDER BY datetime(run_at) DESC, id DESC
        LIMIT 1
      `
    )
    .get(...params);
}

function fetchLatestRunWithRows(db, scannerName, dateOverride) {
  const dateClause = dateOverride ? 'AND DATE(sr.run_at) = DATE(?)' : '';
  const params = dateOverride ? [scannerName, dateOverride] : [scannerName];
  return db
    .prepare(
      `
        SELECT sr.id, sr.scanner_name, sr.run_at, sr.status, sr.error_message, sr.watchlist, sr.criteria
        FROM scan_runs sr
        WHERE sr.scanner_name = ?
          ${dateClause}
          AND EXISTS (
            SELECT 1
            FROM scan_results results
            WHERE results.scan_run_id = sr.id
          )
        ORDER BY datetime(sr.run_at) DESC, sr.id DESC
        LIMIT 1
      `
    )
    .get(...params);
}

function fetchRowsForRun(db, runId, limit) {
  if (!runId) {
    return [];
  }

  return db
    .prepare(
      `
        SELECT id, scan_run_id, symbol, gap_pct, premarket_volume, price, timeframe, metadata
        FROM scan_results
        WHERE scan_run_id = ?
        ORDER BY id DESC
        LIMIT ?
      `
    )
    .all(runId, limit);
}

function fetchAvailableDates(db, scannerName) {
  return db
    .prepare(
      `
        SELECT DISTINCT DATE(run_at) as date
        FROM scan_runs
        WHERE scanner_name = ?
          AND status = 'success'
        ORDER BY DATE(run_at) DESC
      `
    )
    .all(scannerName)
    .map((row) => row.date);
}

function fetchRunStats(db, runId) {
  if (!runId) {
    return null;
  }

  return db
    .prepare(
      `
        SELECT
          COUNT(*) AS row_count,
          AVG(gap_pct) AS average_gap_pct,
          MAX(gap_pct) AS max_gap_pct,
          SUM(COALESCE(premarket_volume, 0)) AS total_premarket_volume
        FROM scan_results
        WHERE scan_run_id = ?
      `
    )
    .get(runId);
}

function summarizeTjlRows(rows) {
  return rows.reduce(
    (summary, row) => {
      const result = String(row.result || '').toUpperCase();

      if (result === 'PASS') {
        summary.passCount += 1;
      } else if (result) {
        summary.nonPassCount += 1;
      }

      return summary;
    },
    { passCount: 0, nonPassCount: 0 }
  );
}

function normalizeRun(run, stats) {
  if (!run) {
    return null;
  }

  return {
    id: run.id,
    scannerName: run.scanner_name,
    runAt: run.run_at,
    status: run.status,
    errorMessage: run.error_message,
    watchlist: run.watchlist,
    criteria: run.criteria,
    rowCount: stats?.row_count || 0,
    averageGapPct: stats?.average_gap_pct ?? null,
    maxGapPct: stats?.max_gap_pct ?? null,
    totalPremarketVolume: stats?.total_premarket_volume ?? null,
  };
}

function buildPremarketSection(db, dateOverride) {
  const latestRun = fetchLatestRun(db, 'premarket-gap-scan', dateOverride);
  const latestRowRun = fetchLatestRunWithRows(db, 'premarket-gap-scan', dateOverride);
  const latestRunStats = fetchRunStats(db, latestRun?.id);
  const rowRunStats = latestRowRun && latestRowRun.id !== latestRun?.id ? fetchRunStats(db, latestRowRun.id) : latestRunStats;
  const rows = fetchRowsForRun(db, latestRowRun?.id, 10).map((row) => {
    const metadata = safeParseMetadata(row.metadata);

    return {
      id: row.id,
      symbol: row.symbol,
      gapPct: row.gap_pct,
      premarketVolume: row.premarket_volume,
      price: row.price,
      rank: metadata.rank ?? null,
      catalyst: metadata.catalyst || null,
      headlines: Array.isArray(metadata.headlines) ? metadata.headlines : [],
      newsItems: Array.isArray(metadata.news_items) ? metadata.news_items : [],
      catalystError: metadata.catalyst_error || null,
      tvEnrichment: metadata.tv_enrichment || null,
      artifactPath: metadata.artifact_path || null,
    };
  }).sort((a, b) => {
    const aRank = a.rank ?? Number.POSITIVE_INFINITY;
    const bRank = b.rank ?? Number.POSITIVE_INFINITY;
    return aRank - bRank;
  });

  return {
    latestRun: normalizeRun(latestRun, latestRunStats),
    latestRowRun: normalizeRun(latestRowRun, rowRunStats),
    rows,
    emptyMessage: latestRun
      ? 'No premarket candidate rows were persisted for the latest available run.'
      : 'No premarket scanner run is stored in SQLite yet.',
  };
}

function isFromToday(isoString) {
  if (!isoString) {
    return false;
  }
  try {
    const formatDate = (value) => new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(value);
    return formatDate(new Date(isoString)) === formatDate(new Date());
  } catch {
    return false;
  }
}

function buildLatestTjlRowMap(db) {
  if (!db) {
    return {};
  }

  const latestRowRun = fetchLatestRunWithRows(db, 'trend-join-long-scan');
  if (!latestRowRun?.id) {
    return {};
  }

  const rows = db
    .prepare(
      `
        SELECT symbol, metadata
        FROM scan_results
        WHERE scan_run_id = ?
      `
    )
    .all(latestRowRun.id);

  const bySymbol = {};
  for (const row of rows) {
    const metadata = safeParseMetadata(row.metadata);
    bySymbol[String(row.symbol || '').toUpperCase()] = {
      result: metadata.result || null,
      reason: metadata.reason || null,
      dailyBreakout: metadata.daily_breakout ?? null,
      intradayBreakout: metadata.intraday_breakout ?? null,
      prevDailyHigh: metadata.prev_daily_high ?? null,
      prevDailyClose: metadata.prev_daily_close ?? null,
      sma200: metadata.sma200 ?? null,
      pmh: metadata.pmh ?? null,
      todayHod: metadata.today_hod ?? null,
    };
  }

  return bySymbol;
}

function summarizeCollectorSymbols(payload, tjlBySymbol = {}) {
  const symbols = Array.isArray(payload.symbols) ? payload.symbols : [];
  return symbols.map((entry) => {
    const quote = entry.quote || {};
    const dailyBars = Array.isArray(entry.daily_bars) ? entry.daily_bars : [];
    const minuteBars = Array.isArray(entry.minute_bars) ? entry.minute_bars : [];
    const closes = dailyBars.map((b) => Number(b.close)).filter((v) => !Number.isNaN(v));
    const sparklineCloses = closes.slice(-60);
    const intradayCloses = minuteBars.map((b) => Number(b.close)).filter((v) => !Number.isNaN(v)).slice(-48);
    const dailyCandles = dailyBars.slice(-24).map((bar) => ({
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    }));

    const smaPeriod = Math.min(200, closes.length);
    const sma200 = smaPeriod > 0
      ? closes.slice(-smaPeriod).reduce((sum, v) => sum + v, 0) / smaPeriod
      : null;

    const prevBar = dailyBars.length >= 2 ? dailyBars[dailyBars.length - 2] : null;
    const prevDailyHigh = prevBar ? Number(prevBar.high) : null;
    const prevDailyClose = prevBar ? Number(prevBar.close) : null;
    const symbolKey = String(entry.symbol || quote.symbol || '').toUpperCase();
    const latestTjl = tjlBySymbol[symbolKey] || {};

    return {
      symbol: entry.symbol || quote.symbol || 'Unknown',
      description: quote.description || '',
      exchange: quote.exchange || '',
      currentPrice: quote.last !== undefined && quote.last !== null
        ? Number(quote.last)
        : (quote.close !== undefined && quote.close !== null ? Number(quote.close) : null),
      sparklineCloses,
      intradayCloses,
      dailyCandles,
      sma200: sma200 !== null ? Math.round(sma200 * 100) / 100 : null,
      prevDailyHigh: prevDailyHigh !== null ? Math.round(prevDailyHigh * 100) / 100 : null,
      prevDailyClose: prevDailyClose !== null ? Math.round(prevDailyClose * 100) / 100 : null,
      tjlResult: latestTjl.result || null,
      tjlReason: latestTjl.reason || null,
      dailyBreakout: latestTjl.dailyBreakout ?? null,
      intradayBreakout: latestTjl.intradayBreakout ?? null,
      pmh: latestTjl.pmh ?? null,
      todayHod: latestTjl.todayHod ?? null,
    };
  });
}

function buildCollectorSection(db = null) {
  const artifact = safeReadJson(COLLECTOR_ARTIFACT_PATH);

  if (!artifact.exists) {
    return {
      exists: false,
      path: COLLECTOR_ARTIFACT_PATH,
      error: null,
      emptyMessage: 'Collector artifact not found yet. Run collect-tjl-input to surface live payload details here.',
    };
  }

  if (artifact.error) {
    return {
      exists: true,
      path: COLLECTOR_ARTIFACT_PATH,
      error: artifact.error,
      emptyMessage: 'Collector artifact exists but could not be parsed.',
    };
  }

  const payload = artifact.data || {};
  const requestedSymbols = Array.isArray(payload.requested_symbols)
    ? payload.requested_symbols
    : Array.isArray(payload.symbols)
      ? payload.symbols.map((item) => item.symbol).filter(Boolean)
      : [];
  const successfulSymbols = Array.isArray(payload.successful_symbols)
    ? payload.successful_symbols
    : Array.isArray(payload.symbols)
      ? payload.symbols.map((item) => item.symbol).filter(Boolean)
      : [];
  const failedSymbols = Array.isArray(payload.failed_symbols) ? payload.failed_symbols : [];
  const tjlBySymbol = buildLatestTjlRowMap(db);

  return {
    exists: true,
    path: COLLECTOR_ARTIFACT_PATH,
    error: null,
    source: payload.source || null,
    collectedAt: payload.collected_at || null,
    isToday: isFromToday(payload.collected_at),
    intradayTimeframe: payload.intraday_timeframe || null,
    requestedSymbols,
    successfulSymbols,
    failedSymbols,
    requestedCount: requestedSymbols.length,
    successfulCount: successfulSymbols.length,
    failedCount: failedSymbols.length,
    symbolSummaries: isFromToday(payload.collected_at) ? summarizeCollectorSymbols(payload, tjlBySymbol) : [],
  };
}

function buildTjlSection(db) {
  const latestRun = fetchLatestRun(db, 'trend-join-long-scan');
  const latestRowRun = fetchLatestRunWithRows(db, 'trend-join-long-scan');
  const latestRunStats = fetchRunStats(db, latestRun?.id);
  const rowRunStats = latestRowRun && latestRowRun.id !== latestRun?.id ? fetchRunStats(db, latestRowRun.id) : latestRunStats;
  const rows = fetchRowsForRun(db, latestRowRun?.id, 8).map((row) => {
    const metadata = safeParseMetadata(row.metadata);

    return {
      id: row.id,
      symbol: row.symbol,
      price: row.price,
      timeframe: row.timeframe,
      result: metadata.result || null,
      reason: metadata.reason || null,
      dailyBreakout: metadata.daily_breakout ?? null,
      intradayBreakout: metadata.intraday_breakout ?? null,
      prevDailyHigh: metadata.prev_daily_high ?? null,
      prevDailyClose: metadata.prev_daily_close ?? null,
      sma200: metadata.sma200 ?? null,
      pmh: metadata.pmh ?? null,
      todayHod: metadata.today_hod ?? null,
      artifactPath: metadata.artifact_path || null,
    };
  });

  return {
    latestRun: normalizeRun(latestRun, latestRunStats),
    latestRowRun: normalizeRun(latestRowRun, rowRunStats),
    rows,
    breakdown: summarizeTjlRows(rows),
    emptyMessage: latestRun
      ? 'No TJL result rows are attached to the latest available row-bearing run.'
      : 'No Trend Join Long scanner run is stored in SQLite yet.',
  };
}

function buildDashboardPayload(dateOverride) {
  const db = openDatabase();

  if (!db) {
    return {
      generatedAt: new Date().toISOString(),
      db: {
        exists: false,
        path: DB_PATH,
      },
      collector: buildCollectorSection(null),
      premarket: {
        latestRun: null,
        latestRowRun: null,
        rows: [],
        emptyMessage: 'SQLite database is missing. Run migrations and scanners first.',
      },
      tjl: {
        latestRun: null,
        latestRowRun: null,
        rows: [],
        breakdown: { passCount: 0, nonPassCount: 0 },
        emptyMessage: 'SQLite database is missing. Run migrations and scanners first.',
      },
      recentJobs: buildRecentJobsSection(),
    };
  }

  try {
    return {
      generatedAt: new Date().toISOString(),
      dateOverride: dateOverride || null,
      db: {
        exists: true,
        path: DB_PATH,
      },
      collector: buildCollectorSection(db),
      premarket: buildPremarketSection(db, dateOverride),
      tjl: buildTjlSection(db),
      recentJobs: buildRecentJobsSection(),
      availableDates: fetchAvailableDates(db, 'premarket-gap-scan'),
    };
  } finally {
    db.close();
  }
}

function serveStaticFile(response, filePath) {
  if (!filePath.startsWith(STATIC_DIR + path.sep)) {
    sendText(response, 403, 'Forbidden', 'text/plain; charset=utf-8');
    return;
  }

  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    sendText(response, 404, 'Not found', 'text/plain; charset=utf-8');
    return;
  }

  const extension = path.extname(filePath);
  const contentType = MIME_TYPES[extension] || 'application/octet-stream';
  sendText(response, 200, fs.readFileSync(filePath), contentType);
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url || '/', `http://${HOST}:${PORT}`);

  if (request.method === 'GET') {
    if (url.pathname === '/api/dashboard') {
      try {
        const dateOverride = url.searchParams.get('date') || null;
        sendJson(response, 200, buildDashboardPayload(dateOverride));
      } catch (error) {
        sendJson(response, 500, {
          error: error instanceof Error ? error.message : String(error),
        });
      }
      return;
    }

    if (url.pathname === '/api/jobs') {
      sendJson(response, 200, { jobs: buildRecentJobsSection() });
      return;
    }

    const jobMatch = url.pathname.match(/^\/api\/jobs\/([a-f0-9]+)$/);
    if (jobMatch) {
      const job = jobs.get(jobMatch[1]);
      if (!job) {
        sendJson(response, 404, { error: 'job not found' });
      } else {
        sendJson(response, 200, serializeJob(job));
      }
      return;
    }

    if (url.pathname === '/' || url.pathname === '/index.html') {
      serveStaticFile(response, path.join(STATIC_DIR, 'index.html'));
      return;
    }

    const relativePath = url.pathname.replace(/^\/+/, '').replace(/\//g, path.sep);
    serveStaticFile(response, path.resolve(STATIC_DIR, relativePath));
    return;
  }

  if (request.method === 'POST') {
    if (url.pathname === '/api/run/premarket') {
      const job = createJob('premarket', 'Premarket gap scan');
      sendJson(response, 202, { jobId: job.id, status: job.status });
      runPremarketJob(job.id);
      return;
    }

    if (url.pathname === '/api/run/tjl') {
      try {
        const body = await readJsonBody(request);
        const symbols = Array.isArray(body.symbols)
          ? body.symbols.map((s) => String(s).trim().toUpperCase()).filter(Boolean)
          : [];
        const intradayTimeframe = ['1', '5', '10', '15', '30', '60'].includes(String(body.intradayTimeframe))
          ? String(body.intradayTimeframe)
          : '15';

        if (symbols.length === 0) {
          sendJson(response, 400, { error: 'at least one symbol is required' });
          return;
        }

        const job = createJob('tjl', `TJL scan: ${symbols.join(', ')}`);
        sendJson(response, 202, { jobId: job.id, status: job.status });
        runTjlJob(job.id, symbols, intradayTimeframe);
      } catch (error) {
        sendJson(response, 400, { error: error instanceof Error ? error.message : String(error) });
      }
      return;
    }
  }

  sendText(response, 405, 'Method not allowed', 'text/plain; charset=utf-8');
});

server.listen(PORT, HOST, () => {
  console.log(`Bot Trader dashboard running at http://${HOST}:${PORT}`);
});
