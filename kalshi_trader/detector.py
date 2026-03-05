"""
Anomaly detection engine.

Scans recent trades from Kalshi and Polymarket against a set of signals and
produces a composite suspicion score for each trade.  Trades exceeding the
threshold are flagged for paper-trade mirroring.

The scoring is platform-agnostic — trades must be normalised into a common
dict format before being passed to ``score_trade()``.

Signals
-------
1. **Size**         — notional value of the trade vs. thresholds.
2. **Deadline**     — proximity to market close time.
3. **Price move**   — price change vs. most recent stored snapshot.
4. **Volume spike** — current volume vs. historical baseline.

Each signal yields a normalised sub-score in [0, 1].  The composite score is
a weighted sum, also in [0, 1].

Calibration
-----------
Reference insider trade: $553 000 on "US striking Iran" placed 1 hour before
the event resolved.  With the current weights this produces a composite of
≈ 0.475 (size=1.0, deadline=0.5), well above the 0.35 trigger threshold.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import ANOMALY as CFG
from .database import Database

logger = logging.getLogger(__name__)


@dataclass
class AnomalyResult:
    """Full scoring breakdown for one trade."""

    trade: dict  # normalised trade dict
    market: dict  # normalised market dict
    platform: str = "kalshi"  # "kalshi" or "polymarket"

    # raw signal values
    sig_size_usd: float = 0.0
    sig_minutes_to_close: float | None = None
    sig_price_move_pct: float | None = None
    sig_volume_ratio: float | None = None

    # normalised sub-scores
    score_size: float = 0.0
    score_deadline: float = 0.0
    score_price: float = 0.0
    score_volume: float = 0.0

    # composite
    composite_score: float = 0.0

    # metadata
    triggered: bool = False
    active_signals: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _sigmoid(x: float, midpoint: float, steepness: float = 5.0) -> float:
    """Soft threshold — returns 0–1, crossing 0.5 at `midpoint`."""
    try:
        return 1.0 / (1.0 + math.exp(-steepness * (x - midpoint) / midpoint))
    except (OverflowError, ZeroDivisionError):
        return 1.0 if x >= midpoint else 0.0


def score_size(notional_usd: float) -> float:
    """
    Score based on trade notional value.
    - Below $10k   → ~0
    - Around $10k  → ~0.5
    - $1M+         → ~1.0
    """
    if notional_usd <= 0:
        return 0.0
    # Two-tier: sigmoid around large_order threshold, boosted for mega orders
    base = _sigmoid(notional_usd, CFG.large_order_usd, steepness=3.0)
    if notional_usd >= CFG.mega_order_usd:
        return min(1.0, base + 0.3)
    return base


def score_deadline(minutes_to_close: float | None) -> float:
    """
    Score based on proximity to market close.
    Closer to close → higher score.
    """
    if minutes_to_close is None:
        return 0.0
    if minutes_to_close <= 0:
        return 1.0  # trade at or past close — very suspicious
    if minutes_to_close > CFG.deadline_minutes:
        return 0.0
    # Linear ramp: at deadline → 0.5, at 0 → 1.0
    return 1.0 - (minutes_to_close / CFG.deadline_minutes) * 0.5


def score_price_move(price_move_pct: float | None) -> float:
    """Score based on absolute price change vs. recent history."""
    if price_move_pct is None:
        return 0.0
    abs_move = abs(price_move_pct)
    return _sigmoid(abs_move, CFG.price_move_pct, steepness=4.0)


def score_volume_spike(volume_ratio: float | None) -> float:
    """Score based on volume / baseline ratio."""
    if volume_ratio is None:
        return 0.0
    if volume_ratio <= 1.0:
        return 0.0
    return _sigmoid(volume_ratio, CFG.volume_spike_multiplier, steepness=3.0)


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API (may have trailing Z or +00:00)."""
    if not ts_str:
        return None
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def score_trade(
    trade: dict[str, Any],
    market: dict[str, Any],
    db: Database,
    platform: str = "kalshi",
) -> AnomalyResult:
    """
    Compute all signal sub-scores and the composite score for one trade.

    Parameters
    ----------
    trade    : normalised trade dict (Kalshi native or Polymarket normalised)
    market   : normalised market dict
    db       : Database for historical baselines
    platform : "kalshi" or "polymarket"
    """
    result = AnomalyResult(trade=trade, market=market, platform=platform)

    # --- 1. Size signal ---
    # Both platforms: try notional_usd first, then compute from price * count
    notional = float(trade.get("notional_usd", 0) or 0)
    if notional <= 0:
        price_dollars = float(trade.get("yes_price_dollars") or trade.get("price", 0)) or 0.0
        count = float(trade.get("count_fp") or trade.get("count", 0)) or 0.0
        notional = price_dollars * count
    result.sig_size_usd = notional
    result.score_size = score_size(notional)

    # --- 2. Deadline signal ---
    trade_time = _parse_ts(trade.get("created_time"))
    close_time = _parse_ts(market.get("close_time"))
    if trade_time and close_time:
        delta = (close_time - trade_time).total_seconds() / 60.0
        result.sig_minutes_to_close = delta
        result.score_deadline = score_deadline(delta)

    # --- 3. Price move signal ---
    market_ticker = market.get("ticker", "") or market.get("market_ticker", "")
    last_snap_price = db.get_last_snapshot_price(market_ticker)
    price_dollars = float(trade.get("yes_price_dollars") or trade.get("price", 0)) or 0.0
    if last_snap_price and last_snap_price > 0 and price_dollars > 0:
        move_pct = ((price_dollars - last_snap_price) / last_snap_price) * 100.0
        result.sig_price_move_pct = move_pct
        result.score_price = score_price_move(move_pct)

    # --- 4. Volume spike signal ---
    baseline_vol = db.get_baseline_volume(market_ticker)
    current_vol = market.get("volume_24h") or market.get("volume_24hr") or market.get("volume")
    if baseline_vol and baseline_vol > 0 and current_vol:
        ratio = float(current_vol) / baseline_vol
        result.sig_volume_ratio = ratio
        result.score_volume = score_volume_spike(ratio)

    # --- Composite ---
    result.composite_score = (
        CFG.weight_size * result.score_size
        + CFG.weight_deadline * result.score_deadline
        + CFG.weight_price_move * result.score_price
        + CFG.weight_volume_spike * result.score_volume
    )

    # Track which signals were active (non-zero)
    if result.score_size > 0.1:
        result.active_signals["large_order"] = result.sig_size_usd
    if result.score_deadline > 0.1:
        result.active_signals["near_deadline"] = result.sig_minutes_to_close or 0
    if result.score_price > 0.1:
        result.active_signals["price_move"] = result.sig_price_move_pct or 0
    if result.score_volume > 0.1:
        result.active_signals["volume_spike"] = result.sig_volume_ratio or 0

    # Flag as triggered if above threshold
    result.triggered = result.composite_score >= CFG.trigger_threshold

    return result
