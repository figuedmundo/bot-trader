#!/usr/bin/env python3
"""Premarket gappers scanner.

Fetches Yahoo's day-gainers screener data, filters the largest movers, enriches
the top names with TradingView news headlines and Groq catalyst summaries, writes
the requested JSON shape, and prints a one-line summary.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import os
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
CATALYST_WORKERS = 2
GROQ_MAX_RETRIES = 4
GROQ_RETRY_BASE_SECONDS = 12.0
TRADINGVIEW_NEWS_HOST = "https://news-headlines.tradingview.com"
TRADINGVIEW_SCANNER_HOST = "https://scanner.tradingview.com"
TRADINGVIEW_WEB_HOST = "https://www.tradingview.com"

# Curated over ~300 columns — see skills/tradingview/references/SCANNER_COLUMNS.md
DEFAULT_TV_ENRICHMENT_COLUMNS = [
    "change_from_open",
    "Recommend.All",
    "market_cap_basic",
    "sector",
    "industry",
    "RSI",
    "Perf.W",
    "price_52_week_high",
]

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
    exchange: str = ""
    tv_symbol: str = ""


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


def read_env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value

    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() != name:
            continue
        candidate = raw_value.strip().strip('"').strip("'")
        return candidate or None

    return None


YAHOO_TO_TV_EXCHANGE = {
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NYQ": "NYSE",
    "ASE": "AMEX",
    "PCX": "ARCA",
    "OEM": "OTC",
    "PNK": "OTC",
}


def yahoo_exchange_to_tv(exchange_code: str) -> str:
    return YAHOO_TO_TV_EXCHANGE.get(exchange_code, exchange_code)


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
                    "news_items": gapper.get("news_items", []),
                    "catalyst_method": gapper.get("catalyst_method") or ("tradingview-groq" if gapper.get("catalyst") else "none"),
                    "catalyst_error": gapper.get("catalyst_error"),
                    "news_url": gapper.get("news_url"),
                    "exchange": gapper.get("exchange"),
                    "tv_symbol": gapper.get("tv_symbol"),
                    "tv_enrichment": gapper.get("tv_enrichment"),
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


def strip_html(value: str) -> str:
    without_scripts = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.IGNORECASE)
    without_styles = re.sub(r"<style[\s\S]*?</style>", " ", without_scripts, flags=re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def story_url_for_path(story_path: str) -> str:
    normalized = story_path if story_path.startswith("/") else f"/{story_path}"
    return f"{TRADINGVIEW_WEB_HOST}{normalized}"


def published_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def fetch_story_content(story_path: str, options: ScannerOptions | None = None) -> dict[str, Any]:
    if not story_path:
        return {}
    url = story_url_for_path(story_path)
    html_body = fetch_text(url, options=options)
    title_match = re.search(r"<title>([^<]+)</title>", html_body, flags=re.IGNORECASE)
    body_match = re.search(r"<article[^>]*>(.+?)</article>", html_body, flags=re.IGNORECASE | re.DOTALL)
    if not body_match:
        body_match = re.search(r'data-name="news-content"[^>]*>(.+?)</div>\s*</div>', html_body, flags=re.IGNORECASE | re.DOTALL)
    title = normalize_text(title_match.group(1)) if title_match else None
    body = strip_html(body_match.group(1)) if body_match else None
    return {
        "url": url,
        "title": title or None,
        "body": body or None,
    }


def parse_groq_status_error(error: Any) -> str:
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    payload = None
    if response is not None:
        try:
            payload = response.json()
        except Exception:
            try:
                payload = json.loads(getattr(response, "text", "") or "")
            except Exception:
                payload = None
    info = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(info, dict):
        code = normalize_text(info.get("code", ""))
        message = normalize_text(info.get("message", ""))
        error_code = normalize_text(info.get("error_code", ""))
        title = normalize_text(info.get("title", ""))
        if str(status) == "403" and error_code == "1010":
            return "Groq HTTP 403: Cloudflare blocked this request signature (Error 1010)."
        if str(status) == "403" and title:
            return f"Groq HTTP 403: {title}{f' — {message}' if message else ''}"
        detail = " — ".join(part for part in [code, message] if part)
        return f"Groq HTTP {status}: {detail}" if detail else f"Groq HTTP {status}"
    if status:
        return f"Groq HTTP {status}: {error}"
    return f"Groq request failed: {error}"


def parse_yahoo_quote(raw_quote: dict[str, Any]) -> Gapper | None:
    symbol = raw_quote.get("symbol")
    price = as_number(raw_value(raw_quote.get("regularMarketPrice")))
    gap_pct = as_number(raw_value(raw_quote.get("regularMarketChangePercent")))
    premarket_volume = as_volume(raw_value(raw_quote.get("regularMarketVolume")))

    if not symbol or price is None or gap_pct is None or premarket_volume is None:
        return None

    exchange = str(raw_quote.get("exchange", ""))
    tv_prefix = yahoo_exchange_to_tv(exchange)
    tv_symbol = f"{tv_prefix}:{symbol}" if tv_prefix else str(symbol)

    return Gapper(
        symbol=str(symbol),
        price=price,
        gap_pct=gap_pct,
        premarket_volume=premarket_volume,
        exchange=exchange,
        tv_symbol=tv_symbol,
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


def extract_escaped_json_array(page_html: str, key: str) -> list[dict[str, Any]]:
    needle = f'\\"{key}\\":['
    start_search = 0
    best: list[dict[str, Any]] = []

    while True:
        found = page_html.find(needle, start_search)
        if found == -1:
            break
        start = found + len(f'\\"{key}\\":')
        depth = 0
        in_string = False
        escaped = False
        end = None

        for index in range(start, len(page_html)):
            char = page_html[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break

        if end is not None:
            raw = page_html[start:end]
            try:
                decoded = raw.encode("utf-8").decode("unicode_escape").replace('\\"', '"').replace('\\/', '/')
                parsed = json.loads(decoded)
                if isinstance(parsed, list):
                    candidates = [item for item in parsed if isinstance(item, dict) and item.get("title")]
                    if len(candidates) > len(best):
                        best = candidates
            except Exception:
                pass
        start_search = found + len(needle)

    return best


def fetch_symbol_news(symbol: str, tv_symbol: str | None = None, options: ScannerOptions | None = None) -> dict[str, Any]:
    query_symbol = tv_symbol or symbol
    url = (
        f"{TRADINGVIEW_NEWS_HOST}/v2/headlines?"
        f"symbol={urllib.parse.quote(query_symbol)}&client=web&lang=en"
    )
    log_message(options, f"fetching TradingView news for {symbol} ({query_symbol}): {url}")
    try:
        body = fetch_text(url, headers={"Accept": "application/json"}, options=options)
        data = json.loads(body)
        news_items = data.get("items", [])
        if not news_items:
            raise RuntimeError("TradingView news returned no items")

        sorted_items = sorted(news_items, key=lambda item: item.get("urgency", 0), reverse=True)
        headlines = []
        details = []
        news_items_payload = []
        for item in sorted_items[:3]:
            title = normalize_text(item.get("title", ""))
            if not title:
                continue
            source = normalize_text(item.get("source", ""))
            provider = normalize_text(item.get("provider", ""))
            story_path = normalize_text(item.get("storyPath", ""))
            published = published_iso(item.get("published"))
            link = normalize_text(item.get("link", ""))
            teaser = f"Source: {source}" if source else ""
            story = {}
            if story_path:
                try:
                    story = fetch_story_content(story_path, options)
                except Exception as error:
                    log_message(options, f"story fetch failed for {symbol} {story_path}: {error}")
            headlines.append(title)
            details.append({
                "title": title,
                "teaser": teaser,
                "body": normalize_text(story.get("body", "")) if story.get("body") else "",
            })
            news_items_payload.append({
                "title": title,
                "body": story.get("body"),
                "url": link or story.get("url") or None,
                "story_url": story.get("url") or None,
                "source": source or None,
                "provider": provider or None,
                "published": published,
                "story_path": story_path or None,
            })
        if not headlines:
            raise RuntimeError("TradingView news had items but no usable titles")
        source_url = f"https://www.tradingview.com/symbols/{urllib.parse.quote(query_symbol.replace(':', '-'))}/news/"
        return {"headlines": headlines, "details": details, "news_items": news_items_payload, "source_url": source_url}
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"TradingView news fetch failed: {error}") from error


def summarize_catalyst_with_groq(symbol: str, details: list[dict[str, str]], options: ScannerOptions | None = None) -> str:
    api_key = read_env_value("GROQ_API_KEY")
    model = read_env_value("GROQ_MODEL") or "openai/gpt-oss-120b"
    if not api_key:
        raise RuntimeError("Groq API key missing (GROQ_API_KEY)")
    if not details:
        raise RuntimeError("No news headlines available for Groq summarization")

    try:
        groq_module = importlib.import_module("groq")
        groq_client = getattr(groq_module, "Groq")
        rate_limit_error = getattr(groq_module, "RateLimitError")
        connection_error = getattr(groq_module, "APIConnectionError")
        status_error = getattr(groq_module, "APIStatusError")
    except Exception as error:
        raise RuntimeError("Groq Python SDK is not installed. Run `python3 -m pip install groq`.") from error

    prompt_lines = [
        f"What recent news or catalyst is driving {symbol} stock today?",
        "",
        "Use only the news items below.",
        "Return JSON with exactly one key: catalyst.",
        "The catalyst must be one sentence only, no commentary, no bullets.",
        "",
        "News items:",
    ]
    max_body_chars = 100_000
    for index, item in enumerate(details, start=1):
        prompt_lines.append(f"{index}. Headline: {item['title']}")
        if item.get("teaser"):
            prompt_lines.append(f"   Teaser: {item['teaser']}")
        body = item.get("body", "")
        if body and max_body_chars > 0:
            if len(body) <= max_body_chars:
                prompt_lines.append(f"   Body: {body}")
                max_body_chars -= len(body)
            else:
                prompt_lines.append(f"   Body: {body[:max_body_chars]}[...trimmed]")
                max_body_chars = 0

    client = groq_client(api_key=api_key, max_retries=0)
    completion = None
    for attempt in range(GROQ_MAX_RETRIES):
        try:
            completion = client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": "You summarize stock news into one precise catalyst sentence. Respond only with valid JSON.",
                    },
                    {
                        "role": "user",
                        "content": "\n".join(prompt_lines),
                    },
                ],
            )
            break
        except rate_limit_error as error:
            if attempt >= GROQ_MAX_RETRIES - 1:
                raise RuntimeError(f"Groq rate limit: exhausted {GROQ_MAX_RETRIES} retries — {error}") from error
            wait = GROQ_RETRY_BASE_SECONDS * (2 ** attempt) + random.uniform(0.5, 2.0)
            log_message(options, f"Groq 429 on {symbol}, retry {attempt + 2}/{GROQ_MAX_RETRIES} after {wait:.0f}s")
            time.sleep(wait)
        except connection_error as error:
            raise RuntimeError(f"Groq connection failed: {error.__cause__ or error}") from error
        except status_error as error:
            raise RuntimeError(parse_groq_status_error(error)) from error
        except Exception as error:
            raise RuntimeError(f"Groq request failed: {error}") from error

    if completion is None:
        raise RuntimeError("Groq returned no completion after retries")

    content = completion.choices[0].message.content or ""
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Groq returned empty content")
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Groq returned invalid JSON: {error}") from error
    catalyst = parsed.get("catalyst") if isinstance(parsed, dict) else None
    if not isinstance(catalyst, str) or not catalyst.strip():
        raise RuntimeError("Groq returned no catalyst text")
    return re.sub(r"\s+", " ", catalyst).strip()


def fetch_catalyst(symbol: str, tv_symbol: str | None = None, options: ScannerOptions | None = None) -> dict[str, Any]:
    news = fetch_symbol_news(symbol, tv_symbol, options)
    headlines = news["headlines"]
    try:
        catalyst = summarize_catalyst_with_groq(symbol, news["details"], options)
        log_message(options, f"tradingview+groq for {symbol}: {len(headlines)} headline(s), catalyst=found")
        return {
            "catalyst": catalyst,
            "headlines": headlines,
            "news_items": news.get("news_items", []),
            "catalyst_method": "tradingview-groq",
            "catalyst_error": None,
            "news_url": news["source_url"],
        }
    except RuntimeError as error:
        log_message(options, f"groq catalyst lookup failed for {symbol}: {error}")
        return {
            "catalyst": None,
            "headlines": headlines,
            "news_items": news.get("news_items", []),
            "catalyst_method": "error",
            "catalyst_error": str(error),
            "news_url": news["source_url"],
        }


def enrichment_config() -> dict[str, Any]:
    raw = load_scanner_config().get("enrichment", {}).get("tradingView", {})
    return raw if isinstance(raw, dict) else {}


def enrichment_columns() -> list[str]:
    configured = enrichment_config().get("columns")
    if isinstance(configured, list) and configured:
        return [str(col) for col in configured if isinstance(col, str) and col.strip()]
    return list(DEFAULT_TV_ENRICHMENT_COLUMNS)


def enrichment_enabled() -> bool:
    return bool(enrichment_config().get("enabled", True))


def enrichment_market() -> str:
    return str(enrichment_config().get("market", "global"))


def post_scanner_json(
    symbols: list[str],
    columns: list[str],
    market: str,
    options: ScannerOptions | None = None,
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "symbols": {"tickers": symbols},
            "columns": columns,
            "range": [0, len(symbols)],
            "filter": [],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{TRADINGVIEW_SCANNER_HOST}/{market}/scan",
        data=payload,
        method="POST",
        headers={
            **build_headers(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def enrich_with_tradingview(
    gappers: list[dict[str, Any]],
    options: ScannerOptions | None = None,
) -> dict[str, dict[str, Any]]:
    if not enrichment_enabled():
        log_message(options, "tradingview enrichment disabled by config")
        return {}

    symbols = [str(g["tv_symbol"]) for g in gappers if g.get("tv_symbol")]
    if not symbols:
        log_message(options, "tradingview enrichment skipped: no tv_symbol candidates")
        return {}

    columns = enrichment_columns()
    market = enrichment_market()
    log_message(
        options,
        f"tradingview enrichment: {len(symbols)} symbol(s), market={market}, cols={len(columns)}",
    )

    try:
        raw = post_scanner_json(symbols, columns, market, options)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
        log_message(options, f"tradingview enrichment POST failed: {error}")
        return {}

    rows = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        log_message(options, f"tradingview enrichment: unexpected payload shape (keys={list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__})")
        return {}

    enriched: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = row.get("s") if isinstance(row, dict) else None
        values = row.get("d") if isinstance(row, dict) else None
        if not symbol or not isinstance(values, list):
            continue
        fields = {col: val for col, val in zip(columns, values)}
        enriched[str(symbol)] = fields
    log_message(options, f"tradingview enrichment: resolved {len(enriched)}/{len(symbols)} symbol(s)")
    return enriched


def format_percent(value: float) -> str:
    formatted = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{formatted}%"


def format_summary(gappers: list[dict[str, Any]]) -> str:
    top = [
        f"{gapper['symbol']} ({format_percent(gapper['gap_pct'])}) — {gapper['catalyst'] or ('ERROR: ' + gapper['catalyst_error'] if gapper.get('catalyst_error') else 'no catalyst found')}"
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
            futures = {executor.submit(fetch_catalyst, gapper.symbol, gapper.tv_symbol, options): gapper for gapper in filtered}
            for future in as_completed(futures):
                gapper = futures[future]
                try:
                    enriched_by_symbol[gapper.symbol] = future.result()
                except Exception as error:
                    log_message(options, f"unexpected catalyst worker error for {gapper.symbol}: {error}")
                    enriched_by_symbol[gapper.symbol] = {
                        "catalyst": None,
                        "headlines": [],
                        "catalyst_method": "error",
                        "catalyst_error": str(error),
                        "news_url": None,
                    }

        enriched = []
        tv_fields_by_symbol = enrich_with_tradingview(
            [
                {"tv_symbol": gapper.tv_symbol, "symbol": gapper.symbol}
                for gapper in filtered
            ],
            options,
        )
        for index, gapper in enumerate(filtered, start=1):
            catalyst = enriched_by_symbol.get(gapper.symbol, {"catalyst": None, "headlines": []})
            enriched.append(
                {
                    "rank": index,
                    "symbol": gapper.symbol,
                    "price": gapper.price,
                    "gap_pct": gapper.gap_pct,
                    "premarket_volume": gapper.premarket_volume,
                    "exchange": gapper.exchange,
                    "tv_symbol": gapper.tv_symbol,
                    "catalyst": catalyst["catalyst"],
                    "headlines": catalyst["headlines"],
                    "news_items": catalyst.get("news_items", []),
                    "catalyst_method": catalyst.get("catalyst_method"),
                    "catalyst_error": catalyst.get("catalyst_error"),
                    "news_url": catalyst.get("news_url"),
                    "tv_enrichment": tv_fields_by_symbol.get(gapper.tv_symbol),
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
