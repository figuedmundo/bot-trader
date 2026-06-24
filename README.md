# Bot Trader

Local, AI-assisted trading research tooling built around OpenCode and the TradingView MCP bridge. The project is intentionally research-only: it scans, records, visualizes, and alerts, but it does not execute live trades or store broker credentials.

## Current status

Phase 0 is being finalized. TradingView MCP is configured through `opencode.json`, and `tv_health_check` has been verified with `cdp_connected: true` and `api_available: true`.

The next milestone is Phase 1: create the SQLite foundation and the first premarket gap scanner.

## Premarket gappers scanner

The premarket scanner is a Python script that writes a dated JSON file at the repo root:

```bash
python3 scripts/scanner-premarket.py
```

For troubleshooting source data, run it with detailed progress logs on stderr:

```bash
python3 scripts/scanner-premarket.py --log
```

Output files match `premarket_gappers_YYYY-MM-DD.json` and are ignored by git because they are runtime scan artifacts.

The scanner is stdlib-only by default. For more reliable JS-rendered fallback scraping, install optional Crawl4AI support:

```bash
python3 -m pip install crawl4ai
crawl4ai-setup
crawl4ai-doctor
```

Crawl4AI is used only as a fallback when normal HTTP fetches fail or return browser-gated pages; it does not guarantee that public data sources will always be available.

## Planned MVP

- Read a static watchlist from `data/watchlist.json`.
- Apply configurable gap-scan rules from `config/scanner.json`.
- Persist scan runs, scan results, alerts, and logs in `data/bot-trader.db`.
- Send formatted Telegram alerts for Scanner A runs.
- Keep all runtime data local and reproducible.

See [`docs/plan.md`](docs/plan.md) for the full roadmap and architecture.

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

## Safety boundaries

- No live order execution.
- No broker API keys.
- No cloud dashboard in the MVP.
- Secrets belong in `.env`, which is gitignored.
- SQLite databases and logs are local runtime artifacts and are gitignored.
