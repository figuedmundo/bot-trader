const fs = require('node:fs');
const path = require('node:path');
const Database = require('better-sqlite3');

const rootDir = path.resolve(__dirname, '..');
const dataDir = path.join(rootDir, 'data');
const dbPath = path.join(dataDir, 'bot-trader.db');

fs.mkdirSync(dataDir, { recursive: true });

const db = new Database(dbPath);

try {
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');

  db.exec(`
    CREATE TABLE IF NOT EXISTS scan_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scanner_name TEXT NOT NULL,
      run_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      watchlist TEXT,
      criteria TEXT,
      status TEXT NOT NULL CHECK (status IN ('success', 'error', 'empty')),
      error_message TEXT
    );

    CREATE TABLE IF NOT EXISTS scan_results (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
      symbol TEXT NOT NULL,
      gap_pct REAL,
      premarket_volume INTEGER,
      price REAL,
      timeframe TEXT,
      metadata TEXT
    );

    CREATE TABLE IF NOT EXISTS alerts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_run_id INTEGER REFERENCES scan_runs(id) ON DELETE SET NULL,
      channel TEXT NOT NULL CHECK (channel IN ('telegram')),
      sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      payload TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
      error_message TEXT
    );

    CREATE TABLE IF NOT EXISTS logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      level TEXT NOT NULL,
      source TEXT NOT NULL,
      message TEXT NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS backtest_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT NOT NULL,
      strategy_name TEXT NOT NULL,
      run_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      pine_script TEXT,
      status TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS backtest_metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      backtest_run_id INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
      metric_name TEXT NOT NULL,
      metric_value REAL
    );

    CREATE INDEX IF NOT EXISTS idx_scan_runs_run_at ON scan_runs(run_at);
    CREATE INDEX IF NOT EXISTS idx_scan_results_scan_run_id ON scan_results(scan_run_id);
    CREATE INDEX IF NOT EXISTS idx_scan_results_symbol ON scan_results(symbol);
    CREATE INDEX IF NOT EXISTS idx_alerts_scan_run_id ON alerts(scan_run_id);
    CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs(created_at);
    CREATE INDEX IF NOT EXISTS idx_backtest_runs_symbol ON backtest_runs(symbol);
    CREATE INDEX IF NOT EXISTS idx_backtest_metrics_backtest_run_id ON backtest_metrics(backtest_run_id);
  `);

  const tables = db
    .prepare(`
      SELECT name
      FROM sqlite_master
      WHERE type = 'table'
        AND name NOT LIKE 'sqlite_%'
      ORDER BY name
    `)
    .all()
    .map((row) => row.name);

  console.log(`Migrated ${dbPath}`);
  console.log(`Tables: ${tables.join(', ')}`);
} finally {
  db.close();
}
