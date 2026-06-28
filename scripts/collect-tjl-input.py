#!/usr/bin/env python3
"""Collect normalized TradingView payloads for latest premarket DB candidates."""

from __future__ import annotations

import argparse
from contextlib import closing
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.premarket_candidates import database_path, latest_premarket_run, premarket_candidates


TJL_SCANNER_PATH = ROOT_DIR / "scripts" / "scanner-trend-join-long.py"
TRADINGVIEW_CLI_PATH = ROOT_DIR / "tradingview-mcp" / "src" / "cli" / "index.js"
TRADINGVIEW_CLI_CWD = ROOT_DIR / "tradingview-mcp"
DEFAULT_OUTPUT_PATH = ROOT_DIR / ".cache" / "tjl-live-input-today.json"
NEW_YORK = ZoneInfo("America/New_York")
COLLECTOR_SOURCE = "tradingview-mcp-cli-live"
DEFAULT_DAILY_BAR_COUNT = 210
DEFAULT_INTRADAY_BAR_COUNT = 400
SETTLE_SECONDS = 1.5
MAX_COLLECTION_ATTEMPTS = 4
STATE_CONFIRMATION_READS = 2
MAX_STATE_CONFIRMATION_POLLS = 4


def load_tjl_module() -> Any:
    spec = importlib.util.spec_from_file_location("scanner_trend_join_long_collector", TJL_SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Trend Join Long scanner module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TJL = load_tjl_module()


@dataclass(frozen=True)
class CollectorOptions:
    intraday_timeframe: str
    premarket_run_id: int | None = None
    max_symbols: int | None = None
    symbols: list[str] | None = None
    db_path: Path | None = None
    output_path: Path = DEFAULT_OUTPUT_PATH
    daily_bar_count: int = DEFAULT_DAILY_BAR_COUNT
    intraday_bar_count: int = DEFAULT_INTRADAY_BAR_COUNT
    now: datetime | None = None
    log: bool = False


@dataclass(frozen=True)
class CollectorStageError(Exception):
    symbol: str
    stage: str
    detail: str

    def __str__(self) -> str:
        return f"{self.stage}: {self.detail}"


def fallback_timeframe_for(timeframe: str) -> str:
    return "1" if normalize_state_resolution(timeframe) != "1" else "D"


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=NEW_YORK)
    return parsed


def parse_args(argv: list[str] | None = None) -> CollectorOptions:
    parser = argparse.ArgumentParser(
        description="Collect TradingView daily + intraday bars for latest premarket DB candidates."
    )
    parser.add_argument(
        "--intraday-timeframe",
        dest="intraday_timeframe",
        default=TJL.DEFAULT_INTRADAY_TIMEFRAME,
        type=TJL.parse_intraday_timeframe_arg,
        help="Intraday timeframe to collect from TradingView.",
    )
    parser.add_argument("--premarket-run-id", dest="premarket_run_id", type=int, help="Specific premarket scan_runs.id to source candidates from.")
    parser.add_argument("--max-symbols", dest="max_symbols", type=int, help="Limit the number of ranked premarket candidates to collect.")
    parser.add_argument(
        "--symbols",
        dest="symbols",
        type=str,
        help="Comma-separated list of symbols to collect directly (bypasses premarket DB candidate lookup).",
    )
    parser.add_argument("--db-path", dest="db_path", type=Path, help="Override SQLite database path.")
    parser.add_argument("--output", dest="output_path", type=Path, default=DEFAULT_OUTPUT_PATH, help="Write normalized collector payload to this path.")
    parser.add_argument("--daily-bars", dest="daily_bar_count", type=int, default=DEFAULT_DAILY_BAR_COUNT, help="Number of daily bars to collect (default 210).")
    parser.add_argument("--intraday-bars", dest="intraday_bar_count", type=int, default=DEFAULT_INTRADAY_BAR_COUNT, help="Number of intraday bars to collect (default 400).")
    parser.add_argument("--now", dest="now", help="Override current time for metadata, as an ISO datetime. Naive values are interpreted as ET.")
    parser.add_argument("--log", "--verbose", dest="log", action="store_true", help="Print detailed collector progress to stderr.")
    args = parser.parse_args(argv)
    return CollectorOptions(
        intraday_timeframe=args.intraday_timeframe,
        premarket_run_id=args.premarket_run_id,
        max_symbols=args.max_symbols,
        symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else None,
        db_path=args.db_path,
        output_path=args.output_path,
        daily_bar_count=args.daily_bar_count,
        intraday_bar_count=args.intraday_bar_count,
        now=parse_datetime(args.now) if args.now else None,
        log=args.log,
    )


def log_message(options: CollectorOptions | None, message: str) -> None:
    if not options or not options.log:
        return
    timestamp = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=sys.stderr)


def write_payload(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def run_tv_cli(args: list[str], *, timeout_seconds: float = 30.0) -> dict[str, Any]:
    command = ["node", str(TRADINGVIEW_CLI_PATH), *args]
    result = subprocess.run(
        command,
        cwd=TRADINGVIEW_CLI_CWD,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if result.returncode != 0:
        message = stderr or stdout or f"tv command failed with exit code {result.returncode}"
        raise RuntimeError(f"{' '.join(args)}: {message}")
    if not stdout:
        return {"success": True}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{' '.join(args)}: CLI returned non-JSON output") from error
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"{' '.join(args)}: {payload.get('error', 'unknown CLI failure')}")
    return payload if isinstance(payload, dict) else {"success": True, "data": payload}


def tradingview_connection_help(error: Exception) -> str:
    detail = str(error)
    if any(token in detail.lower() for token in ["cdp connection failed", "fetch failed", "unhealthy connection", "no tradingview chart target"]):
        return (
            "TradingView Desktop is not connected to CDP on port 9222. "
            "Launch or restart TradingView with remote debugging enabled, open a real chart tab, then retry. "
            "OpenCode option: run the TradingView tv_launch tool, then tv_health_check. "
            "Manual macOS option: /Applications/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=9222. "
            f"Details: {detail}"
        )
    return detail


def expected_intraday_step_seconds(intraday_timeframe: str) -> int:
    return int(intraday_timeframe) * 60


def typical_step_seconds(bars: list[dict[str, Any]]) -> int | None:
    timestamps = [int(value) for bar in bars if isinstance((value := bar.get("time")), (int, float))]
    deltas = [later - earlier for earlier, later in zip(timestamps, timestamps[1:]) if later > earlier]
    if not deltas:
        return None
    counts: dict[int, int] = {}
    for delta in deltas:
        counts[delta] = counts.get(delta, 0) + 1
    return max(counts, key=lambda delta: counts[delta])


def is_daily_bar_series(bars: list[dict[str, Any]]) -> bool:
    step = typical_step_seconds(bars)
    return step is not None and step >= 60 * 60 * 6


def is_intraday_bar_series(bars: list[dict[str, Any]], intraday_timeframe: str) -> bool:
    step = typical_step_seconds(bars)
    expected = expected_intraday_step_seconds(intraday_timeframe)
    return step is not None and abs(step - expected) <= 1


def wait_for_settle() -> None:
    time.sleep(SETTLE_SECONDS)


def rearm_chart_context(symbol: str, timeframe: str, runner: Callable[[list[str]], dict[str, Any]], options: CollectorOptions | None = None) -> None:
    fallback = fallback_timeframe_for(timeframe)
    log_message(options, f"rearming chart context for {symbol}: fallback timeframe {fallback} -> {timeframe}")
    runner(["symbol", symbol])
    wait_for_settle()
    runner(["timeframe", fallback])
    wait_for_settle()
    runner(["timeframe", timeframe])
    wait_for_settle()


def normalize_state_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def normalize_state_resolution(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.endswith("D"):
        return "D"
    return text


def state_matches_requested_symbol_and_timeframe(state: dict[str, Any], symbol: str, timeframe: str) -> bool:
    state_symbol = normalize_state_symbol(state.get("symbol"))
    expected_symbol = normalize_state_symbol(symbol)
    state_resolution = normalize_state_resolution(state.get("resolution"))
    expected_resolution = normalize_state_resolution(timeframe)
    return state_symbol == expected_symbol and state_resolution == expected_resolution


def confirm_chart_state(
    *,
    symbol: str,
    timeframe: str,
    options: CollectorOptions | None = None,
    runner: Callable[[list[str]], dict[str, Any]],
) -> dict[str, Any]:
    consecutive_matches = 0
    last_state: dict[str, Any] = {}
    for poll in range(1, MAX_STATE_CONFIRMATION_POLLS + 1):
        state = runner(["state"])
        last_state = state
        log_message(options, f"chart state poll {poll}/{MAX_STATE_CONFIRMATION_POLLS}: symbol={state.get('symbol')} resolution={state.get('resolution')}")
        if state_matches_requested_symbol_and_timeframe(state, symbol, timeframe):
            consecutive_matches += 1
            if consecutive_matches >= STATE_CONFIRMATION_READS:
                return state
        else:
            consecutive_matches = 0
        wait_for_settle()

    state_symbol = normalize_state_symbol(last_state.get("symbol"))
    state_resolution = normalize_state_resolution(last_state.get("resolution"))
    expected_symbol = normalize_state_symbol(symbol)
    expected_resolution = normalize_state_resolution(timeframe)
    raise ValueError(
        f"chart state mismatch after switch: expected {expected_symbol} {expected_resolution}, "
        f"got {state_symbol or 'unknown'} {state_resolution or 'unknown'}"
    )


def bars_are_consistent(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    timeframe: str,
) -> bool:
    if len(first) != len(second):
        return False

    first_step = typical_step_seconds(first)
    second_step = typical_step_seconds(second)
    if first_step is None or second_step is None or first_step != second_step:
        return False

    if not first or not second:
        return False

    first_times: list[int] = []
    second_times: list[int] = []
    for bar in first:
        value = bar.get("time")
        if not isinstance(value, (int, float)):
            return False
        first_times.append(int(value))
    for bar in second:
        value = bar.get("time")
        if not isinstance(value, (int, float)):
            return False
        second_times.append(int(value))
    return first_times == second_times


def validate_bars_or_raise(
    *,
    bars: Any,
    symbol: str,
    timeframe: str,
    validator: Callable[[list[dict[str, Any]]], bool],
) -> list[dict[str, Any]]:
    if not isinstance(bars, list):
        raise ValueError(f"ohlcv response missing bars for {symbol} {timeframe}")
    if not validator(bars):
        raise ValueError(f"collected bars for {symbol} {timeframe} failed cadence validation")
    return bars


def collect_bars(
    *,
    symbol: str,
    timeframe: str,
    count: int,
    validator: Callable[[list[dict[str, Any]]], bool],
    options: CollectorOptions | None = None,
    cli_runner: Callable[[list[str]], dict[str, Any]] | None = None,
    chart_symbol: str | None = None,
) -> list[dict[str, Any]]:
    runner = cli_runner or run_tv_cli
    tv_sym = chart_symbol or symbol
    last_error: Exception | None = None
    for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
        try:
            log_message(options, f"collect attempt {attempt}/{MAX_COLLECTION_ATTEMPTS}: {symbol} ({tv_sym}) @ {timeframe}")
            runner(["symbol", tv_sym])
            wait_for_settle()
            runner(["timeframe", timeframe])
            wait_for_settle()
            confirm_chart_state(symbol=tv_sym, timeframe=timeframe, options=options, runner=runner)

            first_ohlcv = runner(["ohlcv", "-n", str(count)])
            first_bars = validate_bars_or_raise(
                bars=first_ohlcv.get("bars"),
                symbol=symbol,
                timeframe=timeframe,
                validator=validator,
            )

            wait_for_settle()
            confirm_chart_state(symbol=tv_sym, timeframe=timeframe, options=options, runner=runner)

            second_ohlcv = runner(["ohlcv", "-n", str(count)])
            second_bars = validate_bars_or_raise(
                bars=second_ohlcv.get("bars"),
                symbol=symbol,
                timeframe=timeframe,
                validator=validator,
            )

            if not bars_are_consistent(first_bars, second_bars, timeframe):
                raise ValueError(f"collected bars for {symbol} {timeframe} were not stable across repeated reads")

            return second_bars
        except Exception as error:
            last_error = error
            log_message(options, f"collection retry for {symbol} {timeframe}: {error}")
            try:
                status = runner(["status"])
                log_message(
                    options,
                    f"collector status after failure: cdp_connected={status.get('cdp_connected')} api_available={status.get('api_available')}",
                )
            except Exception as status_error:
                log_message(options, f"collector status check failed after error: {status_error}")
            try:
                rearm_chart_context(tv_sym, timeframe, runner, options)
            except Exception as rearm_error:
                log_message(options, f"collector rearm failed after error: {rearm_error}")
            wait_for_settle()
    raise RuntimeError(f"unable to collect validated bars for {symbol} {timeframe}: {last_error}")


def collect_quote(
    *,
    symbol: str,
    chart_symbol: str,
    timeframe: str,
    options: CollectorOptions | None = None,
    cli_runner: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runner = cli_runner or run_tv_cli
    last_error: Exception | None = None
    for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
        try:
            log_message(options, f"quote attempt {attempt}/{MAX_COLLECTION_ATTEMPTS}: {symbol} ({chart_symbol}) @ {timeframe}")
            runner(["symbol", chart_symbol])
            wait_for_settle()
            runner(["timeframe", timeframe])
            wait_for_settle()
            confirm_chart_state(symbol=chart_symbol, timeframe=timeframe, options=options, runner=runner)
            return runner(["quote"])
        except Exception as error:
            last_error = error
            log_message(options, f"quote retry for {symbol}: {error}")
            try:
                rearm_chart_context(chart_symbol, timeframe, runner, options)
            except Exception as rearm_error:
                log_message(options, f"quote rearm failed after error: {rearm_error}")
            wait_for_settle()
    raise RuntimeError(f"unable to collect quote for {symbol}: {last_error}")


def collect_symbol_payload(
    symbol: str,
    *,
    intraday_timeframe: str,
    daily_bar_count: int,
    intraday_bar_count: int,
    tv_symbol: str | None = None,
    options: CollectorOptions | None = None,
    cli_runner: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runner = cli_runner or run_tv_cli
    tv_sym = tv_symbol or symbol
    try:
        daily_bars = collect_bars(
            symbol=symbol,
            chart_symbol=tv_sym,
            timeframe="D",
            count=daily_bar_count,
            validator=is_daily_bar_series,
            options=options,
            cli_runner=runner,
        )
    except Exception as error:
        raise CollectorStageError(symbol=symbol, stage="daily_bars", detail=str(error)) from error

    try:
        intraday_bars = collect_bars(
            symbol=symbol,
            chart_symbol=tv_sym,
            timeframe=intraday_timeframe,
            count=intraday_bar_count,
            validator=lambda bars: is_intraday_bar_series(bars, intraday_timeframe),
            options=options,
            cli_runner=runner,
        )
    except Exception as error:
        raise CollectorStageError(symbol=symbol, stage="intraday_bars", detail=str(error)) from error

    try:
        quote = collect_quote(
            symbol=symbol,
            chart_symbol=tv_sym,
            timeframe=intraday_timeframe,
            options=options,
            cli_runner=runner,
        )
    except Exception as error:
        raise CollectorStageError(symbol=symbol, stage="quote", detail=str(error)) from error

    return {
        "symbol": symbol,
        "tv_symbol": tv_sym,
        "quote": quote,
        "daily_bars": daily_bars,
        "minute_bars": intraday_bars,
        "intraday_timeframe": intraday_timeframe,
    }


def build_failed_symbol(symbol: str, error: Exception) -> dict[str, str]:
    if isinstance(error, CollectorStageError):
        return {
            "symbol": symbol,
            "stage": error.stage,
            "error": error.detail,
        }
    return {
        "symbol": symbol,
        "stage": "unknown",
        "error": str(error),
    }


def build_payload(
    *,
    symbols: list[str],
    intraday_timeframe: str,
    daily_bar_count: int,
    intraday_bar_count: int,
    collected_at: datetime,
    tv_symbol_map: dict[str, str] | None = None,
    options: CollectorOptions | None = None,
    cli_runner: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runner = cli_runner or run_tv_cli
    symbol_map = tv_symbol_map or {}
    requested_symbols = list(symbols)
    collected_symbols: list[dict[str, Any]] = []
    failed_symbols: list[dict[str, str]] = []

    for symbol in symbols:
        try:
            collected_symbols.append(
                collect_symbol_payload(
                    symbol,
                    intraday_timeframe=intraday_timeframe,
                    daily_bar_count=daily_bar_count,
                    intraday_bar_count=intraday_bar_count,
                    tv_symbol=symbol_map.get(symbol),
                    options=options,
                    cli_runner=runner,
                )
            )
        except Exception as error:
            log_message(options, f"collector symbol failed: {symbol}: {error}")
            failed_symbols.append(build_failed_symbol(symbol, error))

    return {
        "source": COLLECTOR_SOURCE,
        "collected_at": TJL.normalize_now(collected_at).isoformat(),
        "intraday_timeframe": intraday_timeframe,
        "requested_symbols": requested_symbols,
        "successful_symbols": [item["symbol"] for item in collected_symbols],
        "symbols": collected_symbols,
        "failed_symbols": failed_symbols,
    }


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    current = TJL.normalize_now(options.now)
    target_db_path = options.db_path or database_path()

    try:
        try:
            status = run_tv_cli(["status"])
            if not status.get("cdp_connected") or not status.get("api_available"):
                raise RuntimeError(f"TradingView CLI reported an unhealthy connection: {status}")
        except Exception as error:
            raise RuntimeError(tradingview_connection_help(error)) from error

        tv_symbol_map: dict[str, str] = {}

        if options.symbols:
            symbols = []
            for raw in options.symbols:
                if ":" in raw:
                    bare = raw.split(":", 1)[1].strip().upper()
                    tv_symbol_map[bare] = raw.strip()
                    symbols.append(bare)
                else:
                    symbols.append(raw)
        else:
            with closing(sqlite3.connect(target_db_path)) as connection:
                premarket_run = latest_premarket_run(connection, options.premarket_run_id)
                candidates = premarket_candidates(connection, int(premarket_run["id"]), options.max_symbols)
            symbols = [candidate["symbol"] for candidate in candidates]
            for candidate in candidates:
                tv = candidate.get("tv_symbol")
                if tv:
                    tv_symbol_map[candidate["symbol"]] = tv

        if not symbols:
            raise ValueError("no symbols to collect: provide --symbols or ensure a successful premarket scan exists")

        payload = build_payload(
            symbols=symbols,
            intraday_timeframe=options.intraday_timeframe,
            daily_bar_count=options.daily_bar_count,
            intraday_bar_count=options.intraday_bar_count,
            collected_at=current,
            tv_symbol_map=tv_symbol_map,
            options=options,
            cli_runner=run_tv_cli,
        )
        if not payload["symbols"]:
            raise RuntimeError("collector could not gather data for any requested symbols")
        write_payload(payload, options.output_path)
        success_count = len(payload["symbols"])
        failed_count = len(payload["failed_symbols"])
        print(
            f"Collected TJL input for {success_count}/{len(symbols)} symbol(s): "
            f"{','.join(item['symbol'] for item in payload['symbols'])} -> {options.output_path}"
        )
        if failed_count:
            print(
                "Collector skipped symbols: "
                + ", ".join(f"{item['symbol']} ({item['error']})" for item in payload["failed_symbols"]),
                file=sys.stderr,
            )
        return 0
    except Exception as error:
        print(f"TJL collector failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
