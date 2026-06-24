#!/usr/bin/env python3
"""Premarket gappers scanner.

Fetches Yahoo's day-gainers screener data, filters the largest movers, enriches
the top names with best-effort Benzinga quote-page catalyst snippets, writes the
requested JSON shape, and prints a one-line summary.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "bot-trader.db"
YAHOO_GAINERS_PAGE_URL = "https://finance.yahoo.com/markets/stocks/gainers/"
YAHOO_SCREENER_URLS = [
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    "?count=100&formatted=false&scrIds=day_gainers&lang=en-US&region=US&corsDomain=finance.yahoo.com",
    "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"
    "?count=100&formatted=false&scrIds=day_gainers&lang=en-US&region=US&corsDomain=finance.yahoo.com",
]
MAX_RESULTS = 10
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_ATTEMPTS = 3
CATALYST_WORKERS = 4

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
]

BASE_HEADERS = {
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass(frozen=True)
class Gapper:
    symbol: str
    price: float
    gap_pct: float
    premarket_volume: int


@dataclass(frozen=True)
class ScannerOptions:
    log: bool = False


def parse_args(argv: list[str] | None = None) -> ScannerOptions:
    parser = argparse.ArgumentParser(description="Scan and summarize premarket gappers.")
    parser.add_argument(
        "--log",
        "--verbose",
        dest="log",
        action="store_true",
        help="Print detailed scanner progress to stderr.",
    )
    args = parser.parse_args(argv)
    return ScannerOptions(log=args.log)


def log_message(options: ScannerOptions | None, message: str) -> None:
    if not options or not options.log:
        return

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=sys.stderr)


def today_iso_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def output_path_for_today() -> Path:
    return ROOT_DIR / f"premarket_gappers_{today_iso_date()}.json"


def load_scanner_config() -> dict[str, Any]:
    config_path = ROOT_DIR / "config" / "scanner.json"
    if not config_path.exists():
        return {}

    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def scanner_name() -> str:
    return str(load_scanner_config().get("scannerName", "premarket-gap-scan"))


def database_path() -> Path:
    configured = load_scanner_config().get("data", {}).get("databasePath")
    if not configured:
        return DEFAULT_DB_PATH

    path = Path(str(configured))
    return path if path.is_absolute() else ROOT_DIR / path


def watchlist_snapshot() -> list[str]:
    configured = load_scanner_config().get("data", {}).get("watchlistPath", "data/watchlist.json")
    path = Path(str(configured))
    watchlist_path = path if path.is_absolute() else ROOT_DIR / path
    if not watchlist_path.exists():
        return []

    try:
        payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    symbols = payload.get("symbols") if isinstance(payload, dict) else payload
    if not isinstance(symbols, list):
        return []

    return [str(symbol) for symbol in symbols if isinstance(symbol, str) and symbol.strip()]


def scanner_criteria() -> dict[str, float | int]:
    criteria = load_scanner_config().get("criteria", {})
    return {
        "min_gap_pct": float(criteria.get("minGapPct", 5)),
        "min_price": float(criteria.get("minPrice", 3)),
        "min_premarket_volume": int(criteria.get("minPremarketVolume", 50_000)),
        "max_results": int(criteria.get("maxResults", MAX_RESULTS)),
    }


def config_criteria_snapshot() -> dict[str, Any]:
    criteria = load_scanner_config().get("criteria", {})
    return dict(criteria) if isinstance(criteria, dict) else {}


def benzinga_quote_url(symbol: str) -> str:
    return f"https://www.benzinga.com/quote/{urllib.parse.quote(symbol)}"


def persist_scan_run(
    gappers: list[dict[str, Any]],
    *,
    status: str,
    error_message: str | None = None,
    db_path: Path | None = None,
    artifact_path: Path | None = None,
) -> int:
    target_db_path = db_path or database_path()
    target_db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(target_db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        cursor = connection.cursor()
        with connection:
            cursor.execute(
                """
                INSERT INTO scan_runs (scanner_name, watchlist, criteria, status, error_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scanner_name(),
                    json.dumps(watchlist_snapshot(), separators=(",", ":")),
                    json.dumps(config_criteria_snapshot(), separators=(",", ":")),
                    status,
                    error_message,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a scan_runs row id")
            scan_run_id = cursor.lastrowid

            for gapper in gappers:
                symbol = str(gapper["symbol"])
                metadata = {
                    "rank": gapper.get("rank"),
                    "catalyst": gapper.get("catalyst"),
                    "headlines": gapper.get("headlines", []),
                    "catalyst_method": "regex" if gapper.get("catalyst") or gapper.get("headlines") else "none",
                    "benzinga_url": benzinga_quote_url(symbol),
                }
                if artifact_path:
                    metadata["artifact_path"] = str(artifact_path)

                cursor.execute(
                    """
                    INSERT INTO scan_results (
                      scan_run_id, symbol, gap_pct, premarket_volume, price, timeframe, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_run_id,
                        symbol,
                        gapper.get("gap_pct"),
                        gapper.get("premarket_volume"),
                        gapper.get("price"),
                        None,
                        json.dumps(metadata, separators=(",", ":")),
                    ),
                )
        return scan_run_id
    finally:
        connection.close()


def as_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)

    if not isinstance(value, str):
        return None

    normalized = re.sub(r"[$,%+\s]", "", value).replace(",", "")
    try:
        return float(normalized)
    except ValueError:
        return None


def as_volume(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)

    if not isinstance(value, str):
        return None

    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMB])?$", value.strip().upper().replace(",", ""))
    if not match:
        return None

    raw_number, suffix = match.groups()
    multiplier = {"B": 1_000_000_000, "M": 1_000_000, "K": 1_000}.get(suffix, 1)
    return int(float(raw_number) * multiplier)


def raw_value(value: Any) -> Any:
    if isinstance(value, dict) and "raw" in value:
        return value["raw"]
    return value


def build_headers(headers: dict[str, str] | None = None, attempt: int = 0) -> dict[str, str]:
    user_agent = USER_AGENTS[attempt % len(USER_AGENTS)]
    return {**BASE_HEADERS, "User-Agent": user_agent, **(headers or {})}


def fetch_text(
    url: str,
    headers: dict[str, str] | None = None,
    options: ScannerOptions | None = None,
) -> str:
    errors: list[str] = []

    for attempt in range(REQUEST_ATTEMPTS):
        try:
            log_message(options, f"fetch attempt {attempt + 1}/{REQUEST_ATTEMPTS}: {url}")
            request = urllib.request.Request(url, headers=build_headers(headers, attempt))
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
                log_message(options, f"fetched {len(body)} chars: {url}")
                return body
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            errors.append(str(error))
            log_message(options, f"fetch failed on attempt {attempt + 1}/{REQUEST_ATTEMPTS}: {url} ({error})")
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep((2**attempt) + random.uniform(0.1, 0.7))

    raise RuntimeError("; ".join(errors))


def crawl4ai_fetch_text(url: str, options: ScannerOptions | None = None) -> str | None:
    """Best-effort optional browser-rendered fetch.

    Crawl4AI is intentionally optional. If it is not installed and initialized,
    the scanner still works with urllib retries and explicit errors.
    """
    try:
        import asyncio
        crawl4ai = importlib.import_module("crawl4ai")
    except ImportError:
        log_message(options, "Crawl4AI fallback unavailable: package is not installed")
        return None

    async def crawl() -> str | None:
        browser_config = crawl4ai.BrowserConfig(headless=True, verbose=False)
        run_config = crawl4ai.CrawlerRunConfig(cache_mode=crawl4ai.CacheMode.BYPASS)
        async with crawl4ai.AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)
            if not result.success:
                log_message(options, f"Crawl4AI fallback failed for {url}")
                return None
            log_message(options, f"Crawl4AI fallback succeeded for {url}")
            return result.html or getattr(result.markdown, "raw_markdown", None) or str(result.markdown or "")

    try:
        log_message(options, f"starting Crawl4AI fallback: {url}")
        return asyncio.run(crawl())
    except Exception as error:
        log_message(options, f"Crawl4AI fallback errored for {url}: {error}")
        return None


def strip_html(value: str) -> str:
    without_scripts = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.IGNORECASE)
    without_styles = re.sub(r"<style[\s\S]*?</style>", " ", without_scripts, flags=re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def parse_yahoo_quote(raw_quote: dict[str, Any]) -> Gapper | None:
    symbol = raw_quote.get("symbol")
    price = as_number(raw_value(raw_quote.get("regularMarketPrice")))
    gap_pct = as_number(raw_value(raw_quote.get("regularMarketChangePercent")))
    premarket_volume = as_volume(raw_value(raw_quote.get("regularMarketVolume")))

    if not symbol or price is None or gap_pct is None or premarket_volume is None:
        return None

    return Gapper(
        symbol=str(symbol),
        price=price,
        gap_pct=gap_pct,
        premarket_volume=premarket_volume,
    )


def fetch_yahoo_gainers(options: ScannerOptions | None = None) -> list[Gapper]:
    errors: list[str] = []
    criteria = scanner_criteria()
    log_message(
        options,
        "scanner criteria: "
        f"min_gap_pct={criteria['min_gap_pct']}, "
        f"min_price={criteria['min_price']}, "
        f"min_premarket_volume={criteria['min_premarket_volume']}, "
        f"max_results={criteria['max_results']}",
    )

    for url in YAHOO_SCREENER_URLS:
        try:
            log_message(options, f"loading Yahoo screener endpoint: {url}")
            body = fetch_text(
                url,
                headers={
                    "Accept": "application/json",
                    "Referer": YAHOO_GAINERS_PAGE_URL,
                },
                options=options,
            )
            payload = json.loads(body)
            quotes = payload.get("finance", {}).get("result", [{}])[0].get("quotes")

            if not isinstance(quotes, list):
                raise ValueError("Yahoo response did not include finance.result[0].quotes")

            parsed = [quote for quote in (parse_yahoo_quote(raw_quote) for raw_quote in quotes) if quote]
            filtered = sorted(
                (
                    quote
                    for quote in parsed
                    if quote.gap_pct > criteria["min_gap_pct"]
                    and quote.price > criteria["min_price"]
                    and quote.premarket_volume > criteria["min_premarket_volume"]
                ),
                key=lambda quote: quote.gap_pct,
                reverse=True,
            )[: int(criteria["max_results"])]
            log_message(
                options,
                f"Yahoo endpoint returned {len(quotes)} raw quotes, parsed {len(parsed)}, "
                f"kept {len(filtered)}: {', '.join(quote.symbol for quote in filtered) or 'none'}",
            )
            return filtered
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"{url}: {error}")
            log_message(options, f"Yahoo screener endpoint failed: {url} ({error})")

    log_message(options, "Yahoo JSON endpoints failed; trying rendered Yahoo page fallback")
    html_body = crawl4ai_fetch_text(YAHOO_GAINERS_PAGE_URL, options)
    if html_body:
        parsed = parse_yahoo_gainers_from_html(html_body)
        if parsed:
            filtered = sorted(
                (
                    quote
                    for quote in parsed
                    if quote.gap_pct > criteria["min_gap_pct"]
                    and quote.price > criteria["min_price"]
                    and quote.premarket_volume > criteria["min_premarket_volume"]
                ),
                key=lambda quote: quote.gap_pct,
                reverse=True,
            )[: int(criteria["max_results"])]
            log_message(
                options,
                f"Yahoo HTML fallback parsed {len(parsed)} quotes, kept {len(filtered)}: "
                f"{', '.join(quote.symbol for quote in filtered) or 'none'}",
            )
            return filtered

    raise RuntimeError(f"Yahoo gainers source failed ({'; '.join(errors)})")


def parse_yahoo_gainers_from_html(page_html: str) -> list[Gapper]:
    """Fallback parser for rendered Yahoo table text.

    This is intentionally conservative because the Yahoo page changes often.
    The JSON screener endpoint remains the primary source.
    """
    text = strip_html(page_html)
    pattern = re.compile(
        r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,6})\b\s+"
        r"(?P<price>\d+(?:\.\d+)?)\s+"
        r"(?:[+\-]?\d+(?:\.\d+)?\s+)?"
        r"\+?(?P<gap>\d+(?:\.\d+)?)%\s+"
        r"(?P<volume>\d+(?:\.\d+)?[KMB]?)\b"
    )
    gappers: list[Gapper] = []
    for match in pattern.finditer(text):
        price = as_number(match.group("price"))
        gap_pct = as_number(match.group("gap"))
        volume = as_volume(match.group("volume"))
        if price is None or gap_pct is None or volume is None:
            continue
        gappers.append(
            Gapper(
                symbol=match.group("symbol"),
                price=price,
                gap_pct=gap_pct,
                premarket_volume=volume,
            )
        )
    return gappers


def extract_headlines(page_html: str, symbol: str) -> list[str]:
    text = strip_html(page_html)
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if 20 < len(sentence.strip()) < 220
    ]
    symbol_pattern = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
    market_keywords = re.compile(
        r"stock|shares|trading|price|analyst|earnings|revenue|guidance|upgrade|downgrade|"
        r"FDA|merger|acquisition|offering|contract|partnership|approval|results",
        re.IGNORECASE,
    )

    candidates = [sentence for sentence in sentences if symbol_pattern.search(sentence) or market_keywords.search(sentence)]
    deduped = list(dict.fromkeys(candidates))
    return deduped[:2]


def extract_catalyst(headlines: list[str], symbol: str) -> str | None:
    if not headlines:
        return None

    first = re.sub(r"\s+", " ", headlines[0]).strip()
    if not first:
        return None

    return first if symbol in first else f"{symbol}: {first}"


def fetch_catalyst(symbol: str, options: ScannerOptions | None = None) -> dict[str, Any]:
    try:
        quote_url = benzinga_quote_url(symbol)
        log_message(options, f"loading Benzinga catalyst page for {symbol}: {quote_url}")
        page_html = fetch_text(
            quote_url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            options=options,
        )
        if "Just a moment" in page_html or "enable JavaScript" in page_html:
            log_message(options, f"Benzinga page for {symbol} appears browser-gated; trying Crawl4AI")
            page_html = crawl4ai_fetch_text(quote_url, options) or page_html
        headlines = extract_headlines(page_html, symbol)
        catalyst = extract_catalyst(headlines, symbol)
        log_message(
            options,
            f"catalyst lookup for {symbol}: {len(headlines)} headline(s), "
            f"catalyst={'found' if catalyst else 'not found'}",
        )
        return {
            "catalyst": catalyst,
            "headlines": headlines,
        }
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as error:
        log_message(options, f"catalyst lookup failed for {symbol}: {error}")
        return {"catalyst": None, "headlines": []}


def format_percent(value: float) -> str:
    formatted = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{formatted}%"


def format_summary(gappers: list[dict[str, Any]]) -> str:
    top = [
        f"{gapper['symbol']} ({format_percent(gapper['gap_pct'])}) — {gapper['catalyst'] or 'no catalyst found'}"
        for gapper in gappers[:3]
    ]
    return f"Premarket Gappers: {len(gappers)} names. Top: {', '.join(top)}"


def write_output(gappers: list[dict[str, Any]]) -> None:
    output = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "gappers": gappers,
    }
    output_path_for_today().write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    log_message(options, "starting premarket gappers scan")
    artifact_written = False

    try:
        filtered = fetch_yahoo_gainers(options)
        enriched_by_symbol: dict[str, dict[str, Any]] = {}
        log_message(options, f"fetching catalysts for {len(filtered)} symbol(s)")

        with ThreadPoolExecutor(max_workers=CATALYST_WORKERS) as executor:
            futures = {executor.submit(fetch_catalyst, gapper.symbol, options): gapper for gapper in filtered}
            for future in as_completed(futures):
                gapper = futures[future]
                try:
                    enriched_by_symbol[gapper.symbol] = future.result()
                except Exception:
                    log_message(options, f"unexpected catalyst worker error for {gapper.symbol}")
                    enriched_by_symbol[gapper.symbol] = {"catalyst": None, "headlines": []}

        enriched = []
        for index, gapper in enumerate(filtered, start=1):
            catalyst = enriched_by_symbol.get(gapper.symbol, {"catalyst": None, "headlines": []})
            enriched.append(
                {
                    "rank": index,
                    "symbol": gapper.symbol,
                    "price": gapper.price,
                    "gap_pct": gapper.gap_pct,
                    "premarket_volume": gapper.premarket_volume,
                    "catalyst": catalyst["catalyst"],
                    "headlines": catalyst["headlines"],
                }
            )

        write_output(enriched)
        artifact_written = True
        artifact_path = output_path_for_today()
        log_message(options, f"wrote scan output to {artifact_path}")
        status = "success" if enriched else "empty"
        scan_run_id = persist_scan_run(enriched, status=status, artifact_path=artifact_path)
        log_message(options, f"persisted scan_run_id={scan_run_id} to {database_path()}")
        print(format_summary(enriched))
        return 0
    except Exception as error:
        if not artifact_written:
            write_output([])
        artifact_path = output_path_for_today()
        try:
            scan_run_id = persist_scan_run([], status="error", error_message=str(error), artifact_path=artifact_path)
            log_message(options, f"persisted failed scan_run_id={scan_run_id} to {database_path()}")
        except Exception as db_error:
            log_message(options, f"failed to persist error scan: {db_error}")
        log_message(options, f"scan failed: {error}")
        print(f"Premarket gappers scan failed: {error}", file=sys.stderr)
        print("Premarket Gappers: 0 names. Top:")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
