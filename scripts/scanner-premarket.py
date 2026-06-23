#!/usr/bin/env python3
"""Premarket gappers scanner.

Fetches Yahoo's day-gainers screener data, filters the largest movers, enriches
the top names with best-effort Benzinga quote-page catalyst snippets, writes the
requested JSON shape, and prints a one-line summary.
"""

from __future__ import annotations

import html
import hashlib
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
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
RAW_CACHE_DIR = ROOT_DIR / ".cache" / "premarket-scanner"
RAW_CACHE_MAX_AGE = timedelta(hours=18)
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


def scanner_criteria() -> dict[str, float | int]:
    criteria = load_scanner_config().get("criteria", {})
    return {
        "min_gap_pct": float(criteria.get("minGapPct", 5)),
        "min_price": float(criteria.get("minPrice", 3)),
        "min_premarket_volume": int(criteria.get("minPremarketVolume", 50_000)),
        "max_results": int(criteria.get("maxResults", MAX_RESULTS)),
    }


def cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return RAW_CACHE_DIR / f"{digest}.txt"


def cache_is_fresh(path: Path, max_age: timedelta = RAW_CACHE_MAX_AGE) -> bool:
    if not path.exists():
        return False

    modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return datetime.now(timezone.utc) - modified_at <= max_age


def read_cached_text(url: str, max_age: timedelta = RAW_CACHE_MAX_AGE) -> str | None:
    path = cache_path_for_url(url)
    if not cache_is_fresh(path, max_age):
        return None

    return path.read_text(encoding="utf-8", errors="replace")


def write_cached_text(url: str, body: str) -> None:
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path_for_url(url).write_text(body, encoding="utf-8")


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


def fetch_text(url: str, headers: dict[str, str] | None = None, use_cache: bool = True) -> str:
    cached = read_cached_text(url) if use_cache else None
    errors: list[str] = []

    for attempt in range(REQUEST_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers=build_headers(headers, attempt))
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
                if use_cache:
                    write_cached_text(url, body)
                return body
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            errors.append(str(error))
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep((2**attempt) + random.uniform(0.1, 0.7))

    if cached is not None:
        print(f"Using cached response for {url} after fetch failures: {'; '.join(errors)}", file=sys.stderr)
        return cached

    raise RuntimeError("; ".join(errors))


def crawl4ai_fetch_text(url: str) -> str | None:
    """Best-effort optional browser-rendered fetch.

    Crawl4AI is intentionally optional. If it is not installed and initialized,
    the scanner still works with urllib + cache/retry fallbacks.
    """
    try:
        import asyncio
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    except ImportError:
        return None

    async def crawl() -> str | None:
        browser_config = BrowserConfig(headless=True, verbose=False)
        run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)
            if not result.success:
                return None
            return result.html or getattr(result.markdown, "raw_markdown", None) or str(result.markdown or "")

    try:
        return asyncio.run(crawl())
    except Exception:
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


def fetch_yahoo_gainers() -> list[Gapper]:
    errors: list[str] = []
    criteria = scanner_criteria()

    for url in YAHOO_SCREENER_URLS:
        try:
            body = fetch_text(
                url,
                headers={
                    "Accept": "application/json",
                    "Referer": YAHOO_GAINERS_PAGE_URL,
                },
            )
            payload = json.loads(body)
            quotes = payload.get("finance", {}).get("result", [{}])[0].get("quotes")

            if not isinstance(quotes, list):
                raise ValueError("Yahoo response did not include finance.result[0].quotes")

            parsed = [quote for quote in (parse_yahoo_quote(raw_quote) for raw_quote in quotes) if quote]
            return sorted(
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
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"{url}: {error}")

    html_body = crawl4ai_fetch_text(YAHOO_GAINERS_PAGE_URL)
    if html_body:
        parsed = parse_yahoo_gainers_from_html(html_body)
        if parsed:
            return sorted(
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


def fetch_catalyst(symbol: str) -> dict[str, Any]:
    try:
        quote_url = f"https://www.benzinga.com/quote/{urllib.parse.quote(symbol)}"
        page_html = fetch_text(
            quote_url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        if "Just a moment" in page_html or "enable JavaScript" in page_html:
            page_html = crawl4ai_fetch_text(quote_url) or page_html
        headlines = extract_headlines(page_html, symbol)
        return {
            "catalyst": extract_catalyst(headlines, symbol),
            "headlines": headlines,
        }
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError):
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


def main() -> int:
    try:
        filtered = fetch_yahoo_gainers()
        enriched_by_symbol: dict[str, dict[str, Any]] = {}

        with ThreadPoolExecutor(max_workers=CATALYST_WORKERS) as executor:
            futures = {executor.submit(fetch_catalyst, gapper.symbol): gapper for gapper in filtered}
            for future in as_completed(futures):
                gapper = futures[future]
                try:
                    enriched_by_symbol[gapper.symbol] = future.result()
                except Exception:
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
        print(format_summary(enriched))
        return 0
    except Exception as error:
        write_output([])
        print(f"Premarket gappers scan failed: {error}", file=sys.stderr)
        print("Premarket Gappers: 0 names. Top:")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
