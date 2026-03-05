#!/usr/bin/env python3
"""
Single-pass scanner — fetches recent trades from both platforms,
scores them, and prints ONLY triggered anomalies (positives).

Usage:  python scan_once.py [--lookback SECONDS] [--poly-limit N]
"""

import argparse
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
# Helpers
# ---------------------------------------------------------------------------

def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1e6:.1f}M"
    if v >= 1_000:
        return f"${v/1e3:.1f}K"
    return f"${v:.2f}"


def print_hit(idx: int, result, platform: str):
    trade = result.trade
    market = result.market
    title = (market.get("title") or market.get("question")
             or trade.get("title") or market.get("ticker", "?"))[:72]
    side = trade.get("taker_side", "?")
    notional = result.sig_size_usd
    score = result.composite_score

    signals = []
    if result.score_size > 0.1:
        signals.append(f"size={fmt_usd(result.sig_size_usd)}")
    if result.score_deadline > 0.1:
        m2c = result.sig_minutes_to_close
        signals.append(f"deadline={m2c:.0f}min" if m2c else "deadline")
    if result.score_price > 0.1:
        signals.append(f"Δprice={result.sig_price_move_pct:.1f}%")
    if result.score_volume > 0.1:
        signals.append(f"vol×{result.sig_volume_ratio:.1f}")

    plat = "KAL" if platform == "kalshi" else "PLY"
    print(f"  [{plat}] #{idx:>3d}  score={score:.3f}  {side:>3s}  {fmt_usd(notional):>8s}  "
          f"{', '.join(signals):<40s}  {title}")


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

def scan_polymarket(db: Database, lookback_s: int, limit: int) -> list:
    from kalshi_trader.polymarket_client import PolymarketClient

    client = PolymarketClient()
    now = datetime.now(timezone.utc)
    cutoff_ts = int(now.timestamp()) - lookback_s

    print(f"[Polymarket] Fetching up to {limit} recent trades …")
    raw_trades = client.get_recent_trades(limit=limit)
    recent = [t for t in raw_trades if int(t.get("timestamp", 0)) >= cutoff_ts]
    print(f"[Polymarket] {len(raw_trades)} fetched, {len(recent)} within lookback window")

    if not recent:
        return []

    # Collect unique slugs (reliable key between Data API and Gamma API)
    slugs = list({t.get("slug", "") for t in recent if t.get("slug")})
    print(f"[Polymarket] Fetching {len(slugs)} markets by slug …")
    raw_markets = client.fetch_markets_batch(slugs)
    # Deduplicate (fetch_markets_batch returns both conditionId→market and slug→market)
    unique_cids = {m.get("conditionId", k) for k, m in raw_markets.items()}
    print(f"[Polymarket] Got {len(unique_cids)} unique markets")

    positives = []
    all_scores = []
    for rt in recent:
        cid = rt.get("conditionId", "")
        slug = rt.get("slug", "")
        raw_m = raw_markets.get(cid) or raw_markets.get(slug)
        norm_t = PolymarketClient.normalise_trade(rt, raw_m)
        norm_m = PolymarketClient.normalise_market(raw_m) if raw_m else {
            "ticker": cid, "close_time": "", "volume": 0, "status": "unknown",
            "title": rt.get("title", ""),
        }
        result = score_trade(norm_t, norm_m, db, platform="polymarket")
        all_scores.append(result.composite_score)
        if result.triggered:
            positives.append(result)

    return positives, all_scores


def scan_kalshi(db: Database, lookback_s: int) -> list:
    from kalshi_trader.api_client import KalshiClient

    client = KalshiClient()
    now = datetime.now(timezone.utc)
    since_ts = int(now.timestamp()) - lookback_s

    print(f"[Kalshi] Fetching trades since {datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime('%H:%M:%S UTC')} …")
    raw_trades = list(client.iter_recent_trades(since_ts=since_ts))
    print(f"[Kalshi] {len(raw_trades)} trades fetched")

    if not raw_trades:
        return []

    # Tag and compute notional
    for t in raw_trades:
        t["platform"] = "kalshi"
        if "notional_usd" not in t:
            price = float(t.get("yes_price_dollars") or t.get("yes_price", 0) or 0)
            count = float(t.get("count_fp") or t.get("count", 0) or 0)
            t["notional_usd"] = price * count

    # Collect tickers
    tickers = list({t.get("ticker", "") for t in raw_trades if t.get("ticker")})
    print(f"[Kalshi] Fetching {len(tickers)} markets (batched by URL length) …")
    markets = client.fetch_markets_batch(tickers)
    print(f"[Kalshi] Resolved {len(markets)} / {len(tickers)} markets")

    positives = []
    all_scores = []
    for t in raw_trades:
        ticker = t.get("ticker", "")
        market = markets.get(ticker)
        if not market:
            continue
        market["platform"] = "kalshi"
        result = score_trade(t, market, db, platform="kalshi")
        all_scores.append(result.composite_score)
        if result.triggered:
            positives.append(result)

    return positives, all_scores


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Single-pass anomaly scan")
    parser.add_argument("--lookback", type=int, default=3600,
                        help="Trade lookback window in seconds (default: 3600 = 1 hour)")
    parser.add_argument("--poly-limit", type=int, default=500,
                        help="Max Polymarket trades to fetch (default: 500)")
    parser.add_argument("--kalshi", action="store_true", default=None,
                        help="Scan Kalshi only")
    parser.add_argument("--polymarket", action="store_true", default=None,
                        help="Scan Polymarket only")
    args = parser.parse_args()

    # If neither explicit flag, use .env config
    do_kalshi = KALSHI_ENABLED
    do_poly = POLYMARKET_ENABLED
    if args.kalshi and not args.polymarket:
        do_kalshi, do_poly = True, False
    elif args.polymarket and not args.kalshi:
        do_kalshi, do_poly = False, True

    print(f"═══ Prediction Market Anomaly Scanner ═══")
    print(f"Threshold:  {ANOMALY.trigger_threshold}")
    print(f"Weights:    size={ANOMALY.weight_size}  deadline={ANOMALY.weight_deadline}  "
          f"price={ANOMALY.weight_price_move}  volume={ANOMALY.weight_volume_spike}")
    print(f"Lookback:   {args.lookback}s ({args.lookback//60} min)")
    print(f"Platforms:  {'Kalshi' if do_kalshi else ''}{'  ' if do_kalshi and do_poly else ''}{'Polymarket' if do_poly else ''}")
    print()

    # Use temp DB so we don't pollute the real one during testing
    db = Database(Path("/tmp/scan_once_test.db"))

    all_positives = []
    all_scores = []  # track ALL scores for distribution analysis
    t0 = time.monotonic()

    if do_poly:
        try:
            hits, scores = scan_polymarket(db, args.lookback, args.poly_limit)
            all_positives.extend(hits)
            all_scores.extend(scores)
        except Exception as e:
            print(f"[Polymarket] ERROR: {e}")
            import traceback; traceback.print_exc()

    if do_kalshi:
        try:
            hits, scores = scan_kalshi(db, args.lookback)
            all_positives.extend(hits)
            all_scores.extend(scores)
        except Exception as e:
            print(f"[Kalshi] ERROR: {e}")
            import traceback; traceback.print_exc()

    elapsed = time.monotonic() - t0

    print()
    print(f"═══ Results ({elapsed:.1f}s) ═══")
    if not all_positives:
        print("  No anomalies triggered.")
    else:
        # Sort by score descending
        all_positives.sort(key=lambda r: r.composite_score, reverse=True)
        print(f"  {len(all_positives)} triggered anomalies (score ≥ {ANOMALY.trigger_threshold}):\n")
        print(f"  {'PLAT':>5s}  {'#':>4s}  {'SCORE':>6s}  {'SIDE':>4s}  {'NOTIONAL':>9s}  "
              f"{'SIGNALS':<40s}  {'MARKET'}")
        print(f"  {'─'*5}  {'─'*4}  {'─'*6}  {'─'*4}  {'─'*9}  {'─'*40}  {'─'*40}")
        for i, r in enumerate(all_positives, 1):
            print_hit(i, r, r.platform)

    # Score distribution for debugging
    print()
    if all_scores:
        total = len(all_scores)
        above_01 = sum(1 for s in all_scores if s > 0.1)
        above_02 = sum(1 for s in all_scores if s > 0.2)
        above_03 = sum(1 for s in all_scores if s > 0.3)
        above_035 = sum(1 for s in all_scores if s >= 0.35)
        above_04 = sum(1 for s in all_scores if s >= 0.40)
        above_05 = sum(1 for s in all_scores if s >= 0.50)
        print(f"  Score distribution across ALL {total} trades:")
        print(f"    >0.10: {above_01:>5d} ({above_01/total*100:.1f}%)")
        print(f"    >0.20: {above_02:>5d} ({above_02/total*100:.1f}%)")
        print(f"    >0.30: {above_03:>5d} ({above_03/total*100:.1f}%)")
        print(f"    ≥0.35: {above_035:>5d} ({above_035/total*100:.1f}%)  ← current threshold")
        print(f"    ≥0.40: {above_04:>5d} ({above_04/total*100:.1f}%)")
        print(f"    ≥0.50: {above_05:>5d} ({above_05/total*100:.1f}%)")
        if all_scores:
            sorted_s = sorted(all_scores, reverse=True)
            print(f"    Top 10 scores: {', '.join(f'{s:.3f}' for s in sorted_s[:10])}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
