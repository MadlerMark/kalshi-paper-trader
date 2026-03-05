"""
SQLite persistence layer.

Three core tables:
  - anomalies        — every detected anomaly, with per-signal scores
  - paper_trades     — paper positions mirroring suspicious trades
  - market_snapshots — periodic market state for baseline computation

Every table includes a `platform` column ("kalshi" or "polymarket") so that
cross-platform analytics are straightforward.

Everything is written through this module so the DB is the single source of
truth. The schema auto-migrates on startup.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
-- Detected anomalies (one row per suspicious trade / event)
CREATE TABLE IF NOT EXISTS anomalies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT    NOT NULL,          -- ISO-8601 UTC
    platform        TEXT    NOT NULL DEFAULT 'kalshi',  -- 'kalshi' or 'polymarket'
    trade_id        TEXT    UNIQUE,            -- dedup key (trade_id or txHash)
    market_ticker   TEXT    NOT NULL,
    event_ticker    TEXT,
    market_title    TEXT,
    trade_price     REAL,                      -- dollars
    trade_count     REAL,                      -- contract count
    trade_side      TEXT,                      -- 'yes' or 'no'
    trade_time      TEXT,                      -- ISO-8601
    notional_usd    REAL,                      -- price * count
    -- per-signal raw values
    sig_size_usd            REAL,
    sig_minutes_to_close    REAL,
    sig_price_move_pct      REAL,
    sig_volume_ratio        REAL,
    -- per-signal normalised sub-scores (0-1)
    score_size      REAL,
    score_deadline  REAL,
    score_price     REAL,
    score_volume    REAL,
    -- composite
    composite_score REAL    NOT NULL,
    triggered_trade INTEGER NOT NULL DEFAULT 0 -- 1 if a paper trade was opened
);

-- Paper trades
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    anomaly_id      INTEGER NOT NULL REFERENCES anomalies(id),
    platform        TEXT    NOT NULL DEFAULT 'kalshi',
    opened_at       TEXT    NOT NULL,          -- ISO-8601 UTC
    market_ticker   TEXT    NOT NULL,
    event_ticker    TEXT,
    market_title    TEXT,
    side            TEXT    NOT NULL,          -- 'yes' or 'no'
    entry_price     REAL    NOT NULL,          -- dollars per contract
    quantity        REAL    NOT NULL,          -- contracts (fractional ok)
    notional_usd    REAL    NOT NULL,          -- entry_price * quantity
    -- resolution
    status          TEXT    NOT NULL DEFAULT 'open',   -- open / won / lost / void
    resolved_at     TEXT,
    exit_price      REAL,                      -- 1.00 if won, 0.00 if lost
    pnl             REAL,                      -- profit/loss in USD
    -- metadata
    signals_json    TEXT                       -- JSON dict of active signal names & values
);

-- Periodic market snapshots for baseline computation
CREATE TABLE IF NOT EXISTS market_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT    NOT NULL DEFAULT 'kalshi',
    captured_at     TEXT    NOT NULL,
    market_ticker   TEXT    NOT NULL,
    event_ticker    TEXT,
    status          TEXT,
    yes_bid         REAL,
    yes_ask         REAL,
    last_price      REAL,
    volume          INTEGER,
    volume_24h      INTEGER,
    open_interest   INTEGER,
    close_time      TEXT,
    result          TEXT
);

-- Speed up common queries
CREATE INDEX IF NOT EXISTS idx_anomalies_ticker    ON anomalies(market_ticker);
CREATE INDEX IF NOT EXISTS idx_anomalies_trade_id  ON anomalies(trade_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_platform  ON anomalies(platform);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status  ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_ticker  ON paper_trades(market_ticker);
CREATE INDEX IF NOT EXISTS idx_paper_trades_platform ON paper_trades(platform);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker     ON market_snapshots(market_ticker);
CREATE INDEX IF NOT EXISTS idx_snapshots_time       ON market_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_platform   ON market_snapshots(platform);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ---------------------------------------------------------------------------
# Migration from v1 → v2: add platform columns
# ---------------------------------------------------------------------------

_MIGRATE_V1_TO_V2 = [
    "ALTER TABLE anomalies ADD COLUMN platform TEXT NOT NULL DEFAULT 'kalshi'",
    "ALTER TABLE paper_trades ADD COLUMN platform TEXT NOT NULL DEFAULT 'kalshi'",
    "ALTER TABLE market_snapshots ADD COLUMN platform TEXT NOT NULL DEFAULT 'kalshi'",
    "CREATE INDEX IF NOT EXISTS idx_anomalies_platform ON anomalies(platform)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_platform ON paper_trades(platform)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_platform ON market_snapshots(platform)",
]


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------

class Database:
    """Thin SQLite wrapper with auto-migration and convenience methods."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    # ---- connection management -------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    @contextmanager
    def _tx(self):
        """Context manager that commits on success, rolls back on error."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_schema(self):
        # Check current version
        try:
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = int(row["value"]) if row else 0
        except sqlite3.OperationalError:
            current_version = 0

        if current_version < 2:
            if current_version == 1:
                # Migrate v1 → v2: add platform columns
                logger.info("Migrating database schema v1 → v2 (adding platform columns)")
                for stmt in _MIGRATE_V1_TO_V2:
                    try:
                        self.conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                            raise
                self.conn.commit()
            else:
                # Fresh install — create everything
                self.conn.executescript(_DDL)

        # Always ensure all tables/indexes exist (idempotent)
        self.conn.executescript(_DDL)

        # Store schema version
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()
        logger.info("Database ready at %s (schema v%d)", self.db_path, SCHEMA_VERSION)

    # ---- anomalies -------------------------------------------------------

    def insert_anomaly(self, data: dict[str, Any]) -> int:
        """Insert an anomaly row. Returns the new row id."""
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO anomalies (
                    detected_at, platform, trade_id, market_ticker, event_ticker, market_title,
                    trade_price, trade_count, trade_side, trade_time, notional_usd,
                    sig_size_usd, sig_minutes_to_close, sig_price_move_pct, sig_volume_ratio,
                    score_size, score_deadline, score_price, score_volume,
                    composite_score, triggered_trade
                ) VALUES (
                    :detected_at, :platform, :trade_id, :market_ticker, :event_ticker, :market_title,
                    :trade_price, :trade_count, :trade_side, :trade_time, :notional_usd,
                    :sig_size_usd, :sig_minutes_to_close, :sig_price_move_pct, :sig_volume_ratio,
                    :score_size, :score_deadline, :score_price, :score_volume,
                    :composite_score, :triggered_trade
                )
                """,
                data,
            )
            return cur.lastrowid or 0

    def anomaly_exists(self, trade_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM anomalies WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        return row is not None

    def mark_anomaly_triggered(self, anomaly_id: int):
        with self._tx() as c:
            c.execute(
                "UPDATE anomalies SET triggered_trade = 1 WHERE id = ?",
                (anomaly_id,),
            )

    def get_recent_anomalies(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM anomalies ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- paper trades ----------------------------------------------------

    def insert_paper_trade(self, data: dict[str, Any]) -> int:
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO paper_trades (
                    anomaly_id, platform, opened_at, market_ticker, event_ticker, market_title,
                    side, entry_price, quantity, notional_usd, status, signals_json
                ) VALUES (
                    :anomaly_id, :platform, :opened_at, :market_ticker, :event_ticker, :market_title,
                    :side, :entry_price, :quantity, :notional_usd, :status, :signals_json
                )
                """,
                data,
            )
            return cur.lastrowid or 0

    def get_open_trades(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_trade(self, trade_id: int, result: str, exit_price: float, pnl: float):
        """
        Mark a paper trade as resolved.
        result: 'won', 'lost', or 'void'
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._tx() as c:
            c.execute(
                """
                UPDATE paper_trades
                SET status = ?, resolved_at = ?, exit_price = ?, pnl = ?
                WHERE id = ?
                """,
                (result, now, exit_price, pnl, trade_id),
            )

    def get_all_trades(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pnl_summary(self) -> dict:
        """Aggregate P&L across all resolved trades."""
        row = self.conn.execute(
            """
            SELECT
                COUNT(*)                                    AS total_trades,
                COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open_trades,
                COALESCE(SUM(CASE WHEN status = 'won'  THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN status = 'void' THEN 1 ELSE 0 END), 0) AS voids,
                COALESCE(SUM(pnl), 0.0)                    AS total_pnl,
                COALESCE(SUM(notional_usd), 0.0)           AS total_risked,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0.0) AS gross_profit,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0.0) AS gross_loss
            FROM paper_trades
            """
        ).fetchone()
        return dict(row) if row else {}

    def get_signal_performance(self) -> list[dict]:
        """
        Break down win-rate and avg P&L by which signals were active.
        Parses the signals_json column.
        """
        rows = self.conn.execute(
            "SELECT signals_json, pnl, status FROM paper_trades WHERE status IN ('won','lost')"
        ).fetchall()

        from collections import defaultdict
        stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "wins": 0, "total_pnl": 0.0})

        for r in rows:
            sigs = json.loads(r["signals_json"]) if r["signals_json"] else {}
            for sig_name in sigs:
                s = stats[sig_name]
                s["count"] += 1
                if r["status"] == "won":
                    s["wins"] += 1
                s["total_pnl"] += r["pnl"] or 0.0

        result = []
        for name, s in sorted(stats.items()):
            result.append({
                "signal": name,
                "trades": s["count"],
                "wins": s["wins"],
                "win_rate": s["wins"] / s["count"] if s["count"] else 0,
                "total_pnl": s["total_pnl"],
                "avg_pnl": s["total_pnl"] / s["count"] if s["count"] else 0,
            })
        return result

    # ---- market snapshots ------------------------------------------------

    def insert_snapshot(self, data: dict[str, Any]):
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO market_snapshots (
                    platform, captured_at, market_ticker, event_ticker, status,
                    yes_bid, yes_ask, last_price, volume, volume_24h,
                    open_interest, close_time, result
                ) VALUES (
                    :platform, :captured_at, :market_ticker, :event_ticker, :status,
                    :yes_bid, :yes_ask, :last_price, :volume, :volume_24h,
                    :open_interest, :close_time, :result
                )
                """,
                data,
            )

    def get_baseline_volume(self, market_ticker: str, lookback_hours: int = 24) -> float | None:
        """Average volume from recent snapshots for a market."""
        row = self.conn.execute(
            """
            SELECT AVG(volume_24h) as avg_vol
            FROM market_snapshots
            WHERE market_ticker = ?
              AND captured_at >= datetime('now', ? || ' hours')
            """,
            (market_ticker, f"-{lookback_hours}"),
        ).fetchone()
        return row["avg_vol"] if row and row["avg_vol"] else None

    def get_last_snapshot_price(self, market_ticker: str) -> float | None:
        """Most recent snapshot price for a market."""
        row = self.conn.execute(
            """
            SELECT last_price FROM market_snapshots
            WHERE market_ticker = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (market_ticker,),
        ).fetchone()
        return row["last_price"] if row else None

    def get_snapshot_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM market_snapshots").fetchone()
        return row["cnt"] if row else 0

    def get_anomaly_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM anomalies").fetchone()
        return row["cnt"] if row else 0
