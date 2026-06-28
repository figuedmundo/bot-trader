# Bot Trader

Local, AI-assisted trading research tooling built around OpenCode and the TradingView MCP bridge. The project is intentionally research-only: it scans, records, visualizes, and alerts, but it does not execute live trades or store broker credentials.

## Current status

Phase 0 is being finalized. TradingView MCP is configured through `opencode.json`, and `tv_health_check` has been verified with `cdp_connected: true` and `api_available: true`.


## Premarket gappers scanner

The premarket scanner:

```bash
python3 scripts/scanner-premarket.py
```

For troubleshooting source data, run it with detailed progress logs on stderr:

```bash
python3 scripts/scanner-premarket.py --log
```

Output files match `premarket_gappers_YYYY-MM-DD.json` and are ignored by git because they are runtime scan artifacts.

The JSON artifact is mainly for debugging and auditability. The canonical scanner-to-scanner handoff source is SQLite: `scanner-premarket.py` persists each candidate into `scan_runs` and `scan_results`, and downstream orchestration should read candidates back from the database.

The scanner is stdlib-only by default. For more reliable JS-rendered fallback scraping, install optional Crawl4AI support:

```bash
python3 -m pip install crawl4ai
crawl4ai-setup
crawl4ai-doctor
```

Crawl4AI is used only as a fallback when normal HTTP fetches fail or return browser-gated pages; it does not guarantee that public data sources will always be available.

## Trend Join Long scanner

The Trend Join Long scanner evaluates OpenCode-collected TradingView MCP data.

```bash
python3 scripts/scanner-trend-join-long.py --input path/to/tjl-input.json --intraday-timeframe 1
```

OpenCode should collect the live chart data sequentially through the TradingView MCP bridge, then pass normalized daily bars, quote data, and intraday bars into this script.

The scanner derives its symbol universe from the normalized input payload. If no payload symbols are present, it falls back to the small demo universe from the tutorial (`AMD`, `NVDA`, `MU`).

`--intraday-timeframe` defaults to `1` and supports `1`, `5`, `10`, and `15` directly. `30` and `60` are available as experimental modes. `240` / `4h` is intentionally rejected for this scanner because the PMH/HOD logic is meant for intraday decision-making, not swing-style bars.

Important: this script does not resample data for you. The upstream TradingView MCP collection step must fetch intraday bars at the same resolution you pass to `--intraday-timeframe`, or the scan metadata will not match the bars being evaluated.

For non-default modes like `5`, `10`, `15`, `30`, or `60`, the input JSON should also declare that cadence with `intraday_timeframe` so the scanner can reject mismatched or ambiguous payloads.

The scanner evaluates whenever it is invoked. The idea is to run the scripts automatically using cron, launchd, or similar scheduling.

## Premarket → TJL bridge

The bridge script connects the two scanners without making JSON artifacts the source of truth:

```bash
python3 scripts/bridge-premarket-to-tjl.py \
  --input path/to/normalized-tradingview-payload.json \
  --intraday-timeframe 15
```

What it does:

1. Reads the latest successful premarket scan candidates from SQLite.
2. Filters the normalized TradingView payload down to those candidate symbols.
3. Runs the Trend Join Long evaluator on the overlapping set.
4. Writes the normal `tjl_watchlist_YYYY-MM-DD_HHMMET.json` artifact and persists TJL results back into SQLite.

Important: the bridge does **not** collect TradingView data by itself. You still need an upstream MCP collection step that produces normalized daily bars, quote data, and intraday bars for the candidate symbols you want to evaluate.

## TradingView collector step

The collector script builds that normalized TradingView payload automatically for the latest successful premarket candidates stored in SQLite:

```bash
python3 scripts/collect-tjl-input.py \
  --intraday-timeframe 15 \
  --output .cache/tjl-live-input-today.json
```

What it does:

1. Reads the latest successful premarket scan from SQLite.
2. Selects candidates in rank order from `scan_results`.
3. Uses the local `tradingview-mcp` CLI to collect, for each symbol, a quote, `210` daily bars, and intraday bars at the requested timeframe.
4. Writes a normalized payload compatible with the TJL scanner and the DB-backed bridge.
5. Records any per-symbol collection failures in `failed_symbols` instead of aborting the whole run, as long as at least one symbol succeeds.

The current normalized collector payload uses:

- top-level `source`
- top-level `intraday_timeframe`
- `symbols[]`
- `failed_symbols[]`
- per symbol: `symbol`, `quote`, `daily_bars`, `minute_bars`, `intraday_timeframe`

The TJL path intentionally accepts both `minute_bars` (collector shape) and `intraday_bars` (older test/helper shape), but the collector writes `minute_bars`.

## End-to-end staged workflow

```bash
python3 scripts/scanner-premarket.py
python3 scripts/collect-tjl-input.py --intraday-timeframe 15 --output .cache/tjl-live-input-today.json
python3 scripts/bridge-premarket-to-tjl.py --input .cache/tjl-live-input-today.json --intraday-timeframe 15
```

Notes:

- SQLite is the source of truth for candidate handoff.
- JSON artifacts remain useful as logs, reproducibility snapshots, and debug inputs.
- The collector confirms chart state across repeated polls, validates bar cadence, and requires repeated OHLCV reads to stay consistent before accepting data. It still depends on a healthy local TradingView Desktop + CDP session.
- If TradingView reports a new timeframe in chart state but `ohlcv` still returns stale bars from the prior resolution, the collector will fail intentionally instead of writing mislabeled payload data. In that case, rerun after the chart settles or restart the TradingView session.
- If some symbols fail collection but others succeed, the collector writes a partial payload and lists the failures in `failed_symbols`. If no symbols can be collected, the run still fails hard.

## TradingView MCP setup

This repo expects a local checkout of `tradesdontlie/tradingview-mcp` at:

```text
./tradingview-mcp
```

The project-level `opencode.json` currently points OpenCode at that checkout:

```json
{
  "mcp": {
    "tradingview": {
      "type": "local",
      "command": ["node", "src/server.js"],
      "cwd": "/Users/ed/Projects/bot-trader/tradingview-mcp",
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

The `tradingview-mcp/` directory is ignored by this repo because it is an external dependency checkout, not application code.

## Health check

With TradingView Desktop running and remote debugging enabled on port `9222`, ask OpenCode to run:

```text
Use the tradingview MCP server to run tv_health_check.
```

A healthy response should include:

```text
cdp_connected: true
api_available: true
```
