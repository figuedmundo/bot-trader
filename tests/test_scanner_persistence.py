from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
SCANNER_PATH = ROOT_DIR / "scripts" / "scanner-premarket.py"


def load_scanner_module() -> Any:
    spec = importlib.util.spec_from_file_location("scanner_premarket", SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load scanner module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
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


class ScannerPersistenceTests(unittest.TestCase):
    def test_persist_scan_run_inserts_run_results_and_metadata(self) -> None:
        scanner = load_scanner_module()

        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot-trader.db"
            create_schema(db_path)

            scan_run_id = scanner.persist_scan_run(
                [
                    {
                        "rank": 1,
                        "symbol": "AAPL",
                        "price": 175.2,
                        "gap_pct": 7.5,
                        "premarket_volume": 1_200_000,
                        "catalyst": "Beat Q1 earnings and raised guidance",
                        "headlines": ["Apple Reports Strong Q1", "Analysts Boost Price Targets"],
                    }
                ],
                status="success",
                db_path=db_path,
                artifact_path=Path("premarket_gappers_2026-06-24.json"),
            )

            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute("SELECT * FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
                result = connection.execute("SELECT * FROM scan_results WHERE scan_run_id = ?", (scan_run_id,)).fetchone()

            self.assertIsNotNone(run)
            self.assertEqual(run["scanner_name"], "premarket-gap-scan")
            self.assertEqual(run["status"], "success")
            self.assertIsNone(run["error_message"])
            self.assertEqual(json.loads(run["criteria"])["minGapPct"], 3)

            self.assertIsNotNone(result)
            self.assertEqual(result["symbol"], "AAPL")
            self.assertEqual(result["gap_pct"], 7.5)
            self.assertEqual(result["premarket_volume"], 1_200_000)
            self.assertEqual(result["price"], 175.2)

            metadata = json.loads(result["metadata"])
            self.assertEqual(metadata["rank"], 1)
            self.assertEqual(metadata["catalyst"], "Beat Q1 earnings and raised guidance")
            self.assertEqual(metadata["headlines"], ["Apple Reports Strong Q1", "Analysts Boost Price Targets"])
            self.assertEqual(metadata["catalyst_method"], "regex")
            self.assertEqual(metadata["artifact_path"], "premarket_gappers_2026-06-24.json")
            self.assertEqual(metadata["benzinga_url"], "https://www.benzinga.com/quote/AAPL")

    def test_main_persists_success_when_catalyst_worker_fails(self) -> None:
        scanner = load_scanner_module()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "bot-trader.db"
            artifact_path = temp_path / "premarket_gappers_2026-06-24.json"
            create_schema(db_path)

            original_fetch_yahoo_gainers = scanner.fetch_yahoo_gainers
            original_fetch_catalyst = scanner.fetch_catalyst
            original_database_path = scanner.database_path
            original_output_path_for_today = scanner.output_path_for_today

            scanner.fetch_yahoo_gainers = lambda options=None: [scanner.Gapper("AAPL", 175.2, 7.5, 1_200_000)]

            def raise_catalyst_error(symbol: str, options=None):
                raise RuntimeError(f"catalyst failed for {symbol}")

            scanner.fetch_catalyst = raise_catalyst_error
            scanner.database_path = lambda: db_path
            scanner.output_path_for_today = lambda: artifact_path

            try:
                exit_code = scanner.main([])
            finally:
                scanner.fetch_yahoo_gainers = original_fetch_yahoo_gainers
                scanner.fetch_catalyst = original_fetch_catalyst
                scanner.database_path = original_database_path
                scanner.output_path_for_today = original_output_path_for_today

            self.assertEqual(exit_code, 0)

            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(len(artifact["gappers"]), 1)
            self.assertIsNone(artifact["gappers"][0]["catalyst"])
            self.assertEqual(artifact["gappers"][0]["headlines"], [])

            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute("SELECT * FROM scan_runs").fetchone()
                result = connection.execute("SELECT * FROM scan_results").fetchone()

            self.assertEqual(run["status"], "success")
            self.assertIsNone(run["error_message"])
            self.assertEqual(result["symbol"], "AAPL")
            metadata = json.loads(result["metadata"])
            self.assertIsNone(metadata["catalyst"])
            self.assertEqual(metadata["headlines"], [])
            self.assertEqual(metadata["catalyst_method"], "none")


if __name__ == "__main__":
    unittest.main()
