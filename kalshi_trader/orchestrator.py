"""
Main orchestration loop.

Ties together the Kalshi and Polymarket API clients, anomaly detector, paper
trader, database, and terminal UI into a single long-running process.

Cycle:
  1. Fetch recent trades from Kalshi and Polymarket.
  2. Fetch the market metadata we need (batch).
  3. Snapshot market state for baselines.
  4. Score every trade against the anomaly detector.
  5. Log all anomalies to the DB.
  6. Open paper trades for those above the threshold.
  7. Check open paper trades against settled markets and resolve them.
  8. Render the terminal UI.
  9. Sleep until the next poll.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .api_client import KalshiClient
from .polymarket_client import PolymarketClient
from .config import (
    ANOMALY,
    KALSHI_ENABLED,
    POLYMARKET_ENABLED,
    POLL_INTERVAL_SECONDS,
    TRADES_LOOKBACK_SECONDS,
    POLYMARKET_TRADES_LIMIT,
    MARKETS_PAGE_LIMIT,
)
from .database import Database
from .detector import AnomalyResult, score_trade
from .trader import PaperTrader
from .ui import TerminalUI

logger = logging.getLogger(__name__)


class Orchestrator:
    """Main event loop for the paper trading simulator."""

    def __init__(self):
        self.db = Database()
        self.trader = PaperTrader(self.db)
        self.ui = TerminalUI()
        self.cycle = 0
        self.start_time = time.monotonic()
        self._running = True

        # Platform clients — initialised based on config
        self.kalshi: KalshiClient | None = None
        self.polymarket: PolymarketClient | None = None

        if KALSHI_ENABLED:
            self.kalshi = KalshiClient()
            logger.info("Kalshi client enabled")
        if POLYMARKET_ENABLED:
            self.polymarket = PolymarketClient()
            logger.info("Polymarket client enabled")

        if not self.kalshi and not self.polymarket:
            raise RuntimeError("At least one platform must be enabled (KALSHI_ENABLED or POLYMARKET_ENABLED)")

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %d — shutting down gracefully", signum)
        self._running = False

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    def run(self):
        """Block forever, running scan cycles."""
        platforms = []
        if self.kalshi:
            platforms.append("Kalshi")
        if self.polymarket:
            platforms.append("Polymarket")
        logger.info("Starting paper trading simulator — platforms: %s", " + ".join(platforms))
        logger.info("Poll interval: %ds  |  Trades lookback: %ds", POLL_INTERVAL_SECONDS, TRADES_LOOKBACK_SECONDS)

        while self._running:
            error_msg: str | None = None
            anomalies_this_cycle = 0
            trades_opened = 0
            trades_scanned = 0
            markets_scanned = 0
            resolutions: list[dict] = []

            try:
                (
                    markets_scanned,
                    trades_scanned,
                    anomalies_this_cycle,
                    trades_opened,
                    resolutions,
                ) = self._run_cycle()
            except KeyboardInterrupt:
                self._running = False
                break
            except Exception as exc:
                logger.exception("Cycle %d failed", self.cycle)
                error_msg = str(exc)[:120]

            # ---- Render UI ----
            try:
                self.ui.render(
                    cycle=self.cycle,
                    uptime_seconds=time.monotonic() - self.start_time,
                    last_poll=datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                    markets_scanned=markets_scanned,
                    trades_scanned=trades_scanned,
                    anomalies_this_cycle=anomalies_this_cycle,
                    anomalies_total=self.db.get_anomaly_count(),
                    trades_opened_this_cycle=trades_opened,
                    resolutions_this_cycle=resolutions,
                    pnl_summary=self.trader.get_summary(),
                    signal_perf=self.trader.get_signal_breakdown(),
                    recent_anomalies=self.db.get_recent_anomalies(limit=10),
                    open_trades=self.db.get_open_trades(),
                    snapshot_count=self.db.get_snapshot_count(),
                    kalshi_enabled=self.kalshi is not None,
                    polymarket_enabled=self.polymarket is not None,
                    error=error_msg,
                )
            except Exception:
                logger.exception("UI render failed")

            # ---- Sleep ----
            self._interruptible_sleep(POLL_INTERVAL_SECONDS)

        # Shutdown
        self.db.close()
        logger.info("Shutdown complete.  Total cycles: %d", self.cycle)

    def _interruptible_sleep(self, seconds: float):
        """Sleep in small increments so we can respond to signals."""
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    # ------------------------------------------------------------------
    # Single cycle
    # ------------------------------------------------------------------

    def _run_cycle(self) -> tuple[int, int, int, int, list[dict]]:
        self.cycle += 1
        now = datetime.now(timezone.utc)
        logger.info("=== Cycle %d at %s ===", self.cycle, now.isoformat())

        # Aggregate trades from all platforms (normalised dicts)
        all_trades: list[dict] = []
        # Aggregate markets from all platforms
        all_markets: dict[str, dict] = {}  # ticker/conditionId → market dict
        # Map platform → trades for this cycle
        platform_trade_counts: dict[str, int] = {}

        # ------ Kalshi ------
        if self.kalshi:
            k_trades, k_markets = self._fetch_kalshi(now)
            all_trades.extend(k_trades)
            all_markets.update(k_markets)
            platform_trade_counts["kalshi"] = len(k_trades)
            logger.info("Kalshi: %d trades, %d markets", len(k_trades), len(k_markets))

        # ------ Polymarket ------
        if self.polymarket:
            p_trades, p_markets = self._fetch_polymarket(now)
            all_trades.extend(p_trades)
            all_markets.update(p_markets)
            platform_trade_counts["polymarket"] = len(p_trades)
            logger.info("Polymarket: %d trades, %d markets", len(p_trades), len(p_markets))

        trades_scanned = len(all_trades)
        markets_scanned = len(all_markets)

        # 4. Snapshot markets (only active/open ones)
        self._snapshot_markets(all_markets, now)

        # 5. Score & detect anomalies
        anomalies: list[tuple[AnomalyResult, int]] = []
        for trade in all_trades:
            trade_id = trade.get("trade_id", "")
            if not trade_id or self.db.anomaly_exists(trade_id):
                continue  # already processed

            ticker = trade.get("ticker", "") or trade.get("market_ticker", "")
            market = all_markets.get(ticker)
            if not market:
                continue

            platform = trade.get("platform", "kalshi")
            result = score_trade(trade, market, self.db, platform=platform)

            # Log ALL above a minimal floor (store the full dataset)
            if result.composite_score > 0.05:
                anom_data = self._anomaly_to_row(result, now)
                anom_id = self.db.insert_anomaly(anom_data)
                if anom_id and result.triggered:
                    anomalies.append((result, anom_id))

        anomalies_this_cycle = len(anomalies)

        # 6. Open paper trades for triggered anomalies
        trades_opened = 0
        for result, anom_id in anomalies:
            tid = self.trader.open_trade(result, anom_id)
            if tid:
                trades_opened += 1

        # 7. Resolve settled positions
        resolutions = self.trader.resolve_trades(all_markets)

        logger.info(
            "Cycle %d done: %d anomalies, %d new trades, %d resolutions (scanned: %s)",
            self.cycle, anomalies_this_cycle, trades_opened, len(resolutions),
            ", ".join(f"{p}={c}" for p, c in platform_trade_counts.items()),
        )

        return markets_scanned, trades_scanned, anomalies_this_cycle, trades_opened, resolutions

    # ------------------------------------------------------------------
    # Platform-specific fetchers
    # ------------------------------------------------------------------

    def _fetch_kalshi(self, now: datetime) -> tuple[list[dict], dict[str, dict]]:
        """
        Fetch recent Kalshi trades and the markets they reference.

        Returns (trades, markets_by_ticker).
        """
        since_ts = int(now.timestamp()) - TRADES_LOOKBACK_SECONDS
        raw_trades = list(self.kalshi.iter_recent_trades(since_ts=since_ts))

        # Kalshi trades already have the right field names — just tag platform
        trades = []
        for t in raw_trades:
            t["platform"] = "kalshi"
            # Compute notional for Kalshi trades if not present
            if "notional_usd" not in t:
                price = float(t.get("yes_price_dollars") or t.get("price", 0)) or 0.0
                count = float(t.get("count_fp") or t.get("count", 0)) or 0.0
                t["notional_usd"] = price * count
            trades.append(t)

        # Collect tickers we need
        needed_tickers: set[str] = set()
        for t in trades:
            ticker = t.get("ticker", "")
            if ticker:
                needed_tickers.add(ticker)

        # Include tickers from open Kalshi paper trades for resolution
        for pt in self.db.get_open_trades():
            if pt.get("platform", "kalshi") == "kalshi":
                ticker = pt.get("market_ticker", "")
                if ticker:
                    needed_tickers.add(ticker)

        markets: dict[str, dict] = {}
        if needed_tickers:
            raw_markets = self.kalshi.fetch_markets_batch(sorted(needed_tickers))
            for ticker, m in raw_markets.items():
                m["platform"] = "kalshi"
                markets[ticker] = m

        return trades, markets

    def _fetch_polymarket(self, now: datetime) -> tuple[list[dict], dict[str, dict]]:
        """
        Fetch recent Polymarket trades and the markets they reference.

        Returns (normalised_trades, normalised_markets_by_conditionId).
        """
        raw_trades = self.polymarket.get_recent_trades(limit=POLYMARKET_TRADES_LIMIT)

        # Filter to only trades within our lookback window
        cutoff_ts = int(now.timestamp()) - TRADES_LOOKBACK_SECONDS
        recent_raw = [t for t in raw_trades if int(t.get("timestamp", 0)) >= cutoff_ts]

        # Collect the unique slugs we need to look up (slug is the reliable
        # key between the Data API and the Gamma API).
        needed_slugs: set[str] = set()
        for rt in recent_raw:
            slug = rt.get("slug", "")
            if slug:
                needed_slugs.add(slug)

        # Include slugs/tickers from open Polymarket paper trades for resolution
        for pt in self.db.get_open_trades():
            if pt.get("platform", "") == "polymarket":
                ticker = pt.get("market_ticker", "")
                if ticker:
                    needed_slugs.add(ticker)

        # Batch-fetch market metadata (returns both conditionId→market AND slug→market)
        raw_markets_by_key: dict[str, dict] = {}
        if needed_slugs:
            raw_markets_by_key = self.polymarket.fetch_markets_batch(sorted(needed_slugs))

        # Normalise trades — look up market by conditionId first, then slug
        trades: list[dict] = []
        for rt in recent_raw:
            cid = rt.get("conditionId", "")
            slug = rt.get("slug", "")
            raw_market = raw_markets_by_key.get(cid) or raw_markets_by_key.get(slug)
            normalised = PolymarketClient.normalise_trade(rt, raw_market)
            trades.append(normalised)

        # Normalise markets (deduplicate — same market may be keyed by both cid and slug)
        markets: dict[str, dict] = {}
        seen_cids: set[str] = set()
        for key, raw_m in raw_markets_by_key.items():
            cid = raw_m.get("conditionId", key)
            if cid in seen_cids:
                continue
            seen_cids.add(cid)
            norm_m = PolymarketClient.normalise_market(raw_m)
            markets[norm_m.get("ticker", cid)] = norm_m

        return trades, markets

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _snapshot_markets(self, markets: dict[str, dict], now: datetime):
        """Store a snapshot of each non-settled market for baseline computation."""
        now_iso = now.isoformat()
        count = 0
        for ticker, m in markets.items():
            status = m.get("status", "")
            # Snapshot active/open markets (skip settled/closed)
            if status in ("settled", "closed"):
                continue
            platform = m.get("platform", "kalshi")
            self.db.insert_snapshot({
                "platform": platform,
                "captured_at": now_iso,
                "market_ticker": ticker,
                "event_ticker": m.get("event_ticker", ""),
                "status": status,
                "yes_bid": _safe_float(m.get("yes_bid_dollars")),
                "yes_ask": _safe_float(m.get("yes_ask_dollars")),
                "last_price": _safe_float(m.get("last_price_dollars") or m.get("last_price")),
                "volume": m.get("volume"),
                "volume_24h": m.get("volume_24h") or m.get("volume_24hr"),
                "open_interest": m.get("open_interest"),
                "close_time": m.get("close_time", ""),
                "result": m.get("result", ""),
            })
            count += 1
        logger.debug("Snapshotted %d markets", count)

    @staticmethod
    def _anomaly_to_row(result: AnomalyResult, now: datetime) -> dict[str, Any]:
        trade = result.trade
        market = result.market
        return {
            "detected_at": now.isoformat(),
            "platform": result.platform,
            "trade_id": trade.get("trade_id", ""),
            "market_ticker": market.get("ticker", "") or market.get("market_ticker", ""),
            "event_ticker": market.get("event_ticker", ""),
            "market_title": market.get("title", ""),
            "trade_price": _safe_float(trade.get("yes_price_dollars") or trade.get("price")),
            "trade_count": _safe_float(trade.get("count_fp") or trade.get("count")),
            "trade_side": trade.get("taker_side", ""),
            "trade_time": trade.get("created_time", ""),
            "notional_usd": result.sig_size_usd,
            "sig_size_usd": result.sig_size_usd,
            "sig_minutes_to_close": result.sig_minutes_to_close,
            "sig_price_move_pct": result.sig_price_move_pct,
            "sig_volume_ratio": result.sig_volume_ratio,
            "score_size": result.score_size,
            "score_deadline": result.score_deadline,
            "score_price": result.score_price,
            "score_volume": result.score_volume,
            "composite_score": result.composite_score,
            "triggered_trade": 1 if result.triggered else 0,
        }


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
