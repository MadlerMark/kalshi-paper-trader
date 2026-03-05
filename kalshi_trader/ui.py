"""
Terminal UI — clean, information-dense status display.

Supports multi-platform (Kalshi + Polymarket) with platform badges.
Renders to stdout using ANSI codes.  No curses dependency — works in any
modern terminal.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from .config import TERMINAL_WIDTH, ANOMALY

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
WHITE = "\033[97m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"

CLEAR_SCREEN = "\033[2J\033[H"

# Platform badges
_PLATFORM_BADGE = {
    "kalshi": f"{CYAN}KAL{RESET}",
    "polymarket": f"{MAGENTA}PLY{RESET}",
}


def _badge(platform: str) -> str:
    return _PLATFORM_BADGE.get(platform, f"{DIM}{platform[:3].upper()}{RESET}")


def _bar(value: float, width: int = 20, filled: str = "█", empty: str = "░") -> str:
    """Render a horizontal bar 0–1."""
    n = int(value * width)
    return filled * n + empty * (width - n)


def _color_pnl(value: float) -> str:
    if value > 0:
        return f"{GREEN}+${value:,.2f}{RESET}"
    elif value < 0:
        return f"{RED}-${abs(value):,.2f}{RESET}"
    return f"${value:,.2f}"


def _color_score(score: float) -> str:
    if score >= ANOMALY.trigger_threshold:
        return f"{RED}{score:.3f}{RESET}"
    elif score >= ANOMALY.trigger_threshold * 0.7:
        return f"{YELLOW}{score:.3f}{RESET}"
    return f"{DIM}{score:.3f}{RESET}"


def _trunc(s: str, width: int) -> str:
    return s[:width - 1] + "…" if len(s) > width else s


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class TerminalUI:
    """Stateless renderer — call render() each cycle."""

    def __init__(self, width: int = TERMINAL_WIDTH):
        self.w = width

    def _line(self, char: str = "─") -> str:
        return char * self.w

    def render(
        self,
        *,
        cycle: int,
        uptime_seconds: float,
        last_poll: str,
        markets_scanned: int,
        trades_scanned: int,
        anomalies_this_cycle: int,
        anomalies_total: int,
        trades_opened_this_cycle: int,
        resolutions_this_cycle: list[dict],
        pnl_summary: dict,
        signal_perf: list[dict],
        recent_anomalies: list[dict],
        open_trades: list[dict],
        snapshot_count: int,
        kalshi_enabled: bool = True,
        polymarket_enabled: bool = True,
        error: str | None = None,
    ):
        lines: list[str] = []
        a = lines.append

        a(CLEAR_SCREEN)
        # Header with platform badges
        platform_tags = []
        if kalshi_enabled:
            platform_tags.append(f"{CYAN}KALSHI{RESET}")
        if polymarket_enabled:
            platform_tags.append(f"{MAGENTA}POLYMARKET{RESET}")
        platform_str = " + ".join(platform_tags) if platform_tags else "NONE"
        a(f"{BOLD} PREDICTION MARKET PAPER TRADER {RESET}  [{platform_str}]")
        a(self._line("═"))

        # ---- Status bar ----
        hrs = int(uptime_seconds // 3600)
        mins = int((uptime_seconds % 3600) // 60)
        secs = int(uptime_seconds % 60)
        a(
            f" Cycle {BOLD}#{cycle}{RESET}  │  "
            f"Uptime {hrs:02d}:{mins:02d}:{secs:02d}  │  "
            f"Last poll: {last_poll}  │  "
            f"Snapshots: {snapshot_count:,}"
        )

        if error:
            a(f" {RED}ERROR: {error}{RESET}")

        a(self._line())

        # ---- Scan summary ----
        a(f" {BOLD}SCAN{RESET}  Markets: {markets_scanned:,}  │  "
          f"Trades checked: {trades_scanned:,}  │  "
          f"Anomalies: {YELLOW}{anomalies_this_cycle}{RESET} this cycle "
          f"({anomalies_total:,} total)  │  "
          f"New paper trades: {trades_opened_this_cycle}")

        a(self._line())

        # ---- P&L summary ----
        s = pnl_summary
        total_trades = s.get("total_trades", 0)
        open_count = s.get("open_trades", 0)
        wins = s.get("wins", 0)
        losses = s.get("losses", 0)
        total_pnl = s.get("total_pnl", 0.0)
        total_risked = s.get("total_risked", 0.0)
        resolved = wins + losses + s.get("voids", 0)
        win_rate = (wins / resolved * 100) if resolved > 0 else 0

        a(f" {BOLD}P&L SUMMARY{RESET}")
        a(f"   Total P&L: {_color_pnl(total_pnl)}  │  "
          f"Trades: {total_trades} (open: {open_count}, resolved: {resolved})  │  "
          f"W/L: {GREEN}{wins}{RESET}/{RED}{losses}{RESET} "
          f"({win_rate:.1f}%)  │  "
          f"Risked: ${total_risked:,.2f}")

        a(self._line())

        # ---- Signal performance ----
        if signal_perf:
            a(f" {BOLD}SIGNAL PERFORMANCE{RESET}")
            a(f"   {'Signal':<16} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'Avg P&L':>10} {'Total P&L':>11}")
            a(f"   {'─'*16} {'─'*7} {'─'*6} {'─'*7} {'─'*10} {'─'*11}")
            for sp in signal_perf:
                a(f"   {sp['signal']:<16} {sp['trades']:>7} {sp['wins']:>6} "
                  f"{sp['win_rate']*100:>6.1f}% "
                  f"{_color_pnl(sp['avg_pnl']):>10} "
                  f"{_color_pnl(sp['total_pnl']):>11}")
            a(self._line())

        # ---- Recent anomalies (with platform badge) ----
        display_anomalies = recent_anomalies[:10]
        if display_anomalies:
            a(f" {BOLD}RECENT ANOMALIES{RESET} (last {len(display_anomalies)})")
            a(f"   {'Plat':>4} {'Time':<20} {'Ticker / Title':<24} {'Side':>4} {'Notional':>10} "
              f"{'Score':>7} {'Sz':>5} {'Dl':>5} {'Pr':>5} {'Vl':>5} {'Trade?':>6}")
            a(f"   {'─'*4} {'─'*20} {'─'*24} {'─'*4} {'─'*10} {'─'*7} {'─'*5} {'─'*5} {'─'*5} {'─'*5} {'─'*6}")
            for an in display_anomalies:
                triggered = f"{GREEN}YES{RESET}" if an.get("triggered_trade") else f"{DIM}no{RESET}"
                platform = an.get("platform", "kalshi")
                # Show title for Polymarket (long conditionIds aren't useful)
                display_name = an.get("market_title", "") or an.get("market_ticker", "")
                a(
                    f"   {_badge(platform):>4} "
                    f"{str(an.get('detected_at',''))[:19]:<20} "
                    f"{_trunc(display_name, 24):<24} "
                    f"{(an.get('trade_side') or '?'):>4} "
                    f"${an.get('notional_usd',0):>9,.2f} "
                    f"{_color_score(an.get('composite_score',0)):>7} "
                    f"{an.get('score_size',0):>5.2f} "
                    f"{an.get('score_deadline',0):>5.2f} "
                    f"{an.get('score_price',0):>5.2f} "
                    f"{an.get('score_volume',0):>5.2f} "
                    f"{triggered:>6}"
                )
            a(self._line())

        # ---- Open paper trades (with platform badge) ----
        display_open = open_trades[:10]
        if display_open:
            a(f" {BOLD}OPEN PAPER TRADES{RESET} ({len(open_trades)} total)")
            a(f"   {'Plat':>4} {'Opened':<20} {'Ticker / Title':<24} {'Side':>4} {'Entry':>7} {'Qty':>8} {'$':>8} {'Signals'}")
            a(f"   {'─'*4} {'─'*20} {'─'*24} {'─'*4} {'─'*7} {'─'*8} {'─'*8} {'─'*20}")
            for pt in display_open:
                sigs = pt.get("signals_json", "{}") or "{}"
                sig_names = ", ".join(json.loads(sigs).keys()) if sigs != "{}" else ""
                platform = pt.get("platform", "kalshi")
                display_name = pt.get("market_title", "") or pt.get("market_ticker", "")
                a(
                    f"   {_badge(platform):>4} "
                    f"{str(pt.get('opened_at',''))[:19]:<20} "
                    f"{_trunc(display_name, 24):<24} "
                    f"{pt.get('side','?'):>4} "
                    f"${pt.get('entry_price',0):>6.4f} "
                    f"{pt.get('quantity',0):>8.2f} "
                    f"${pt.get('notional_usd',0):>7.2f} "
                    f"{_trunc(sig_names, 30)}"
                )
            a(self._line())

        # ---- Resolutions this cycle ----
        if resolutions_this_cycle:
            a(f" {BOLD}RESOLUTIONS THIS CYCLE{RESET}")
            for r in resolutions_this_cycle:
                color = GREEN if r["outcome"] == "won" else RED
                plat_badge = _badge(r.get("platform", "kalshi"))
                display_ticker = _trunc(r["ticker"], 30)
                a(f"   {plat_badge} #{r['trade_id']} {display_ticker} {r['side']} — "
                  f"{color}{r['outcome'].upper()}{RESET}  P&L: {_color_pnl(r['pnl'])}")
            a(self._line())

        # ---- Footer ----
        a(f" {DIM}Threshold: {ANOMALY.trigger_threshold:.2f}  │  "
          f"Weights: size={ANOMALY.weight_size} deadline={ANOMALY.weight_deadline} "
          f"price={ANOMALY.weight_price_move} volume={ANOMALY.weight_volume_spike}  │  "
          f"Press Ctrl+C to stop{RESET}")
        a("")

        # Write all at once to reduce flicker
        sys.stdout.write("\n".join(lines))
        sys.stdout.flush()
