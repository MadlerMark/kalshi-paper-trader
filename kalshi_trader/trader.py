"""
Paper trading engine.

Responsibilities:
  1. Open paper positions when an anomaly crosses the threshold.
  2. Track open positions with platform attribution.
  3. Resolve positions when their market settles.
  4. Accumulate P&L.

No real orders are ever placed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .config import PAPER as CFG
from .database import Database
from .detector import AnomalyResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def compute_position(notional_usd: float) -> tuple[float, float]:
    """
    Determine how many dollars to allocate for the paper trade.

    Returns (position_usd, quantity) where quantity ≈ dollars / price but
    we just track dollar exposure.
    """
    raw = notional_usd * CFG.mirror_fraction
    clamped = max(CFG.min_position_usd, min(raw, CFG.max_position_usd))
    return clamped, clamped  # for simplicity, quantity ≈ dollar size


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class PaperTrader:
    """Manages the lifecycle of paper trades."""

    def __init__(self, db: Database):
        self.db = db

    # ---- opening trades --------------------------------------------------

    def open_trade(self, anomaly: AnomalyResult, anomaly_id: int) -> int | None:
        """
        Open a paper trade mirroring the suspicious trade.

        Returns the paper_trade id, or None if skipped.
        """
        trade = anomaly.trade
        market = anomaly.market
        platform = anomaly.platform

        taker_side = trade.get("taker_side", "yes")  # mirror the taker
        price_dollars = float(trade.get("yes_price_dollars") or trade.get("price", 0)) or 0.0

        if taker_side == "no":
            # If the suspicious trade is on the "no" side, mirror that.
            price_dollars = float(trade.get("no_price_dollars") or 0) or (1.0 - price_dollars)

        if price_dollars <= 0 or price_dollars >= 1.0:
            logger.warning("Skipping trade — price out of range: %.4f", price_dollars)
            return None

        position_usd, quantity = compute_position(anomaly.sig_size_usd)

        # Quantity in contracts: position_usd / entry_price
        qty_contracts = position_usd / price_dollars if price_dollars > 0 else 0
        if qty_contracts < 0.01:
            return None

        now = datetime.now(timezone.utc).isoformat()

        data = {
            "anomaly_id": anomaly_id,
            "platform": platform,
            "opened_at": now,
            "market_ticker": market.get("ticker", "") or market.get("market_ticker", ""),
            "event_ticker": market.get("event_ticker", ""),
            "market_title": market.get("title", ""),
            "side": taker_side,
            "entry_price": price_dollars,
            "quantity": qty_contracts,
            "notional_usd": position_usd,
            "status": "open",
            "signals_json": json.dumps(anomaly.active_signals),
        }

        trade_id = self.db.insert_paper_trade(data)
        self.db.mark_anomaly_triggered(anomaly_id)

        label = f"[{platform.upper()}]"
        logger.info(
            "PAPER TRADE #%d %s: %s %s @ $%.4f (%.2f contracts, $%.2f notional) — score %.3f",
            trade_id,
            label,
            taker_side.upper(),
            market.get("ticker", "") or market.get("title", "?")[:30],
            price_dollars,
            qty_contracts,
            position_usd,
            anomaly.composite_score,
        )
        return trade_id

    # ---- resolving trades ------------------------------------------------

    def resolve_trades(self, markets_cache: dict[str, dict]) -> list[dict]:
        """
        Check all open trades. If their market has settled, close them out.

        Parameters
        ----------
        markets_cache : dict mapping ticker → market API data

        Returns
        -------
        List of dicts describing each resolution.
        """
        open_trades = self.db.get_open_trades()
        resolutions = []

        for pt in open_trades:
            ticker = pt["market_ticker"]
            market = markets_cache.get(ticker)

            if market is None:
                continue  # market not in current cache — try next cycle

            status = market.get("status", "")
            result = market.get("result", "")

            # Kalshi: status == "settled" and result in ("yes", "no")
            # Polymarket: closed == True (mapped to status "settled")
            if status != "settled" or not result:
                continue

            # Determine outcome
            side = pt["side"]
            entry_price = pt["entry_price"]
            quantity = pt["quantity"]

            if result == side:
                # Won: payout is $1 per contract, cost was entry_price
                exit_price = 1.0
                pnl = (1.0 - entry_price) * quantity
                outcome = "won"
            elif result in ("yes", "no"):
                # Lost: payout is $0
                exit_price = 0.0
                pnl = -entry_price * quantity
                outcome = "lost"
            else:
                # Void / unknown
                exit_price = entry_price
                pnl = 0.0
                outcome = "void"

            self.db.resolve_trade(pt["id"], outcome, exit_price, round(pnl, 4))

            resolution = {
                "trade_id": pt["id"],
                "platform": pt.get("platform", "kalshi"),
                "ticker": ticker,
                "side": side,
                "entry": entry_price,
                "exit": exit_price,
                "pnl": round(pnl, 4),
                "outcome": outcome,
            }
            resolutions.append(resolution)

            logger.info(
                "RESOLVED #%d [%s]: %s %s → %s  P&L $%.4f",
                pt["id"], pt.get("platform", "?"), side, ticker[:40], outcome, pnl,
            )

        return resolutions

    # ---- reporting -------------------------------------------------------

    def get_summary(self) -> dict:
        """Return the aggregate P&L summary from the DB."""
        return self.db.get_pnl_summary()

    def get_signal_breakdown(self) -> list[dict]:
        """Per-signal win-rate and P&L."""
        return self.db.get_signal_performance()
