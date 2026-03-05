"""
Standalone reporting CLI.

Usage:
    python -m kalshi_trader.report [summary | signals | anomalies | trades | export]

Reads from the SQLite database and prints analysis.  Does not require the
main loop to be running.  Supports filtering by platform (--platform kalshi|polymarket).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .database import Database
from .config import DB_PATH


def cmd_summary(db: Database):
    s = db.get_pnl_summary()
    if not s or s.get("total_trades", 0) == 0:
        print("No trades recorded yet.")
        return
    resolved = s["wins"] + s["losses"] + s["voids"]
    wr = (s["wins"] / resolved * 100) if resolved else 0
    print("═" * 60)
    print("  P&L SUMMARY")
    print("═" * 60)
    print(f"  Total trades:   {s['total_trades']}")
    print(f"  Open:           {s['open_trades']}")
    print(f"  Resolved:       {resolved}  (W: {s['wins']}  L: {s['losses']}  V: {s['voids']})")
    print(f"  Win rate:       {wr:.1f}%")
    print(f"  Total P&L:      ${s['total_pnl']:+,.4f}")
    print(f"  Gross profit:   ${s['gross_profit']:,.4f}")
    print(f"  Gross loss:     ${s['gross_loss']:,.4f}")
    print(f"  Total risked:   ${s['total_risked']:,.4f}")
    print("═" * 60)


def cmd_signals(db: Database):
    perf = db.get_signal_performance()
    if not perf:
        print("No resolved trades with signal data yet.")
        return
    print(f"{'Signal':<18} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'Avg P&L':>10} {'Total P&L':>11}")
    print(f"{'─'*18} {'─'*7} {'─'*6} {'─'*7} {'─'*10} {'─'*11}")
    for s in perf:
        print(
            f"{s['signal']:<18} {s['trades']:>7} {s['wins']:>6} "
            f"{s['win_rate']*100:>6.1f}% "
            f"${s['avg_pnl']:>+9.4f} "
            f"${s['total_pnl']:>+10.4f}"
        )


def cmd_anomalies(db: Database, limit: int, platform: str | None):
    rows = db.get_recent_anomalies(limit=limit)
    if platform:
        rows = [r for r in rows if r.get("platform", "kalshi") == platform]
    if not rows:
        print("No anomalies recorded yet.")
        return
    print(f"{'ID':>5} {'Plat':<5} {'Time':<20} {'Title / Ticker':<26} {'Side':>4} {'Notional':>10} {'Score':>7} {'Trade?':>6}")
    print(f"{'─'*5} {'─'*5} {'─'*20} {'─'*26} {'─'*4} {'─'*10} {'─'*7} {'─'*6}")
    for r in rows:
        triggered = "YES" if r["triggered_trade"] else ""
        plat = (r.get("platform", "kalshi") or "kalshi")[:3].upper()
        display_name = r.get("market_title", "") or r.get("market_ticker", "")
        print(
            f"{r['id']:>5} {plat:<5} {str(r['detected_at'])[:19]:<20} "
            f"{display_name[:26]:<26} "
            f"{(r['trade_side'] or '?'):>4} "
            f"${r['notional_usd']:>9,.2f} "
            f"{r['composite_score']:>7.3f} "
            f"{triggered:>6}"
        )


def cmd_trades(db: Database, limit: int, platform: str | None):
    rows = db.get_all_trades(limit=limit)
    if platform:
        rows = [r for r in rows if r.get("platform", "kalshi") == platform]
    if not rows:
        print("No paper trades yet.")
        return
    print(f"{'ID':>5} {'Plat':<5} {'Opened':<20} {'Title / Ticker':<26} {'Side':>4} {'Entry':>7} {'Notl':>8} {'Status':>6} {'P&L':>10}")
    print(f"{'─'*5} {'─'*5} {'─'*20} {'─'*26} {'─'*4} {'─'*7} {'─'*8} {'─'*6} {'─'*10}")
    for r in rows:
        pnl_str = f"${r['pnl']:+.4f}" if r['pnl'] is not None else ""
        plat = (r.get("platform", "kalshi") or "kalshi")[:3].upper()
        display_name = r.get("market_title", "") or r.get("market_ticker", "")
        print(
            f"{r['id']:>5} {plat:<5} {str(r['opened_at'])[:19]:<20} "
            f"{display_name[:26]:<26} "
            f"{r['side']:>4} "
            f"${r['entry_price']:>6.4f} "
            f"${r['notional_usd']:>7.2f} "
            f"{r['status']:>6} "
            f"{pnl_str:>10}"
        )


def cmd_export(db: Database, path: str, platform: str | None):
    """Export all paper trades to CSV."""
    rows = db.get_all_trades(limit=100_000)
    if platform:
        rows = [r for r in rows if r.get("platform", "kalshi") == platform]
    if not rows:
        print("No trades to export.")
        return
    out_path = Path(path)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(rows)} trades to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Prediction market paper trader — report & analysis")
    parser.add_argument(
        "--platform", "-p", choices=["kalshi", "polymarket"],
        help="Filter results to a specific platform",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("summary", help="P&L summary")
    sub.add_parser("signals", help="Per-signal performance breakdown")

    p_anom = sub.add_parser("anomalies", help="Recent detected anomalies")
    p_anom.add_argument("-n", "--limit", type=int, default=50)

    p_trades = sub.add_parser("trades", help="Paper trade history")
    p_trades.add_argument("-n", "--limit", type=int, default=50)

    p_export = sub.add_parser("export", help="Export trades to CSV")
    p_export.add_argument("-o", "--output", default="trades_export.csv")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    db = Database()
    platform = getattr(args, "platform", None)

    if args.command == "summary":
        cmd_summary(db)
    elif args.command == "signals":
        cmd_signals(db)
    elif args.command == "anomalies":
        cmd_anomalies(db, args.limit, platform)
    elif args.command == "trades":
        cmd_trades(db, args.limit, platform)
    elif args.command == "export":
        cmd_export(db, args.output, platform)

    db.close()


if __name__ == "__main__":
    main()
