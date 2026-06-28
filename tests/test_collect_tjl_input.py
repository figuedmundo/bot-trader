from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
COLLECTOR_PATH = ROOT_DIR / "scripts" / "collect-tjl-input.py"
PREMARKET_PATH = ROOT_DIR / "scripts" / "scanner-premarket.py"
NEW_YORK = ZoneInfo("America/New_York")


def load_module(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_schema(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        with connection:
            connection.executescript(
                """
                CREATE TABLE scan_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  scanner_name TEXT NOT NULL,
                  run_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                  watchlist TEXT,
                  criteria TEXT,
                  status TEXT NOT NULL CHECK (status IN ('success', 'error', 'empty')),
                  error_message TEXT
                );

                CREATE TABLE scan_results (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                  symbol TEXT NOT NULL,
                  gap_pct REAL,
                  premarket_volume INTEGER,
                  price REAL,
                  timeframe TEXT,
                  metadata TEXT
                );
                """
            )


def generate_bars(step_seconds: int, count: int) -> list[dict[str, Any]]:
    start = 1_762_000_000
    return [
        {
            "time": start + (index * step_seconds),
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100.5 + index,
            "volume": 1000 + index,
        }
        for index in range(count)
    ]


class CollectTjlInputTests(unittest.TestCase):
    def test_collect_symbol_payload_uses_quote_daily_and_intraday_commands(self) -> None:
        collector = load_module("collect_tjl_input_payload", COLLECTOR_PATH)
        calls: list[list[str]] = []
        current_resolution = {"value": "15"}
        current_symbol = {"value": "AMD"}

        def fake_cli(args: list[str]) -> dict[str, Any]:
            calls.append(args)
            if args[:1] == ["quote"]:
                quote_symbol = args[1] if len(args) > 1 else current_symbol["value"]
                return {"success": True, "symbol": quote_symbol, "last": 123.45}
            if args[:1] == ["symbol"]:
                current_symbol["value"] = args[1]
                return {"success": True}
            if args[:1] == ["timeframe"]:
                current_resolution["value"] = "D" if args[1] == "D" else str(args[1])
                return {"success": True, "timeframe": args[1], "chart_ready": True}
            if args[:1] == ["state"]:
                resolution = current_resolution["value"]
                return {"success": True, "symbol": current_symbol["value"], "resolution": "1D" if resolution == "D" else resolution}
            if args[:2] == ["ohlcv", "-n"]:
                count = int(args[2])
                if count == 210:
                    return {"success": True, "bars": generate_bars(86_400, 210)}
                return {"success": True, "bars": generate_bars(900, 400)}
            raise AssertionError(f"unexpected CLI args: {args}")

        payload = collector.collect_symbol_payload(
            "AMD",
            intraday_timeframe="15",
            daily_bar_count=210,
            intraday_bar_count=400,
            cli_runner=fake_cli,
        )

        self.assertEqual(payload["symbol"], "AMD")
        self.assertEqual(payload["quote"]["last"], 123.45)
        self.assertEqual(payload["intraday_timeframe"], "15")
        self.assertEqual(len(payload["daily_bars"]), 210)
        self.assertEqual(len(payload["minute_bars"]), 400)
        self.assertEqual(calls[-1], ["quote"])

    def test_collect_bars_retries_when_chart_state_does_not_match_requested_symbol_or_timeframe(self) -> None:
        collector = load_module("collect_tjl_input_state", COLLECTOR_PATH)
        attempt_counter = {"count": 0}

        def fake_cli(args: list[str]) -> dict[str, Any]:
            if args[:1] == ["symbol"]:
                return {"success": True}
            if args[:1] == ["timeframe"]:
                return {"success": True}
            if args[:1] == ["state"]:
                attempt_counter["count"] += 1
                if attempt_counter["count"] == 1:
                    return {"success": True, "symbol": "NVDA", "resolution": "1"}
                return {"success": True, "symbol": "AMD", "resolution": "1D"}
            if args[:2] == ["ohlcv", "-n"]:
                return {"success": True, "bars": generate_bars(86_400, 210)}
            raise AssertionError(f"unexpected CLI args: {args}")

        bars = collector.collect_bars(
            symbol="AMD",
            timeframe="D",
            count=210,
            validator=collector.is_daily_bar_series,
            cli_runner=fake_cli,
        )

        self.assertEqual(len(bars), 210)
        self.assertGreaterEqual(attempt_counter["count"], 3)

    def test_collect_bars_retries_when_repeated_ohlcv_reads_are_inconsistent(self) -> None:
        collector = load_module("collect_tjl_input_ohlcv", COLLECTOR_PATH)
        ohlcv_counter = {"count": 0}

        def fake_cli(args: list[str]) -> dict[str, Any]:
            if args[:1] == ["symbol"]:
                return {"success": True}
            if args[:1] == ["timeframe"]:
                return {"success": True}
            if args[:1] == ["state"]:
                return {"success": True, "symbol": "AMD", "resolution": "1D"}
            if args[:1] == ["status"]:
                return {"success": True, "cdp_connected": True, "api_available": True}
            if args[:2] == ["ohlcv", "-n"]:
                ohlcv_counter["count"] += 1
                if ohlcv_counter["count"] == 1:
                    return {"success": True, "bars": generate_bars(86_400, 210)}
                if ohlcv_counter["count"] == 2:
                    shifted = generate_bars(86_400, 210)
                    for bar in shifted:
                        bar["time"] += 86_400
                    return {"success": True, "bars": shifted}
                return {"success": True, "bars": generate_bars(86_400, 210)}
            raise AssertionError(f"unexpected CLI args: {args}")

        bars = collector.collect_bars(
            symbol="AMD",
            timeframe="D",
            count=210,
            validator=collector.is_daily_bar_series,
            cli_runner=fake_cli,
        )

        self.assertEqual(len(bars), 210)
        self.assertGreaterEqual(ohlcv_counter["count"], 4)

    def test_build_payload_collects_each_symbol_in_rank_order(self) -> None:
        collector = load_module("collect_tjl_input_build", COLLECTOR_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        def fake_collect_symbol(symbol: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "quote": {"symbol": symbol, "last": 100.0},
                "daily_bars": generate_bars(86_400, 210),
                "minute_bars": generate_bars(900, 400),
                "intraday_timeframe": kwargs["intraday_timeframe"],
            }

        original = collector.collect_symbol_payload
        collector.collect_symbol_payload = fake_collect_symbol
        try:
            payload = collector.build_payload(
                symbols=["TSLA", "AMD"],
                intraday_timeframe="15",
                daily_bar_count=210,
                intraday_bar_count=400,
                collected_at=current,
            )
        finally:
            collector.collect_symbol_payload = original

        self.assertEqual(payload["source"], collector.COLLECTOR_SOURCE)
        self.assertEqual(payload["intraday_timeframe"], "15")
        self.assertEqual(payload["requested_symbols"], ["TSLA", "AMD"])
        self.assertEqual(payload["successful_symbols"], ["TSLA", "AMD"])
        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["TSLA", "AMD"])
        self.assertEqual(payload["failed_symbols"], [])

    def test_build_payload_records_failed_symbols_without_aborting_successes(self) -> None:
        collector = load_module("collect_tjl_input_partial", COLLECTOR_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        def fake_collect_symbol(symbol: str, **kwargs: Any) -> dict[str, Any]:
            if symbol == "AMD":
                raise collector.CollectorStageError(symbol=symbol, stage="daily_bars", detail="daily bars stale")
            return {
                "symbol": symbol,
                "quote": {"symbol": symbol, "last": 100.0},
                "daily_bars": generate_bars(86_400, 210),
                "minute_bars": generate_bars(900, 400),
                "intraday_timeframe": kwargs["intraday_timeframe"],
            }

        original = collector.collect_symbol_payload
        collector.collect_symbol_payload = fake_collect_symbol
        try:
            payload = collector.build_payload(
                symbols=["TSLA", "AMD"],
                intraday_timeframe="15",
                daily_bar_count=210,
                intraday_bar_count=400,
                collected_at=current,
            )
        finally:
            collector.collect_symbol_payload = original

        self.assertEqual(payload["requested_symbols"], ["TSLA", "AMD"])
        self.assertEqual(payload["successful_symbols"], ["TSLA"])
        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["TSLA"])
        self.assertEqual(payload["failed_symbols"], [{"symbol": "AMD", "stage": "daily_bars", "error": "daily bars stale"}])

    def test_main_reads_ranked_premarket_candidates_and_writes_payload(self) -> None:
        collector = load_module("collect_tjl_input_main", COLLECTOR_PATH)
        premarket = load_module("scanner_premarket_for_collector", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            output_path = temp_path / "tjl-live-input.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 2, "symbol": "AMD", "price": 150.0, "gap_pct": 9.0, "premarket_volume": 800000, "catalyst": None, "headlines": []},
                    {"rank": 1, "symbol": "TSLA", "price": 320.0, "gap_pct": 12.5, "premarket_volume": 900000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            current_resolution = {"value": "15"}
            current_symbol = {"value": "TSLA"}

            def fake_cli(args: list[str]) -> dict[str, Any]:
                if args == ["status"]:
                    return {"success": True, "cdp_connected": True, "api_available": True}
                if args[:1] == ["quote"]:
                    quote_symbol = args[1] if len(args) > 1 else current_symbol["value"]
                    return {"success": True, "symbol": quote_symbol, "last": 123.45}
                if args[:1] == ["symbol"]:
                    current_symbol["value"] = args[1]
                    return {"success": True}
                if args[:1] == ["timeframe"]:
                    current_resolution["value"] = "D" if args[1] == "D" else str(args[1])
                    return {"success": True, "timeframe": args[1], "chart_ready": True}
                if args[:1] == ["state"]:
                    resolution = current_resolution["value"]
                    return {"success": True, "symbol": current_symbol["value"], "resolution": "1D" if resolution == "D" else resolution}
                if args[:2] == ["ohlcv", "-n"]:
                    count = int(args[2])
                    if count == 210:
                        return {"success": True, "bars": generate_bars(86_400, 210)}
                    return {"success": True, "bars": generate_bars(900, 400)}
                raise AssertionError(f"unexpected CLI args: {args}")

            original_runner = collector.run_tv_cli
            collector.run_tv_cli = fake_cli
            try:
                exit_code = collector.main(
                    [
                        "--db-path",
                        str(db_path),
                        "--output",
                        str(output_path),
                        "--intraday-timeframe",
                        "15",
                        "--now",
                        current.isoformat(),
                    ]
                )
            finally:
                collector.run_tv_cli = original_runner

            self.assertEqual(exit_code, 0)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], collector.COLLECTOR_SOURCE)
            self.assertEqual(payload["intraday_timeframe"], "15")
            self.assertEqual(payload["requested_symbols"], ["TSLA", "AMD"])
            self.assertEqual(payload["successful_symbols"], ["TSLA", "AMD"])
            self.assertEqual([item["symbol"] for item in payload["symbols"]], ["TSLA", "AMD"])
            self.assertEqual(payload["failed_symbols"], [])

    def test_main_writes_partial_payload_when_some_symbols_fail(self) -> None:
        collector = load_module("collect_tjl_input_partial_main", COLLECTOR_PATH)
        premarket = load_module("scanner_premarket_for_collector_partial", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            output_path = temp_path / "tjl-live-input.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 1, "symbol": "TSLA", "price": 320.0, "gap_pct": 12.5, "premarket_volume": 900000, "catalyst": None, "headlines": []},
                    {"rank": 2, "symbol": "AMD", "price": 150.0, "gap_pct": 9.0, "premarket_volume": 800000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            original = collector.collect_symbol_payload

            def fake_collect_symbol(symbol: str, **kwargs: Any) -> dict[str, Any]:
                if symbol == "AMD":
                    raise collector.CollectorStageError(symbol=symbol, stage="daily_bars", detail="daily bars stale")
                return {
                    "symbol": symbol,
                    "quote": {"symbol": symbol, "last": 100.0},
                    "daily_bars": generate_bars(86_400, 210),
                    "minute_bars": generate_bars(900, 400),
                    "intraday_timeframe": kwargs["intraday_timeframe"],
                }

            collector.collect_symbol_payload = fake_collect_symbol
            try:
                exit_code = collector.main(
                    [
                        "--db-path",
                        str(db_path),
                        "--output",
                        str(output_path),
                        "--intraday-timeframe",
                        "15",
                        "--now",
                        current.isoformat(),
                    ]
                )
            finally:
                collector.collect_symbol_payload = original

            self.assertEqual(exit_code, 0)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["requested_symbols"], ["TSLA", "AMD"])
            self.assertEqual(payload["successful_symbols"], ["TSLA"])
            self.assertEqual([item["symbol"] for item in payload["symbols"]], ["TSLA"])
            self.assertEqual(payload["failed_symbols"], [{"symbol": "AMD", "stage": "daily_bars", "error": "daily bars stale"}])

    def test_main_fails_when_no_symbols_can_be_collected(self) -> None:
        collector = load_module("collect_tjl_input_none", COLLECTOR_PATH)
        premarket = load_module("scanner_premarket_for_collector_none", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            output_path = temp_path / "tjl-live-input.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 1, "symbol": "TSLA", "price": 320.0, "gap_pct": 12.5, "premarket_volume": 900000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            original = collector.collect_symbol_payload
            collector.collect_symbol_payload = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("daily bars stale"))
            try:
                exit_code = collector.main(
                    [
                        "--db-path",
                        str(db_path),
                        "--output",
                        str(output_path),
                        "--intraday-timeframe",
                        "15",
                        "--now",
                        current.isoformat(),
                    ]
            )
            finally:
                collector.collect_symbol_payload = original

            self.assertEqual(exit_code, 1)
            self.assertFalse(output_path.exists())

    def test_collect_symbol_payload_reports_stage_specific_failures(self) -> None:
        collector = load_module("collect_tjl_input_stage", COLLECTOR_PATH)

        current_resolution = {"value": "15"}
        current_symbol = {"value": "AMD"}

        def fake_cli(args: list[str]) -> dict[str, Any]:
            if args[:1] == ["quote"]:
                raise RuntimeError("quote backend unavailable")
            if args[:1] == ["symbol"]:
                current_symbol["value"] = args[1]
                return {"success": True}
            if args[:1] == ["timeframe"]:
                current_resolution["value"] = "D" if args[1] == "D" else str(args[1])
                return {"success": True, "timeframe": args[1], "chart_ready": True}
            if args[:1] == ["state"]:
                resolution = current_resolution["value"]
                return {"success": True, "symbol": current_symbol["value"], "resolution": "1D" if resolution == "D" else resolution}
            if args[:2] == ["ohlcv", "-n"]:
                count = int(args[2])
                if count == 210:
                    return {"success": True, "bars": generate_bars(86_400, 210)}
                return {"success": True, "bars": generate_bars(900, 400)}
            raise AssertionError(f"unexpected CLI args: {args}")

        with self.assertRaises(collector.CollectorStageError) as error:
            collector.collect_symbol_payload(
                "AMD",
                intraday_timeframe="15",
                daily_bar_count=210,
                intraday_bar_count=400,
                cli_runner=fake_cli,
            )

        self.assertEqual(error.exception.stage, "quote")
        self.assertEqual(error.exception.detail, "quote backend unavailable")


if __name__ == "__main__":
    unittest.main()
