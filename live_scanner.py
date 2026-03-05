#!/usr/bin/env python3
"""
Live continuous anomaly scanner for Kalshi + Polymarket.

Polls both platforms for new trades, scores them with the anomaly
detector, and persists flagged trades to SQLite.  Filters by market
volume and excludes closed / settled markets so only actionable
early-detection hits appear.

Filters
-------
- Market volume ≥ MIN_MARKET_VOLUME  (skip tiny / obscure markets)
- Market not settled / closed         (skip resolved events)
- Market result not set               (outcome still unknown)

Usage
-----
  python live_scanner.py                            # defaults
  python live_scanner.py --interval 30              # 30s poll
  python live_scanner.py --min-volume 500000        # $500K+ markets only
  python live_scanner.py --lookback 600             # 10-min initial lookback

Monitor (separate terminal):
  python monitor.py                                 # web dashboard on :8080
  tail -f scanner.log                               # raw JSONL stream
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure the package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshi_trader.config import ANOMALY, KALSHI_ENABLED, POLYMARKET_ENABLED
from kalshi_trader.database import Database
from kalshi_trader.detector import score_trade

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOG_FILE = Path(os.getenv("SCANNER_LOG", "scanner.log"))
STATE_FILE = Path(os.getenv("SCANNER_STATE", "scanner_state.json"))
DB_PATH_LIVE = os.getenv("KALSHI_DB_PATH", "kalshi_trader.db")

# ---------------------------------------------------------------------------
# Logging — file gets everything, console gets WARNING+ (hits + errors)
# ---------------------------------------------------------------------------

def setup_logging():
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_handler.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


logger = logging.getLogger("scanner")


# ---------------------------------------------------------------------------
# Market cache (in-memory, TTL-based)
# ---------------------------------------------------------------------------

class MarketCache:
    """Cache market data to avoid re-fetching every cycle."""

    def __init__(self, ttl_seconds: int = 300):
        self._data: dict[str, tuple[dict, float]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> dict | None:
        entry = self._data.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
        return None

    def put(self, key: str, market: dict):
        self._data[key] = (market, time.time() + self._ttl)

    def put_many(self, markets: dict[str, dict]):
        exp = time.time() + self._ttl
        for key, mkt in markets.items():
            self._data[key] = (mkt, exp)

    @property
    def size(self) -> int:
        return len(self._data)

    def prune(self):
        now = time.time()
        self._data = {k: v for k, v in self._data.items() if v[1] > now}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def get_market_volume(market: dict, platform: str) -> float:
    """Total market volume (contracts for Kalshi ≈ USD, USD for Polymarket)."""
    if platform == "kalshi":
        # volume is contract count; at $1 face value ≈ dollar volume
        return float(market.get("volume", 0) or 0)
    else:
        return float(market.get("volumeNum", 0) or market.get("volume", 0) or 0)


def is_market_live(market: dict, platform: str) -> bool:
    """True if the market is still open with no known outcome."""
    if platform == "kalshi":
        status = (market.get("status") or "").lower()
        result = market.get("result", "")
        if result:                          # outcome already known
            return False
        if status in ("settled", "closed", "finalized"):
            return False
        return True
    else:
        # Polymarket
        if market.get("closed", False):
            return False
        return True


def compute_kalshi_notional(trade: dict) -> float:
    """Trade notional in USD for a Kalshi trade."""
    n = float(trade.get("notional_usd", 0) or 0)
    if n > 0:
        return n
    # yes_price_dollars is a string like "0.2400"
    p_str = trade.get("yes_price_dollars")
    if p_str:
        p = float(p_str)
    else:
        # Fallback: cents → dollars
        p = float(trade.get("yes_price", 0) or 0) / 100.0
    count = float(trade.get("count_fp") or trade.get("count", 0) or 0)
    return p * count


def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1e6:.1f}M"
    if v >= 1_000:
        return f"${v / 1e3:.1f}K"
    return f"${v:.2f}"


# ---------------------------------------------------------------------------
# Live Scanner
# ---------------------------------------------------------------------------

class LiveScanner:
    def __init__(
        self,
        db: Database,
        min_volume: float,
        poll_interval: int,
        lookback: int,
    ):
        self.db = db
        self.min_volume = min_volume
        self.poll_interval = poll_interval

        # Market data cache (5-min TTL)
        self.market_cache = MarketCache(ttl_seconds=300)

        # Watermarks for incremental fetching
        now_ts = int(datetime.now(timezone.utc).timestamp())
        self.kalshi_watermark = now_ts - lookback
        self.poly_watermark = now_ts - lookback

        # Seen trade IDs for dedup (cleared periodically)
        self._seen: set[str] = set()

        # Stats
        self.started_at = datetime.now(timezone.utc)
        self.cycle_count = 0
        self.total_trades = 0
        self.total_anomalies = 0
        self.total_filtered_vol = 0
        self.total_filtered_closed = 0

        # Graceful shutdown
        self._running = True
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, *_):
        logger.info("Shutdown signal received")
        self._running = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        plats = []
        if KALSHI_ENABLED:
            plats.append("Kalshi")
        if POLYMARKET_ENABLED:
            plats.append("Polymarket")

        banner = (
            f"\n{'═' * 60}\n"
            f"  Live Anomaly Scanner\n"
            f"  Min market volume:  {fmt_usd(self.min_volume)}\n"
            f"  Trigger threshold:  {ANOMALY.trigger_threshold}\n"
            f"  Poll interval:      {self.poll_interval}s\n"
            f"  Platforms:          {', '.join(plats)}\n"
            f"  Log file:           {LOG_FILE}\n"
            f"  Dashboard:          python monitor.py\n"
            f"{'═' * 60}\n"
        )
        print(banner)
        logger.info(banner)

        while self._running:
            cycle_start = time.time()
            self.cycle_count += 1
            c_hits = 0
            c_trades = 0
            c_filt = {"vol": 0, "closed": 0}

            try:
                if KALSHI_ENABLED:
                    h, t, f = self._scan_kalshi()
                    c_hits += h
                    c_trades += t
                    c_filt["vol"] += f.get("vol", 0)
                    c_filt["closed"] += f.get("closed", 0)

                if POLYMARKET_ENABLED:
                    h, t, f = self._scan_polymarket()
                    c_hits += h
                    c_trades += t
                    c_filt["vol"] += f.get("vol", 0)
                    c_filt["closed"] += f.get("closed", 0)

            except Exception as e:
                logger.error(f"Cycle {self.cycle_count} error: {e}", exc_info=True)
                print(f"  [!] Error: {e}")

            self.total_trades += c_trades
            self.total_filtered_vol += c_filt["vol"]
            self.total_filtered_closed += c_filt["closed"]
            elapsed = time.time() - cycle_start

            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            line = (
                f"[{ts}] C{self.cycle_count:>4d} | "
                f"{c_trades:>6,d} trades | "
                f"skip {c_filt['vol']}vol {c_filt['closed']}closed | "
                f"{c_hits} hits | {elapsed:.1f}s"
            )
            logger.info(line)
            print(line)

            if c_hits:
                msg = f"  >>> {c_hits} ANOMAL{'Y' if c_hits == 1 else 'IES'} DETECTED"
                logger.warning(msg)
                print(msg)

            self._write_state()

            # Periodic maintenance
            if self.cycle_count % 20 == 0:
                self._seen.clear()
                self.market_cache.prune()

            # Sleep to fill the interval
            remaining = max(0, self.poll_interval - (time.time() - cycle_start))
            if remaining > 0 and self._running:
                time.sleep(remaining)

        print("\nScanner stopped.")
        logger.info("Scanner stopped.")

    # ------------------------------------------------------------------
    # Kalshi
    # ------------------------------------------------------------------

    def _scan_kalshi(self) -> tuple[int, int, dict]:
        from kalshi_trader.api_client import KalshiClient

        client = KalshiClient()
        now_ts = int(datetime.now(timezone.utc).timestamp())

        raw = list(client.iter_recent_trades(since_ts=self.kalshi_watermark))
        if not raw:
            self.kalshi_watermark = now_ts
            return 0, 0, {}

        # Dedup against previously seen
        new = []
        for t in raw:
            tid = t.get("trade_id", "")
            if tid and tid not in self._seen:
                self._seen.add(tid)
                new.append(t)

        if not new:
            self.kalshi_watermark = now_ts
            return 0, len(raw), {}

        # Fetch uncached markets
        tickers = list({t.get("ticker", "") for t in new if t.get("ticker")})
        uncached = [tk for tk in tickers if self.market_cache.get(tk) is None]
        if uncached:
            logger.debug(f"[Kalshi] Fetching {len(uncached)} uncached markets")
            fetched = client.fetch_markets_batch(uncached)
            self.market_cache.put_many(fetched)
            logger.debug(f"[Kalshi] Cached {len(fetched)} markets (total cache: {self.market_cache.size})")

        # Score with filters
        hits = 0
        filt: dict[str, int] = {"vol": 0, "closed": 0}

        for t in new:
            ticker = t.get("ticker", "")
            market = self.market_cache.get(ticker)
            if not market:
                continue

            vol = get_market_volume(market, "kalshi")
            if vol < self.min_volume:
                filt["vol"] += 1
                continue

            if not is_market_live(market, "kalshi"):
                filt["closed"] += 1
                continue

            t["notional_usd"] = compute_kalshi_notional(t)
            t["platform"] = "kalshi"

            result = score_trade(t, market, self.db, platform="kalshi")
            if result.triggered:
                self._record_hit(result, "kalshi", vol)
                hits += 1

        self.kalshi_watermark = now_ts
        return hits, len(new), filt

    # ------------------------------------------------------------------
    # Polymarket
    # ------------------------------------------------------------------

    def _scan_polymarket(self) -> tuple[int, int, dict]:
        from kalshi_trader.polymarket_client import PolymarketClient

        client = PolymarketClient()
        raw = client.get_recent_trades(limit=500)

        # Filter to only new trades
        new = []
        for t in raw:
            ts = int(t.get("timestamp", 0))
            if ts <= self.poly_watermark:
                continue
            tid = t.get("transactionHash", "")
            if tid and tid not in self._seen:
                self._seen.add(tid)
                new.append(t)

        if not new:
            if raw:
                self.poly_watermark = max(
                    int(t.get("timestamp", 0)) for t in raw
                )
            return 0, 0, {}

        # Fetch uncached markets by slug
        slugs = list({t.get("slug", "") for t in new if t.get("slug")})
        uncached = [s for s in slugs if self.market_cache.get(f"poly:{s}") is None]
        if uncached:
            logger.debug(f"[Polymarket] Fetching {len(uncached)} uncached markets")
            fetched = client.fetch_markets_batch(uncached)
            for key, mkt in fetched.items():
                self.market_cache.put(f"poly:{key}", mkt)

        # Score with filters
        hits = 0
        filt: dict[str, int] = {"vol": 0, "closed": 0}

        for rt in new:
            cid = rt.get("conditionId", "")
            slug = rt.get("slug", "")
            raw_m = (
                self.market_cache.get(f"poly:{cid}")
                or self.market_cache.get(f"poly:{slug}")
            )
            if not raw_m:
                continue

            vol = get_market_volume(raw_m, "polymarket")
            if vol < self.min_volume:
                filt["vol"] += 1
                continue

            if not is_market_live(raw_m, "polymarket"):
                filt["closed"] += 1
                continue

            norm_m = client.normalise_market(raw_m)
            norm_t = client.normalise_trade(rt, raw_m)

            result = score_trade(norm_t, norm_m, self.db, platform="polymarket")
            if result.triggered:
                self._record_hit(result, "polymarket", vol)
                hits += 1

        if new:
            self.poly_watermark = max(int(t.get("timestamp", 0)) for t in new)

        return hits, len(new), filt

    # ------------------------------------------------------------------
    # Persist + log a detected anomaly
    # ------------------------------------------------------------------

    def _record_hit(self, result, platform: str, market_volume: float):
        trade = result.trade
        market = result.market
        trade_id = str(
            trade.get("trade_id", "")
            or trade.get("transactionHash", "")
            or ""
        )

        if trade_id and self.db.anomaly_exists(trade_id):
            return

        now = datetime.now(timezone.utc)
        title = (
            market.get("title")
            or market.get("question")
            or trade.get("title")
            or market.get("ticker", "?")
        )[:200]

        data = {
            "detected_at": now.isoformat(),
            "platform": platform,
            "trade_id": trade_id,
            "market_ticker": market.get("ticker", "") or market.get("market_ticker", ""),
            "event_ticker": market.get("event_ticker", trade.get("event_ticker", "")),
            "market_title": title,
            "trade_price": float(trade.get("yes_price_dollars") or trade.get("price", 0) or 0),
            "trade_count": float(trade.get("count_fp") or trade.get("count", 0) or 0),
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
            "triggered_trade": 0,
        }

        self.db.insert_anomaly(data)
        self.total_anomalies += 1

        # Build signal summary
        signals = []
        if result.score_size > 0.1:
            signals.append(f"size={fmt_usd(result.sig_size_usd)}")
        if result.score_deadline > 0.1:
            m = result.sig_minutes_to_close
            signals.append(f"deadline={m:.0f}min" if m else "deadline")
        if result.score_price > 0.1:
            signals.append(f"price={result.sig_price_move_pct:.1f}%")
        if result.score_volume > 0.1:
            signals.append(f"vol×{result.sig_volume_ratio:.1f}")

        ptag = "KAL" if platform == "kalshi" else "PLY"
        hit_line = (
            f"  ★ [{ptag}] score={result.composite_score:.3f} "
            f"{trade.get('taker_side', '?'):>3s} {fmt_usd(result.sig_size_usd):>8s} "
            f"({', '.join(signals)}) mkt_vol={fmt_usd(market_volume)} "
            f"— {title[:80]}"
        )

        # WARNING level → shows on console
        logger.warning(hit_line)

        # Append JSONL for tail -f
        log_entry = {
            "time": now.isoformat(),
            "platform": platform,
            "score": round(result.composite_score, 4),
            "notional": round(result.sig_size_usd, 2),
            "side": trade.get("taker_side", ""),
            "signals": signals,
            "market_volume": round(market_volume, 2),
            "title": title[:120],
            "ticker": data["market_ticker"],
            "trade_id": trade_id,
        }
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # State file for the monitor dashboard
    # ------------------------------------------------------------------

    def _write_state(self):
        state = {
            "started_at": self.started_at.isoformat(),
            "last_cycle": datetime.now(timezone.utc).isoformat(),
            "cycle_count": self.cycle_count,
            "total_trades_scanned": self.total_trades,
            "total_anomalies": self.total_anomalies,
            "total_filtered_volume": self.total_filtered_vol,
            "total_filtered_closed": self.total_filtered_closed,
            "min_market_volume": self.min_volume,
            "threshold": ANOMALY.trigger_threshold,
            "poll_interval": self.poll_interval,
            "market_cache_size": self.market_cache.size,
            "platforms": {
                "kalshi": KALSHI_ENABLED,
                "polymarket": POLYMARKET_ENABLED,
            },
        }
        try:
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Live continuous anomaly scanner for prediction markets"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("SCANNER_POLL_INTERVAL", "60")),
        help="Poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=float(os.getenv("MIN_MARKET_VOLUME_USD", "100000")),
        help="Minimum market volume to consider (default: 100000)",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=300,
        help="Initial lookback window in seconds (default: 300 = 5 min)",
    )
    args = parser.parse_args()

    setup_logging()
    db = Database()
    scanner = LiveScanner(db, args.min_volume, args.interval, args.lookback)
    scanner.run()


if __name__ == "__main__":
    main()
