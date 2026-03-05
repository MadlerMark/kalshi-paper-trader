"""
Polymarket API client — public (read-only) endpoints.

Uses two APIs:
  - Gamma API  (gamma-api.polymarket.com) — market metadata, events
  - Data API   (data-api.polymarket.com)  — trade activity

No authentication is required for read-only access.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .config import POLYMARKET_GAMMA_URL, POLYMARKET_DATA_URL, POLYMARKET_TRADES_LIMIT

logger = logging.getLogger(__name__)


class PolymarketClient:
    """Thin wrapper around Polymarket's public REST APIs."""

    def __init__(
        self,
        gamma_url: str = POLYMARKET_GAMMA_URL,
        data_url: str = POLYMARKET_DATA_URL,
    ):
        self.gamma_url = gamma_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        # Rate-limit state
        self._min_request_gap = 0.15  # conservative
        self._last_request_ts = 0.0

    # ---- low-level -------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_request_gap:
            time.sleep(self._min_request_gap - elapsed)

    def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        """Execute a GET with throttle + retry."""
        url = base + path
        self._throttle()

        for attempt in range(3):
            try:
                resp = self._session.get(url, params=params, timeout=15)
                self._last_request_ts = time.monotonic()

                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 3))
                    logger.warning("Polymarket rate limited — sleeping %.1fs", wait)
                    time.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    logger.warning("Polymarket server error %d, retrying…", resp.status_code)
                    time.sleep(2 ** attempt)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.RequestException as exc:
                logger.warning("Polymarket request failed (attempt %d): %s", attempt + 1, exc)
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

        raise RuntimeError(f"Polymarket request to {path} failed after 3 attempts")

    # ---- Data API: trades ------------------------------------------------

    def get_recent_trades(
        self,
        limit: int = POLYMARKET_TRADES_LIMIT,
        offset: int = 0,
    ) -> list[dict]:
        """
        Fetch recent trades from the Data API.

        Returns list of trade dicts sorted by recency.
        Each trade has: proxyWallet, side, asset, conditionId, size (USDC),
        price, timestamp (Unix), title, slug, eventSlug, outcome, outcomeIndex,
        name, pseudonym, transactionHash.
        """
        params: dict[str, Any] = {"limit": limit}
        if offset:
            params["offset"] = offset
        result = self._get(self.data_url, "/trades", params=params)
        return result if isinstance(result, list) else []

    # ---- Gamma API: markets ----------------------------------------------

    def get_markets(
        self,
        *,
        limit: int = 100,
        closed: bool | None = None,
        order: str = "volume24hr",
        ascending: bool = False,
        offset: int = 0,
        condition_ids: list[str] | None = None,
    ) -> list[dict]:
        """
        Fetch markets from the Gamma API.

        Each market has: id, question, conditionId, slug, endDate, outcomes,
        outcomePrices, volume, volume24hr, liquidityNum, active, closed, etc.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if closed is not None:
            params["closed"] = str(closed).lower()
        if offset:
            params["offset"] = offset
        result = self._get(self.gamma_url, "/markets", params=params)
        return result if isinstance(result, list) else []

    def get_market_by_slug(self, slug: str) -> dict | None:
        """Fetch a single market by its slug (reliable across APIs)."""
        if not slug:
            return None
        params = {"slug": slug}
        result = self._get(self.gamma_url, "/markets", params=params)
        if isinstance(result, list) and result:
            return result[0]
        return None

    def get_market_by_clob_token(self, token_id: str) -> dict | None:
        """Fetch a market by a CLOB token ID (the 'asset' field from trades)."""
        if not token_id:
            return None
        params = {"clob_token_ids": token_id}
        result = self._get(self.gamma_url, "/markets", params=params)
        if isinstance(result, list) and result:
            return result[0]
        return None

    def fetch_markets_batch(self, slugs: list[str]) -> dict[str, dict]:
        """
        Fetch multiple markets by slug.

        Returns dict mapping conditionId → market data.
        Uses slug-based lookups (reliable match between Data API and Gamma API).
        """
        result: dict[str, dict] = {}
        seen_slugs: set[str] = set()
        for slug in slugs:
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            m = self.get_market_by_slug(slug)
            if m:
                cid = m.get("conditionId", "")
                if cid:
                    result[cid] = m
                # Also index by slug for fallback lookups
                result[slug] = m
        return result

    # ---- Gamma API: events -----------------------------------------------

    def get_events(
        self,
        *,
        limit: int = 50,
        closed: bool = False,
    ) -> list[dict]:
        """Fetch active events (groups of markets)."""
        params: dict[str, Any] = {
            "limit": limit,
            "closed": str(closed).lower(),
        }
        result = self._get(self.gamma_url, "/events", params=params)
        return result if isinstance(result, list) else []

    # ---- trade normalisation helper --------------------------------------

    @staticmethod
    def normalise_trade(raw_trade: dict, market: dict | None = None) -> dict:
        """
        Convert a raw Polymarket trade into the normalised format consumed by
        the detector.

        Normalised fields (matching Kalshi field names where possible):
          - platform:         "polymarket"
          - trade_id:         transactionHash (unique across Polymarket)
          - ticker:           conditionId (Polymarket's market identifier)
          - market_ticker:    conditionId
          - event_ticker:     eventSlug
          - taker_side:       "yes" or "no" (mapped from BUY/SELL + outcome)
          - yes_price_dollars: price
          - no_price_dollars:  1 - price
          - count_fp:          size / price  (contracts from USDC amount)
          - notional_usd:      size  (already in USDC)
          - created_time:      ISO-8601 from Unix timestamp
          - title:             market question
          - close_time:        from market endDate if available
        """
        from datetime import datetime, timezone

        ts = raw_trade.get("timestamp", 0)
        trade_time = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""

        price = float(raw_trade.get("price", 0) or 0)
        size_usd = float(raw_trade.get("size", 0) or 0)

        # Map Polymarket side (BUY/SELL) + outcome to our yes/no convention
        side_raw = raw_trade.get("side", "BUY").upper()
        outcome_index = int(raw_trade.get("outcomeIndex", 0))
        # outcomeIndex 0 = first outcome ("Yes" typically), 1 = second ("No")
        # BUY of Yes → taker_side = "yes"
        # SELL of Yes → taker_side = "no"
        # BUY of No → taker_side = "no"
        # SELL of No → taker_side = "yes"
        if outcome_index == 0:
            taker_side = "yes" if side_raw == "BUY" else "no"
        else:
            taker_side = "no" if side_raw == "BUY" else "yes"

        count_contracts = size_usd / price if price > 0 else 0

        # Market info
        close_time = ""
        title = raw_trade.get("title", "")
        if market:
            close_time = market.get("endDate", "")
            title = market.get("question", "") or title

        return {
            "platform": "polymarket",
            "trade_id": raw_trade.get("transactionHash", ""),
            "ticker": raw_trade.get("conditionId", ""),
            "market_ticker": raw_trade.get("conditionId", ""),
            "event_ticker": raw_trade.get("eventSlug", ""),
            "taker_side": taker_side,
            "yes_price_dollars": price if taker_side == "yes" else (1.0 - price),
            "no_price_dollars": (1.0 - price) if taker_side == "yes" else price,
            "count_fp": count_contracts,
            "notional_usd": size_usd,
            "price": price,
            "created_time": trade_time,
            "title": title,
            "close_time": close_time,
            "count": count_contracts,
            # Polymarket-specific fields for reference
            "_poly_proxy_wallet": raw_trade.get("proxyWallet", ""),
            "_poly_pseudonym": raw_trade.get("pseudonym", ""),
            "_poly_outcome": raw_trade.get("outcome", ""),
            "_poly_slug": raw_trade.get("slug", ""),
        }

    @staticmethod
    def normalise_market(raw_market: dict) -> dict:
        """
        Convert a Polymarket market into a normalised format matching the
        fields that the detector / trader expect from Kalshi markets.
        """
        import json as _json

        # Parse outcome prices
        prices_raw = raw_market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = _json.loads(prices_raw)
            except _json.JSONDecodeError:
                prices = []
        else:
            prices = prices_raw

        yes_price = float(prices[0]) if len(prices) > 0 else 0.0
        no_price = float(prices[1]) if len(prices) > 1 else 0.0

        # Map status
        closed = raw_market.get("closed", False)
        active = raw_market.get("active", True)
        if closed:
            status = "settled"
        elif active:
            status = "active"
        else:
            status = "inactive"

        return {
            "platform": "polymarket",
            "ticker": raw_market.get("conditionId", ""),
            "market_ticker": raw_market.get("conditionId", ""),
            "event_ticker": raw_market.get("events", [{}])[0].get("slug", "")
                if raw_market.get("events") else raw_market.get("slug", ""),
            "title": raw_market.get("question", ""),
            "status": status,
            "close_time": raw_market.get("endDate", ""),
            "yes_bid_dollars": yes_price,
            "yes_ask_dollars": yes_price,
            "last_price_dollars": yes_price,
            "volume": raw_market.get("volumeNum", 0) or raw_market.get("volume", 0),
            "volume_24h": raw_market.get("volume24hr", 0),
            "open_interest": 0,
            "result": "",  # Polymarket doesn't expose 'yes'/'no' result directly
            # Polymarket-specific
            "_poly_condition_id": raw_market.get("conditionId", ""),
            "_poly_slug": raw_market.get("slug", ""),
            "_poly_liquidity": raw_market.get("liquidityNum", 0),
        }
