"""
Configuration and constants for the prediction-market paper trading simulator.

Supports both Kalshi and Polymarket.
All tunables live here. Override via environment variables or .env file.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("KALSHI_DB_PATH", str(PROJECT_ROOT / "kalshi_trader.db")))
KEY_FILE = Path(os.getenv("KALSHI_KEY_FILE", str(PROJECT_ROOT / "kalshi.key")))

# ---------------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------------
API_BASE_URL = os.getenv("KALSHI_API_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")

# ---------------------------------------------------------------------------
# Polymarket API (public — no auth needed for reads)
# ---------------------------------------------------------------------------
POLYMARKET_GAMMA_URL = os.getenv(
    "POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"
)
POLYMARKET_DATA_URL = os.getenv(
    "POLYMARKET_DATA_URL", "https://data-api.polymarket.com"
)
POLYMARKET_ENABLED = os.getenv("POLYMARKET_ENABLED", "true").lower() in ("1", "true", "yes")
KALSHI_ENABLED = os.getenv("KALSHI_ENABLED", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = int(os.getenv("KALSHI_POLL_INTERVAL", "60"))
TRADES_LOOKBACK_SECONDS = int(os.getenv("KALSHI_TRADES_LOOKBACK", "300"))  # 5 min
MARKETS_PAGE_LIMIT = int(os.getenv("KALSHI_MARKETS_PAGE_LIMIT", "200"))
TRADES_PAGE_LIMIT = int(os.getenv("KALSHI_TRADES_PAGE_LIMIT", "1000"))
POLYMARKET_TRADES_LIMIT = int(os.getenv("POLYMARKET_TRADES_LIMIT", "200"))

# ---------------------------------------------------------------------------
# Anomaly detection thresholds
# ---------------------------------------------------------------------------
# CALIBRATION NOTE:
# The reference insider trade is a $553K order on "US striking Iran" placed
# 1 hour before the event actually occurred.  With only the size + deadline
# signals active, the composite score must be well above the trigger threshold.
#
#   score_size(553_000)    → 1.0   (sigmoid well past $10K midpoint)
#   score_deadline(60 min) → 0.5   (linear ramp at boundary)
#   composite = 0.35×1.0 + 0.25×0.5 = 0.475  ── reliably above 0.35 threshold
#
# A $10K trade with no deadline proximity scores only 0.35×0.5 = 0.175, well
# below threshold.  Two signals must combine to trigger, preventing noise.

@dataclass
class AnomalyConfig:
    """Weights and thresholds for the composite anomaly score."""

    # --- individual signal thresholds ---
    large_order_usd: float = float(os.getenv("KALSHI_LARGE_ORDER_USD", "10000"))
    # Orders above this $ value are flagged.  $1M+ is extreme; we cast a wider
    # net at $10k and let the composite score decide.

    mega_order_usd: float = float(os.getenv("KALSHI_MEGA_ORDER_USD", "1000000"))
    # $1M+ gets maximum weight on the size signal.

    deadline_minutes: float = float(os.getenv("KALSHI_DEADLINE_MINUTES", "60"))
    # Trade placed within this many minutes of market close is suspicious.

    price_move_pct: float = float(os.getenv("KALSHI_PRICE_MOVE_PCT", "15.0"))
    # Percent price change (absolute) vs. recent snapshot considered abnormal.

    volume_spike_multiplier: float = float(os.getenv("KALSHI_VOLUME_SPIKE_X", "5.0"))
    # Volume in recent window exceeding baseline by this factor is suspicious.

    # --- composite score weights (sum to 1.0 for interpretability) ---
    # Calibrated so that size + deadline alone can trigger (insider pattern).
    weight_size: float = 0.35
    weight_deadline: float = 0.25
    weight_price_move: float = 0.20
    weight_volume_spike: float = 0.20

    # --- trigger threshold ---
    trigger_threshold: float = float(os.getenv("KALSHI_TRIGGER_THRESHOLD", "0.35"))
    # Composite score >= this triggers a paper trade.  Range 0‒1.
    # Lowered from 0.40 → 0.35 so a mega-order near the deadline reliably
    # triggers even without price-move or volume-spike data.


ANOMALY = AnomalyConfig()

# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------

@dataclass
class PaperTradingConfig:
    """Position sizing and limits."""

    mirror_fraction: float = float(os.getenv("KALSHI_MIRROR_FRACTION", "0.01"))
    # Mirror 1% of the suspicious order's notional value.

    max_position_usd: float = float(os.getenv("KALSHI_MAX_POSITION_USD", "100.0"))
    # Hard cap per paper position in USD.

    min_position_usd: float = float(os.getenv("KALSHI_MIN_POSITION_USD", "1.0"))
    # Floor — don't bother with dust.


PAPER = PaperTradingConfig()

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
TERMINAL_WIDTH = int(os.getenv("KALSHI_TERMINAL_WIDTH", "140"))
