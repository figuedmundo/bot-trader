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
BRIDGE_PATH = ROOT_DIR / "scripts" / "bridge-premarket-to-tjl.py"
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


def symbol_payload(symbol: str, current: datetime, price: float, intraday_timeframe: str = "15") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "quote": {"price": price},
        "daily_bars": daily_bars(current),
        "intraday_bars": intraday_bars(current),
        "intraday_timeframe": intraday_timeframe,
    }


class BridgePremarketToTjlTests(unittest.TestCase):
    def test_bridge_reads_latest_premarket_candidates_from_db_and_runs_tjl(self) -> None:
        bridge = load_module("bridge_premarket_to_tjl", BRIDGE_PATH)
        premarket = load_module("scanner_premarket_for_bridge", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "collector-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {
                        "rank": 1,
                        "symbol": "TSLA",
                        "price": 320.0,
                        "gap_pct": 12.5,
                        "premarket_volume": 900000,
                        "catalyst": "TSLA catalyst",
                        "headlines": ["TSLA headline"],
                    },
                    {
                        "rank": 2,
                        "symbol": "AMD",
                        "price": 150.0,
                        "gap_pct": 9.0,
                        "premarket_volume": 800000,
                        "catalyst": "AMD catalyst",
                        "headlines": ["AMD headline"],
                    },
                    {
                        "rank": 3,
                        "symbol": "NVDA",
                        "price": 140.0,
                        "gap_pct": 8.0,
                        "premarket_volume": 700000,
                        "catalyst": "NVDA catalyst",
                        "headlines": ["NVDA headline"],
                    },
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            input_path.write_text(
                json.dumps(
                    {
                        "intraday_timeframe": "15",
                        "symbols": [
                            symbol_payload("TSLA", current, 125),
                            symbol_payload("AMD", current, 123),
                            symbol_payload("NVDA", current, 119),
                            symbol_payload("MU", current, 130),
                        ],
                    }
                ),
                encoding="utf-8",
            )

            exit_code = bridge.main(
                [
                    "--input",
                    str(input_path),
                    "--db-path",
                    str(db_path),
                    "--output",
                    str(artifact_path),
                    "--intraday-timeframe",
                    "15",
                    "--now",
                    "2026-06-24T10:30:00-04:00",
                ]
            )

            self.assertEqual(exit_code, 0)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["source_premarket_scan_run_id"], 1)
            self.assertEqual(artifact["source_premarket_symbols"], ["TSLA", "AMD", "NVDA"])
            self.assertEqual(artifact["missing_collector_symbols"], [])
            self.assertEqual(artifact["candidates_checked"], 3)
            self.assertEqual([item["symbol"] for item in artifact["all_results"]], ["TSLA", "AMD", "NVDA"])

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute("SELECT * FROM scan_runs WHERE scanner_name = 'trend-join-long-scan' ORDER BY id DESC LIMIT 1").fetchone()
                results = connection.execute("SELECT * FROM scan_results WHERE scan_run_id = ? ORDER BY id", (run["id"],)).fetchall()

            self.assertEqual(json.loads(run["watchlist"]), ["TSLA", "AMD", "NVDA"])
            self.assertEqual(len(results), 3)
            self.assertEqual([row["symbol"] for row in results], ["TSLA", "AMD", "NVDA"])
            self.assertEqual(results[0]["timeframe"], "D,15")

    def test_bridge_records_missing_collector_symbols_and_scans_overlap(self) -> None:
        bridge = load_module("bridge_premarket_to_tjl_missing", BRIDGE_PATH)
        premarket = load_module("scanner_premarket_for_bridge_missing", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "collector-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 1, "symbol": "AMD", "price": 150.0, "gap_pct": 9.0, "premarket_volume": 800000, "catalyst": None, "headlines": []},
                    {"rank": 2, "symbol": "NVDA", "price": 140.0, "gap_pct": 8.0, "premarket_volume": 700000, "catalyst": None, "headlines": []},
                    {"rank": 3, "symbol": "MU", "price": 120.0, "gap_pct": 7.0, "premarket_volume": 600000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            input_path.write_text(
                json.dumps(
                    {
                        "intraday_timeframe": "15",
                        "symbols": [
                            symbol_payload("AMD", current, 125),
                            symbol_payload("NVDA", current, 123),
                        ],
                    }
                ),
                encoding="utf-8",
            )

            exit_code = bridge.main(
                [
                    "--input",
                    str(input_path),
                    "--db-path",
                    str(db_path),
                    "--output",
                    str(artifact_path),
                    "--intraday-timeframe",
                    "15",
                    "--now",
                    "2026-06-24T10:30:00-04:00",
                ]
            )

            self.assertEqual(exit_code, 0)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["missing_collector_symbols"], ["MU"])
            self.assertEqual(artifact["candidates_checked"], 2)
            self.assertEqual([item["symbol"] for item in artifact["all_results"]], ["AMD", "NVDA"])

    def test_bridge_accepts_real_collector_payload_shape_with_minute_bars_and_failed_symbols(self) -> None:
        bridge = load_module("bridge_premarket_to_tjl_collector_shape", BRIDGE_PATH)
        premarket = load_module("scanner_premarket_for_bridge_collector_shape", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "collector-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 1, "symbol": "AMD", "price": 150.0, "gap_pct": 9.0, "premarket_volume": 800000, "catalyst": None, "headlines": []},
                    {"rank": 2, "symbol": "NVDA", "price": 140.0, "gap_pct": 8.0, "premarket_volume": 700000, "catalyst": None, "headlines": []},
                    {"rank": 3, "symbol": "MU", "price": 120.0, "gap_pct": 7.0, "premarket_volume": 600000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            input_path.write_text(
                json.dumps(
                    {
                        "source": "tradingview-mcp-cli-live",
                        "intraday_timeframe": "15",
                        "failed_symbols": [{"symbol": "MU", "error": "daily bars stale"}],
                        "symbols": [
                            {
                                "symbol": "AMD",
                                "quote": {"price": 125},
                                "daily_bars": daily_bars(current),
                                "minute_bars": intraday_bars(current),
                                "intraday_timeframe": "15",
                            },
                            {
                                "symbol": "NVDA",
                                "quote": {"price": 123},
                                "daily_bars": daily_bars(current),
                                "minute_bars": intraday_bars(current),
                                "intraday_timeframe": "15",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            exit_code = bridge.main(
                [
                    "--input",
                    str(input_path),
                    "--db-path",
                    str(db_path),
                    "--output",
                    str(artifact_path),
                    "--intraday-timeframe",
                    "15",
                    "--now",
                    "2026-06-24T10:30:00-04:00",
                ]
            )

            self.assertEqual(exit_code, 0)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["missing_collector_symbols"], ["MU"])
            self.assertEqual([item["symbol"] for item in artifact["all_results"]], ["AMD", "NVDA"])

    def test_bridge_rejects_top_level_intraday_timeframe_mismatch(self) -> None:
        bridge = load_module("bridge_premarket_to_tjl_mismatch", BRIDGE_PATH)
        premarket = load_module("scanner_premarket_for_bridge_mismatch", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "collector-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 1, "symbol": "AMD", "price": 150.0, "gap_pct": 9.0, "premarket_volume": 800000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            input_path.write_text(
                json.dumps(
                    {
                        "intraday_timeframe": "1",
                        "symbols": [symbol_payload("AMD", current, 125, intraday_timeframe="1")],
                    }
                ),
                encoding="utf-8",
            )

            exit_code = bridge.main(
                [
                    "--input",
                    str(input_path),
                    "--db-path",
                    str(db_path),
                    "--output",
                    str(artifact_path),
                    "--intraday-timeframe",
                    "15",
                    "--now",
                    "2026-06-24T10:30:00-04:00",
                ]
            )

            self.assertEqual(exit_code, 1)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertIn("collector payload declares intraday timeframe 1", artifact["error"])

    def test_bridge_rejects_selected_symbol_intraday_timeframe_mismatch(self) -> None:
        bridge = load_module("bridge_premarket_to_tjl_symbol_mismatch", BRIDGE_PATH)
        premarket = load_module("scanner_premarket_for_bridge_symbol_mismatch", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "collector-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 1, "symbol": "AMD", "price": 150.0, "gap_pct": 9.0, "premarket_volume": 800000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            input_path.write_text(
                json.dumps(
                    {
                        "intraday_timeframe": "15",
                        "symbols": [symbol_payload("AMD", current, 125, intraday_timeframe="1")],
                    }
                ),
                encoding="utf-8",
            )

            exit_code = bridge.main(
                [
                    "--input",
                    str(input_path),
                    "--db-path",
                    str(db_path),
                    "--output",
                    str(artifact_path),
                    "--intraday-timeframe",
                    "15",
                    "--now",
                    "2026-06-24T10:30:00-04:00",
                ]
            )

            self.assertEqual(exit_code, 1)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertIn("AMD: input declares intraday timeframe 1 but scanner is running with 15", artifact["error"])

    def test_bridge_max_symbols_uses_candidate_rank_not_insert_order(self) -> None:
        bridge = load_module("bridge_premarket_to_tjl_ranked", BRIDGE_PATH)
        premarket = load_module("scanner_premarket_for_bridge_ranked", PREMARKET_PATH)
        current = datetime(2026, 6, 24, 10, 30, tzinfo=NEW_YORK)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            input_path = temp_path / "collector-input.json"
            artifact_path = temp_path / "tjl_watchlist_2026-06-24_1030ET.json"
            create_schema(db_path)

            premarket.persist_scan_run(
                [
                    {"rank": 3, "symbol": "MU", "price": 120.0, "gap_pct": 7.0, "premarket_volume": 600000, "catalyst": None, "headlines": []},
                    {"rank": 1, "symbol": "TSLA", "price": 320.0, "gap_pct": 12.5, "premarket_volume": 900000, "catalyst": None, "headlines": []},
                    {"rank": 2, "symbol": "AMD", "price": 150.0, "gap_pct": 9.0, "premarket_volume": 800000, "catalyst": None, "headlines": []},
                ],
                status="success",
                db_path=db_path,
                artifact_path=temp_path / "premarket_gappers_2026-06-24.json",
            )

            input_path.write_text(
                json.dumps(
                    {
                        "intraday_timeframe": "15",
                        "symbols": [
                            symbol_payload("TSLA", current, 125),
                            symbol_payload("AMD", current, 123),
                            symbol_payload("MU", current, 119),
                        ],
                    }
                ),
                encoding="utf-8",
            )

            exit_code = bridge.main(
                [
                    "--input",
                    str(input_path),
                    "--db-path",
                    str(db_path),
                    "--output",
                    str(artifact_path),
                    "--intraday-timeframe",
                    "15",
                    "--max-symbols",
                    "2",
                    "--now",
                    "2026-06-24T10:30:00-04:00",
                ]
            )

            self.assertEqual(exit_code, 0)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["source_premarket_symbols"], ["TSLA", "AMD"])
            self.assertEqual([item["symbol"] for item in artifact["all_results"]], ["TSLA", "AMD"])


if __name__ == "__main__":
    unittest.main()
