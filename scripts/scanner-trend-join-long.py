#!/usr/bin/env python3
"""Trend Join Long strategy scanner.

Consumes TradingView MCP data collected by OpenCode, evaluates the day-trading
entry criteria, writes a timestamped JSON artifact, and persists scan results to
SQLite. TradingView chart automation intentionally stays outside this script so
the calculation and persistence layer can be tested without a live chart tab.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "bot-trader.db"
NEW_YORK = ZoneInfo("America/New_York")
SCANNER_NAME = "trend-join-long-scan"
TEST_UNIVERSE = ["AMD", "NVDA", "MU"]
MARKET_OPEN = time(9, 30)
PREMARKET_START = time(4, 0)
TIME_GATE_START = time(10, 0)
TIME_GATE_END = time(15, 30)


@dataclass(frozen=True)
class ScannerOptions:
    input_path: Path | None = None
    log: bool = False
    now: datetime | None = None
    allow_outside_hours: bool = False


def parse_args(argv: list[str] | None = None) -> ScannerOptions:
    parser = argparse.ArgumentParser(
        description="Evaluate Trend Join Long candidates from collected TradingView MCP data."
    )
    parser.add_argument(
        "--input",
        dest="input_path",
        type=Path,
        help="Path to normalized TradingView MCP collection JSON.",
    )
    parser.add_argument(
        "--log",
        "--verbose",
        dest="log",
        action="store_true",
        help="Print detailed scanner progress to stderr.",
    )
    parser.add_argument(
        "--now",
        dest="now",
        help="Override current time for tests, as an ISO datetime. Naive values are interpreted as ET.",
    )
    parser.add_argument(
        "--allow-outside-hours",
        dest="allow_outside_hours",
        action="store_true",
        help="Development-only override that evaluates input data outside the 10:00-15:30 ET time gate.",
    )
    args = parser.parse_args(argv)
    return ScannerOptions(
        input_path=args.input_path,
        log=args.log,
        now=parse_datetime(args.now) if args.now else None,
        allow_outside_hours=args.allow_outside_hours,
    )


def log_message(options: ScannerOptions | None, message: str) -> None:
    if not options or not options.log:
        return

    timestamp = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=sys.stderr)


def now_et() -> datetime:
    return datetime.now(NEW_YORK)


def normalize_now(value: datetime | None = None) -> datetime:
    current = value or now_et()
    if current.tzinfo is None:
        return current.replace(tzinfo=NEW_YORK)
    return current.astimezone(NEW_YORK)


def output_path_for_now(current: datetime | None = None) -> Path:
    timestamp = normalize_now(current).strftime("%Y-%m-%d_%H%MET")
    return ROOT_DIR / f"tjl_watchlist_{timestamp}.json"


def database_path() -> Path:
    config_path = ROOT_DIR / "config" / "scanner.json"
    if not config_path.exists():
        return DEFAULT_DB_PATH

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_DB_PATH

    configured = config.get("data", {}).get("databasePath") if isinstance(config, dict) else None
    if not configured:
        return DEFAULT_DB_PATH

    path = Path(str(configured))
    return path if path.is_absolute() else ROOT_DIR / path


def watchlist_snapshot() -> list[str]:
    return list(TEST_UNIVERSE)


def criteria_snapshot() -> dict[str, Any]:
    return {
        "dailyBreakout": "curr_px > prev_completed_daily_high and prev_daily_close > sma200",
        "intradayBreakout": "curr_px > premarket_high and curr_px > today_high_of_day_excluding_current_bar",
        "timeGate": "10:00-15:30 America/New_York",
        "dailyBarPolicy": "drop today's daily bar when present; use previous completed daily candle",
        "outsideHoursOverride": "--allow-outside-hours is development-only and must not be used for live scans",
        "symbols": TEST_UNIVERSE,
    }


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC"))
    if not isinstance(value, str):
        raise ValueError(f"unsupported datetime value: {value!r}")

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=NEW_YORK)
    return parsed


def bar_time(bar: dict[str, Any]) -> datetime:
    for key in ("time", "timestamp", "datetime", "date"):
        if key in bar:
            return parse_datetime(bar[key])
    raise ValueError(f"bar missing time field: {bar!r}")


def as_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError as error:
            raise ValueError(f"{field_name} is not numeric: {value!r}") from error
    raise ValueError(f"{field_name} is not numeric: {value!r}")


def bar_value(bar: dict[str, Any], key: str) -> float:
    if key not in bar:
        raise ValueError(f"bar missing {key}: {bar!r}")
    return as_number(bar[key], field_name=key)


def quote_price(quote: Any) -> float:
    if isinstance(quote, (int, float, str)):
        return as_number(quote, field_name="quote")
    if not isinstance(quote, dict):
        raise ValueError(f"unsupported quote payload: {quote!r}")

    for key in ("price", "last", "last_price", "lastPrice", "close", "value"):
        if key in quote:
            return as_number(quote[key], field_name=key)
    raise ValueError(f"quote payload missing price field: {quote!r}")


def completed_daily_bars(daily_bars: list[dict[str, Any]], current: datetime) -> list[dict[str, Any]]:
    current_date = normalize_now(current).date()
    completed = []
    for bar in daily_bars:
        if bar_time(bar).astimezone(NEW_YORK).date() < current_date:
            completed.append(bar)
    return completed


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("cannot calculate mean of an empty list")
    return sum(values) / len(values)


def daily_levels(daily_bars: list[dict[str, Any]], current: datetime) -> dict[str, float]:
    completed = completed_daily_bars(daily_bars, current)
    if len(completed) < 200:
        raise ValueError(f"expected at least 200 completed daily bars, found {len(completed)}")

    previous = completed[-1]
    closes = [bar_value(bar, "close") for bar in completed[-200:]]
    return {
        "prev_daily_high": bar_value(previous, "high"),
        "prev_daily_close": bar_value(previous, "close"),
        "sma200": mean(closes),
    }


def intraday_levels(minute_bars: list[dict[str, Any]], current: datetime) -> dict[str, float]:
    current_et = normalize_now(current)
    today = current_et.date()
    premarket_highs: list[float] = []
    regular_highs: list[float] = []

    for bar in minute_bars:
        observed_at = bar_time(bar).astimezone(NEW_YORK)
        if observed_at.date() != today:
            continue
        observed_time = observed_at.time()
        if PREMARKET_START <= observed_time < MARKET_OPEN:
            premarket_highs.append(bar_value(bar, "high"))
        elif MARKET_OPEN <= observed_time < current_et.time():
            regular_highs.append(bar_value(bar, "high"))

    if not premarket_highs:
        raise ValueError("missing premarket 1-minute bars for 04:00-09:30 ET")
    if not regular_highs:
        raise ValueError("missing regular-session 1-minute bars before now")

    return {
        "pmh": max(premarket_highs),
        "today_hod": max(regular_highs),
    }


def result_reason(result: str, metrics: dict[str, float]) -> str:
    if result == "PASS":
        return "daily and intraday breakout confirmed"
    if result == "fail_daily":
        return (
            f"curr_px {metrics['curr_price']:.2f} must exceed prev_daily_high "
            f"{metrics['prev_daily_high']:.2f}, and prev_daily_close {metrics['prev_daily_close']:.2f} "
            f"must exceed sma200 {metrics['sma200']:.2f}"
        )
    return (
        f"curr_px {metrics['curr_price']:.2f} must exceed pmh {metrics['pmh']:.2f} "
        f"and today_hod {metrics['today_hod']:.2f}"
    )


def evaluate_symbol(payload: dict[str, Any], current: datetime) -> dict[str, Any]:
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("symbol payload missing symbol")

    daily_bars = payload.get("daily_bars") or payload.get("daily") or payload.get("daily_ohlcv")
    minute_bars = payload.get("minute_bars") or payload.get("intraday") or payload.get("minute_ohlcv")
    if not isinstance(daily_bars, list):
        raise ValueError(f"{symbol}: daily_bars must be a list")
    if not isinstance(minute_bars, list):
        raise ValueError(f"{symbol}: minute_bars must be a list")

    curr_price = quote_price(payload.get("quote", payload.get("curr_price")))
    metrics = {
        "curr_price": curr_price,
        **daily_levels(daily_bars, current),
        **intraday_levels(minute_bars, current),
    }
    daily_breakout = metrics["curr_price"] > metrics["prev_daily_high"] and metrics["prev_daily_close"] > metrics["sma200"]
    intraday_breakout = metrics["curr_price"] > metrics["pmh"] and metrics["curr_price"] > metrics["today_hod"]

    if daily_breakout and intraday_breakout:
        result = "PASS"
    elif not daily_breakout:
        result = "fail_daily"
    else:
        result = "fail_intraday"

    return {
        "symbol": symbol,
        "result": result,
        "reason": result_reason(result, metrics),
        "daily_breakout": daily_breakout,
        "intraday_breakout": intraday_breakout,
        **metrics,
    }


def normalize_symbol_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    symbols = payload.get("symbols") or payload.get("tickers") or payload.get("results")
    if isinstance(symbols, list):
        return [item for item in symbols if isinstance(item, dict)]

    candidates = []
    for symbol in TEST_UNIVERSE:
        value = payload.get(symbol)
        if isinstance(value, dict):
            candidates.append({"symbol": symbol, **value})
    return candidates


def load_collection(input_path: Path) -> dict[str, Any]:
    return json.loads(input_path.read_text(encoding="utf-8"))


def scan_collection(payload: dict[str, Any], current: datetime) -> list[dict[str, Any]]:
    by_symbol = {str(item.get("symbol", "")).upper(): item for item in normalize_symbol_payloads(payload)}
    results = []
    for symbol in TEST_UNIVERSE:
        if symbol not in by_symbol:
            raise ValueError(f"input missing collected data for {symbol}")
        results.append(evaluate_symbol(by_symbol[symbol], current))
    return results


def output_payload(
    results: list[dict[str, Any]],
    current: datetime,
    *,
    time_gate_overridden: bool = False,
) -> dict[str, Any]:
    hits = [
        {
            "symbol": result["symbol"],
            "curr_price": result["curr_price"],
            "prev_daily_high": result["prev_daily_high"],
            "sma200": result["sma200"],
            "pmh": result["pmh"],
            "today_hod": result["today_hod"],
        }
        for result in results
        if result["result"] == "PASS"
    ]
    return {
        "scanned_at": normalize_now(current).isoformat(),
        "time_gate_overridden": time_gate_overridden,
        "candidates_checked": len(TEST_UNIVERSE),
        "hits": hits,
        "all_results": [
            {"symbol": result["symbol"], "result": result["result"], "reason": result["reason"]}
            for result in results
        ],
    }


def error_payload(message: str, current: datetime) -> dict[str, Any]:
    return {
        "scanned_at": normalize_now(current).isoformat(),
        "status": "error",
        "error": message,
        "candidates_checked": len(TEST_UNIVERSE),
        "hits": [],
        "all_results": [],
    }


def write_json(payload: dict[str, Any], output_path: Path) -> None:
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def is_time_gate_open(current: datetime) -> bool:
    current_time = normalize_now(current).time()
    return TIME_GATE_START <= current_time <= TIME_GATE_END


def persist_scan_run(
    results: list[dict[str, Any]],
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
                    SCANNER_NAME,
                    json.dumps(watchlist_snapshot(), separators=(",", ":")),
                    json.dumps(criteria_snapshot(), separators=(",", ":")),
                    status,
                    error_message,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a scan_runs row id")
            scan_run_id = cursor.lastrowid

            for result in results:
                metadata = {
                    "result": result["result"],
                    "reason": result["reason"],
                    "daily_breakout": result["daily_breakout"],
                    "intraday_breakout": result["intraday_breakout"],
                    "prev_daily_high": result["prev_daily_high"],
                    "prev_daily_close": result["prev_daily_close"],
                    "sma200": result["sma200"],
                    "pmh": result["pmh"],
                    "today_hod": result["today_hod"],
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
                        result["symbol"],
                        None,
                        None,
                        result["curr_price"],
                        "D,1",
                        json.dumps(metadata, separators=(",", ":")),
                    ),
                )
        return scan_run_id
    finally:
        connection.close()


def print_result_lines(results: list[dict[str, Any]]) -> None:
    for result in results:
        print(f"{result['symbol']}: {result['result']} — {result['reason']}")


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    current = normalize_now(options.now)
    artifact_path = output_path_for_now(current)
    log_message(options, "starting Trend Join Long scan")

    try:
        time_gate_open = is_time_gate_open(current)
        if not time_gate_open and not options.allow_outside_hours:
            message = "Trend Join Long scanner only runs between 10:00 and 15:30 America/New_York"
            write_json(error_payload(message, current), artifact_path)
            persist_scan_run([], status="error", error_message=message, artifact_path=artifact_path)
            print(f"Trend Join Long scan skipped: {message}")
            return 0
        if not time_gate_open:
            log_message(options, "outside-hours development override enabled")

        if options.input_path is None:
            raise ValueError("--input is required; provide normalized TradingView MCP collection JSON")

        payload = load_collection(options.input_path)
        results = scan_collection(payload, current)
        write_json(output_payload(results, current, time_gate_overridden=not time_gate_open), artifact_path)
        status = "success" if any(result["result"] == "PASS" for result in results) else "empty"
        scan_run_id = persist_scan_run(results, status=status, artifact_path=artifact_path)
        log_message(options, f"persisted scan_run_id={scan_run_id} to {database_path()}")
        print_result_lines(results)
        return 0
    except Exception as error:
        write_json(error_payload(str(error), current), artifact_path)
        try:
            persist_scan_run([], status="error", error_message=str(error), artifact_path=artifact_path)
        except Exception as db_error:
            log_message(options, f"failed to persist error scan: {db_error}")
        print(f"Trend Join Long scan failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
