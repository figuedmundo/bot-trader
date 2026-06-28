from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "scanner.json"
DEFAULT_DB_PATH = ROOT_DIR / "data" / "bot-trader.db"
PREMARKET_SCANNER_NAME = "premarket-gap-scan"


def load_scanner_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def database_path() -> Path:
    configured = load_scanner_config().get("data", {}).get("databasePath")
    if not configured:
        return DEFAULT_DB_PATH
    path = Path(str(configured))
    return path if path.is_absolute() else ROOT_DIR / path


def premarket_scanner_name() -> str:
    return str(load_scanner_config().get("scannerName", PREMARKET_SCANNER_NAME))


def latest_premarket_run(connection: sqlite3.Connection, run_id: int | None) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    if run_id is not None:
        row = connection.execute(
            "SELECT * FROM scan_runs WHERE id = ? AND scanner_name = ?",
            (run_id, premarket_scanner_name()),
        ).fetchone()
        if row is None:
            raise ValueError(f"premarket scan_run_id {run_id} not found")
        if row["status"] != "success":
            raise ValueError(f"premarket scan_run_id {run_id} has status {row['status']}")
        return row

    row = connection.execute(
        "SELECT * FROM scan_runs WHERE scanner_name = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
        (premarket_scanner_name(),),
    ).fetchone()
    if row is None:
        raise ValueError("no successful premarket scan found in SQLite")
    return row


def premarket_candidates(connection: sqlite3.Connection, scan_run_id: int, max_symbols: int | None) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT symbol, gap_pct, premarket_volume, price, metadata FROM scan_results WHERE scan_run_id = ? ORDER BY id",
        (scan_run_id,),
    ).fetchall()
    candidates = []
    for row in rows:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        candidates.append(
            {
                "symbol": str(row["symbol"]).upper(),
                "gap_pct": row["gap_pct"],
                "premarket_volume": row["premarket_volume"],
                "price": row["price"],
                "rank": metadata.get("rank"),
                "catalyst": metadata.get("catalyst"),
                "headlines": metadata.get("headlines", []),
                "exchange": metadata.get("exchange"),
                "tv_symbol": metadata.get("tv_symbol"),
            }
        )
    candidates.sort(key=lambda candidate: (candidate["rank"] is None, candidate["rank"] if candidate["rank"] is not None else 0))
    if max_symbols is not None:
        return candidates[:max_symbols]
    return candidates
