#!/usr/bin/env python3
"""Bridge premarket DB candidates into the Trend Join Long scanner flow.

Reads the latest successful premarket scan from SQLite, filters a normalized
TradingView MCP collection payload down to those candidate symbols, and then
runs the Trend Join Long evaluator on the overlapping set.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import importlib.util
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.premarket_candidates import database_path, latest_premarket_run, premarket_candidates


NEW_YORK = ZoneInfo("America/New_York")
TJL_SCANNER_PATH = ROOT_DIR / "scripts" / "scanner-trend-join-long.py"


def load_tjl_module() -> Any:
    spec = importlib.util.spec_from_file_location("scanner_trend_join_long_bridge", TJL_SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Trend Join Long scanner module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TJL = load_tjl_module()


@dataclass(frozen=True)
class BridgeOptions:
    input_path: Path
    intraday_timeframe: str
    premarket_run_id: int | None = None
    max_symbols: int | None = None
    db_path: Path | None = None
    output_path: Path | None = None
    now: datetime | None = None
    log: bool = False


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=NEW_YORK)
    return parsed


def parse_args(argv: list[str] | None = None) -> BridgeOptions:
    parser = argparse.ArgumentParser(
        description="Run Trend Join Long on the latest premarket DB candidates using a normalized TradingView payload."
    )
    parser.add_argument("--input", dest="input_path", type=Path, required=True, help="Path to normalized TradingView MCP collection JSON.")
    parser.add_argument(
        "--intraday-timeframe",
        dest="intraday_timeframe",
        default=TJL.DEFAULT_INTRADAY_TIMEFRAME,
        type=TJL.parse_intraday_timeframe_arg,
        help="Intraday timeframe already used by the normalized TradingView payload.",
    )
    parser.add_argument("--premarket-run-id", dest="premarket_run_id", type=int, help="Specific premarket scan_runs.id to source candidates from.")
    parser.add_argument("--max-symbols", dest="max_symbols", type=int, help="Limit the number of ranked premarket candidates passed into TJL.")
    parser.add_argument("--db-path", dest="db_path", type=Path, help="Override SQLite database path.")
    parser.add_argument("--output", dest="output_path", type=Path, help="Override TJL artifact output path.")
    parser.add_argument("--now", dest="now", help="Override current time for tests, as an ISO datetime. Naive values are interpreted as ET.")
    parser.add_argument("--log", "--verbose", dest="log", action="store_true", help="Print detailed bridge progress to stderr.")
    args = parser.parse_args(argv)
    return BridgeOptions(
        input_path=args.input_path,
        intraday_timeframe=args.intraday_timeframe,
        premarket_run_id=args.premarket_run_id,
        max_symbols=args.max_symbols,
        db_path=args.db_path,
        output_path=args.output_path,
        now=parse_datetime(args.now) if args.now else None,
        log=args.log,
    )


def log_message(options: BridgeOptions | None, message: str) -> None:
    if not options or not options.log:
        return
    timestamp = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=sys.stderr)


def load_collection(input_path: Path) -> dict[str, Any]:
    return json.loads(input_path.read_text(encoding="utf-8"))


def filter_collection_to_candidates(
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
    intraday_timeframe: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    payload_intraday_timeframe = TJL.declared_intraday_timeframe(payload)
    if payload_intraday_timeframe and payload_intraday_timeframe != intraday_timeframe:
        raise ValueError(
            f"collector payload declares intraday timeframe {payload_intraday_timeframe} "
            f"but bridge is running with {intraday_timeframe}"
        )

    by_symbol = {
        str(item.get("symbol", "")).strip().upper(): item
        for item in TJL.normalize_symbol_payloads(payload)
        if isinstance(item, dict)
    }

    selected = []
    selected_symbols: list[str] = []
    missing_symbols: list[str] = []

    for candidate in candidates:
        symbol = candidate["symbol"]
        item = by_symbol.get(symbol)
        if item is None:
            missing_symbols.append(symbol)
            continue
        selected.append(item)
        selected_symbols.append(symbol)

    if not selected:
        raise ValueError("normalized TradingView payload did not contain any of the latest premarket candidates")

    filtered_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"symbols", "tickers", "results", "intraday_timeframe", "intradayTimeframe"}
    }
    filtered_payload["symbols"] = selected
    filtered_payload["intraday_timeframe"] = intraday_timeframe
    return filtered_payload, selected_symbols, missing_symbols


def annotate_output(
    output: dict[str, Any],
    *,
    premarket_run_id: int,
    premarket_symbols: list[str],
    missing_symbols: list[str],
) -> dict[str, Any]:
    output["source_premarket_scan_run_id"] = premarket_run_id
    output["source_premarket_symbols"] = premarket_symbols
    output["missing_collector_symbols"] = missing_symbols
    return output


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    current = TJL.normalize_now(options.now)
    target_db_path = options.db_path or database_path()
    artifact_path = options.output_path or TJL.output_path_for_now(current)
    selected_symbols: list[str] = []
    candidate_symbols: list[str] = []
    missing_symbols: list[str] = []
    premarket_run_id: int | None = None

    try:
        with closing(sqlite3.connect(target_db_path)) as connection:
            premarket_run = latest_premarket_run(connection, options.premarket_run_id)
            premarket_run_id = int(premarket_run["id"])
            candidates = premarket_candidates(connection, premarket_run_id, options.max_symbols)

        if not candidates:
            raise ValueError(f"premarket scan_run_id {premarket_run_id} did not contain any candidates")

        candidate_symbols = [candidate["symbol"] for candidate in candidates]
        log_message(options, f"loaded {len(candidate_symbols)} premarket candidate(s) from scan_run_id={premarket_run_id}")

        payload = load_collection(options.input_path)
        filtered_payload, selected_symbols, missing_symbols = filter_collection_to_candidates(
            payload,
            candidates,
            options.intraday_timeframe,
        )

        results = TJL.scan_collection(filtered_payload, current, options.intraday_timeframe)
        output = TJL.output_payload(results, current, options.intraday_timeframe, selected_symbols)
        TJL.write_json(
            annotate_output(
                output,
                premarket_run_id=premarket_run_id,
                premarket_symbols=candidate_symbols,
                missing_symbols=missing_symbols,
            ),
            artifact_path,
        )

        status = "success" if any(result["result"] == "PASS" for result in results) else "empty"
        scan_run_id = TJL.persist_scan_run(
            results,
            symbols=selected_symbols,
            intraday_timeframe=options.intraday_timeframe,
            status=status,
            db_path=target_db_path,
            artifact_path=artifact_path,
        )
        log_message(options, f"persisted scan_run_id={scan_run_id} to {target_db_path}")
        print(
            f"Premarket -> TJL bridge: scan_run_id={premarket_run_id}, "
            f"selected={','.join(selected_symbols)}, missing={','.join(missing_symbols) or 'none'}"
        )
        TJL.print_result_lines(results)
        return 0
    except Exception as error:
        symbols_for_error = selected_symbols or candidate_symbols or TJL.default_test_universe()
        output = TJL.error_payload(str(error), current, options.intraday_timeframe, symbols_for_error)
        if premarket_run_id is not None:
            output = annotate_output(
                output,
                premarket_run_id=premarket_run_id,
                premarket_symbols=candidate_symbols,
                missing_symbols=missing_symbols,
            )
        TJL.write_json(output, artifact_path)
        try:
            TJL.persist_scan_run(
                [],
                symbols=symbols_for_error,
                intraday_timeframe=options.intraday_timeframe,
                status="error",
                error_message=str(error),
                db_path=target_db_path,
                artifact_path=artifact_path,
            )
        except Exception as db_error:
            log_message(options, f"failed to persist bridge error scan: {db_error}")
        print(f"Premarket -> TJL bridge failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
