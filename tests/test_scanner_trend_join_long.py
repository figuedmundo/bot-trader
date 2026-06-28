from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
SCANNER_PATH = ROOT_DIR / "scripts" / "scanner-trend-join-long.py"
NEW_YORK = ZoneInfo("America/New_York")


def load_scanner_module() -> Any:
    spec = importlib.util.spec_from_file_location("scanner_trend_join_long", SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Trend Join Long scanner module")
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


def daily_bars(current: datetime) -> list[dict[str, Any]]:
    bars = []
    first_day = current.date() - timedelta(days=200)
    for offset in range(199):
        bars.append(
            {
                "time": datetime.combine(first_day + timedelta(days=offset), datetime.min.time(), tzinfo=NEW_YORK).isoformat(),
                "high": 105,
                "close": 100,
            }
        )
    previous_day = current.date() - timedelta(days=1)
    bars.append(
        {
            "time": datetime.combine(previous_day, datetime.min.time(), tzinfo=NEW_YORK).isoformat(),
            "high": 120,
            "close": 130,
        }
    )
    bars.append(
        {
            "time": datetime.combine(current.date(), datetime.min.time(), tzinfo=NEW_YORK).isoformat(),
            "high": 999,
            "close": 999,
        }
    )
    return bars


def intraday_bars(current: datetime) -> list[dict[str, Any]]:
    today = current.date()
    return [
        {"time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK).replace(hour=4, minute=15).isoformat(), "high": 118},
        {"time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK).replace(hour=9, minute=15).isoformat(), "high": 122},
        {"time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK).replace(hour=9, minute=31).isoformat(), "high": 123},
        {"time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK).replace(hour=10, minute=29).isoformat(), "high": 124},
    ]


def symbol_payload(symbol: str, current: datetime, price: float, intraday_timeframe: str = "1") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "quote": {"price": price},
        "daily_bars": daily_bars(current),
        "intraday_bars": intraday_bars(current),
        "intraday_timeframe": intraday_timeframe,
    }


class TrendJoinLongScannerTests(unittest.TestCase):
    def test_normalize_intraday_timeframe_supports_aliases_and_rejects_4h(self) -> None:
        scanner = load_scanner_module()

        self.assertEqual(scanner.normalize_intraday_timeframe("1"), "1")
        self.assertEqual(scanner.normalize_intraday_timeframe("15m"), "15")
        self.assertEqual(scanner.normalize_intraday_timeframe("1h"), "60")

        with self.assertRaisesRegex(ValueError, "too coarse"):
            scanner.normalize_intraday_timeframe("4h")

    def test_evaluate_symbol_pass_and_failure_modes(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        passing = scanner.evaluate_symbol(symbol_payload("AMD", current, 125), current, "1")
        fail_intraday = scanner.evaluate_symbol(symbol_payload("NVDA", current, 123), current, "1")
        fail_daily = scanner.evaluate_symbol(symbol_payload("MU", current, 119), current, "1")

        self.assertEqual(passing["result"], "PASS")
        self.assertEqual(fail_intraday["result"], "fail_intraday")
        self.assertEqual(fail_daily["result"], "fail_daily")
        self.assertEqual(passing["prev_daily_high"], 120)
        self.assertLess(passing["sma200"], passing["prev_daily_close"])

    def test_scan_collection_rejects_intraday_timeframe_mismatch(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        payload = {
            "intraday_timeframe": "15",
            "symbols": [
                symbol_payload("AMD", current, 125, intraday_timeframe="15"),
                symbol_payload("NVDA", current, 123, intraday_timeframe="15"),
                symbol_payload("MU", current, 119, intraday_timeframe="15"),
            ],
        }

        with self.assertRaisesRegex(ValueError, "input declares intraday timeframe 15"):
            scanner.scan_collection(payload, current, "1")

    def test_symbols_from_payload_drives_dynamic_scan_universe(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        payload = {
            "intraday_timeframe": "15",
            "symbols": [
                symbol_payload("TSLA", current, 125, intraday_timeframe="15"),
                symbol_payload("PLTR", current, 123, intraday_timeframe="15"),
            ],
        }

        self.assertEqual(scanner.symbols_from_payload(payload), ["TSLA", "PLTR"])

        results = scanner.scan_collection(payload, current, "15")

        self.assertEqual([result["symbol"] for result in results], ["TSLA", "PLTR"])

    def test_scan_collection_requires_declared_timeframe_for_non_default_mode(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        payload = {
            "symbols": [
                {
                    "symbol": "AMD",
                    "quote": {"price": 125},
                    "daily_bars": daily_bars(current),
                    "intraday_bars": intraday_bars(current),
                },
                {
                    "symbol": "NVDA",
                    "quote": {"price": 123},
                    "daily_bars": daily_bars(current),
                    "intraday_bars": intraday_bars(current),
                },
                {
                    "symbol": "MU",
                    "quote": {"price": 119},
                    "daily_bars": daily_bars(current),
                    "intraday_bars": intraday_bars(current),
                },
            ]
        }

        with self.assertRaisesRegex(ValueError, "must declare intraday_timeframe"):
            scanner.scan_collection(payload, current, "15")

    def test_missing_input_writes_error_json_and_exits_nonzero(self) -> None:
        scanner = load_scanner_module()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_0900ET.json"
            create_schema(db_path)

            original_database_path = scanner.database_path
            original_output_path_for_now = scanner.output_path_for_now
            scanner.database_path = lambda: db_path
            scanner.output_path_for_now = lambda current=None: artifact_path

            try:
                exit_code = scanner.main(["--now", "2026-06-24T09:00:00-04:00"])
            finally:
                scanner.database_path = original_database_path
                scanner.output_path_for_now = original_output_path_for_now

            self.assertEqual(exit_code, 1)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["timeframes"], {"daily": "D", "intraday": "1"})
            self.assertEqual(artifact["status"], "error")
            self.assertEqual(artifact["hits"], [])
            self.assertIn("--input is required", artifact["error"])

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute("SELECT * FROM scan_runs").fetchone()

            self.assertEqual(run["scanner_name"], "trend-join-long-scan")
            self.assertEqual(run["status"], "error")
            self.assertIn("--input is required", run["error_message"])

    def test_today_hod_includes_latest_completed_bar_before_now(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)
        today = current.date()

        levels = scanner.intraday_levels(
            [
                {
                    "time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK)
                    .replace(hour=4, minute=1)
                    .isoformat(),
                    "high": 110,
                },
                {
                    "time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK)
                    .replace(hour=9, minute=29)
                    .isoformat(),
                    "high": 120,
                },
                {
                    "time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK)
                    .replace(hour=10, minute=29)
                    .isoformat(),
                    "high": 130,
                },
                {
                    "time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK)
                    .replace(hour=10, minute=30)
                    .isoformat(),
                    "high": 999,
                },
                {
                    "time": datetime.combine(today, datetime.min.time(), tzinfo=NEW_YORK)
                    .replace(hour=10, minute=31)
                    .isoformat(),
                    "high": 1000,
                },
            ],
            current,
            "1",
        )

        self.assertEqual(levels["pmh"], 120)
        self.assertEqual(levels["today_hod"], 130)

    def test_main_writes_artifact_and_persists_results(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "tjl-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)
            input_path.write_text(
                json.dumps(
                    {
                        "symbols": [
                            symbol_payload("AMD", current, 125),
                            symbol_payload("NVDA", current, 123),
                            symbol_payload("MU", current, 119),
                        ]
                    }
                ),
                encoding="utf-8",
            )

            original_database_path = scanner.database_path
            original_output_path_for_now = scanner.output_path_for_now
            scanner.database_path = lambda: db_path
            scanner.output_path_for_now = lambda current=None: artifact_path

            try:
                exit_code = scanner.main(["--input", str(input_path), "--now", "2026-06-24T10:30:00-04:00"])
            finally:
                scanner.database_path = original_database_path
                scanner.output_path_for_now = original_output_path_for_now

            self.assertEqual(exit_code, 0)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["timeframes"], {"daily": "D", "intraday": "1"})
            self.assertEqual(artifact["candidates_checked"], 3)
            self.assertEqual([hit["symbol"] for hit in artifact["hits"]], ["AMD"])
            self.assertEqual(
                artifact["all_results"],
                [
                    {"symbol": "AMD", "result": "PASS", "reason": "daily and intraday breakout confirmed"},
                    {
                        "symbol": "NVDA",
                        "result": "fail_intraday",
                        "reason": "curr_px 123.00 must exceed pmh 122.00 and today_hod 124.00",
                    },
                    {
                        "symbol": "MU",
                        "result": "fail_daily",
                        "reason": "curr_px 119.00 must exceed prev_daily_high 120.00, and prev_daily_close 130.00 must exceed sma200 100.15",
                    },
                ],
            )

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute("SELECT * FROM scan_runs").fetchone()
                results = connection.execute("SELECT * FROM scan_results ORDER BY id").fetchall()

            self.assertEqual(run["scanner_name"], "trend-join-long-scan")
            self.assertEqual(run["status"], "success")
            self.assertEqual(json.loads(run["watchlist"]), ["AMD", "NVDA", "MU"])
            self.assertEqual(len(results), 3)
            self.assertEqual(results[0]["symbol"], "AMD")
            self.assertEqual(results[0]["price"], 125)
            self.assertEqual(results[0]["timeframe"], "D,1")
            metadata = json.loads(results[0]["metadata"])
            self.assertEqual(metadata["result"], "PASS")
            self.assertEqual(metadata["timeframes"], {"daily": "D", "intraday": "1"})
            self.assertEqual(metadata["artifact_path"], str(artifact_path))

    def test_main_persists_selected_intraday_timeframe(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "tjl-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)
            input_path.write_text(
                json.dumps(
                    {
                        "intraday_timeframe": "15",
                        "symbols": [
                            symbol_payload("AMD", current, 125, intraday_timeframe="15"),
                            symbol_payload("NVDA", current, 123, intraday_timeframe="15"),
                            symbol_payload("MU", current, 119, intraday_timeframe="15"),
                        ]
                    }
                ),
                encoding="utf-8",
            )

            original_database_path = scanner.database_path
            original_output_path_for_now = scanner.output_path_for_now
            scanner.database_path = lambda: db_path
            scanner.output_path_for_now = lambda current=None: artifact_path

            try:
                exit_code = scanner.main(
                    [
                        "--input",
                        str(input_path),
                        "--intraday-timeframe",
                        "15",
                        "--now",
                        "2026-06-24T10:30:00-04:00",
                    ]
                )
            finally:
                scanner.database_path = original_database_path
                scanner.output_path_for_now = original_output_path_for_now

            self.assertEqual(exit_code, 0)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["timeframes"], {"daily": "D", "intraday": "15"})

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                result = connection.execute("SELECT * FROM scan_results ORDER BY id").fetchone()

            self.assertEqual(result["timeframe"], "D,15")
            metadata = json.loads(result["metadata"])
            self.assertEqual(metadata["timeframes"], {"daily": "D", "intraday": "15"})

    def test_scanner_runs_outside_trading_hours_without_special_flag(self) -> None:
        scanner = load_scanner_module()
        current = datetime(2026, 6, 24, 20, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "tjl-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_2030ET.json"
            create_schema(db_path)
            input_path.write_text(
                json.dumps(
                    {
                        "symbols": [
                            symbol_payload("AMD", current, 125),
                            symbol_payload("NVDA", current, 123),
                            symbol_payload("MU", current, 119),
                        ]
                    }
                ),
                encoding="utf-8",
            )

            original_database_path = scanner.database_path
            original_output_path_for_now = scanner.output_path_for_now
            scanner.database_path = lambda: db_path
            scanner.output_path_for_now = lambda current=None: artifact_path

            try:
                exit_code = scanner.main(
                    [
                        "--input",
                        str(input_path),
                        "--now",
                        "2026-06-24T20:30:00-04:00",
                    ]
                )
            finally:
                scanner.database_path = original_database_path
                scanner.output_path_for_now = original_output_path_for_now

            self.assertEqual(exit_code, 0)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual([hit["symbol"] for hit in artifact["hits"]], ["AMD"])

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute("SELECT * FROM scan_runs").fetchone()
                results = connection.execute("SELECT * FROM scan_results ORDER BY id").fetchall()

            self.assertEqual(run["status"], "success")
            self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
