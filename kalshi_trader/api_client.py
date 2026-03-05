"""
Kalshi v2 REST API client with RSA-PSS HMAC authentication.

Only the *public* and *read-only* endpoints we need are implemented.
No orders are ever placed — this is a paper-trading observer only.
"""

from __future__ import annotations

import base64
import datetime
import logging
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import API_BASE_URL, API_KEY_ID, KEY_FILE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RSA helpers
# ---------------------------------------------------------------------------

def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file."""
    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(
            fh.read(),
            password=None,
            backend=default_backend(),
        )


def sign_request(private_key: rsa.RSAPrivateKey, timestamp_ms: str, method: str, path: str) -> str:
    """
    Create the KALSHI-ACCESS-SIGNATURE header value.

    Signature = RSA-PSS(SHA-256)( timestamp_ms + METHOD + path_without_query )
    """
    path_no_query = path.split("?")[0]
    message = (timestamp_ms + method + path_no_query).encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KalshiClient:
    """Thin wrapper around the Kalshi v2 REST API."""

    def __init__(
        self,
        base_url: str = API_BASE_URL,
        api_key_id: str = API_KEY_ID,
        key_file: Path = KEY_FILE,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key: rsa.RSAPrivateKey | None = None
        self._key_file = key_file
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        # Rate-limit state (simple token bucket)
        self._min_request_gap = 0.12  # ~8 req/s conservative
        self._last_request_ts = 0.0

    # ---- auth helpers ----------------------------------------------------

    @property
    def _has_auth(self) -> bool:
        return bool(self.api_key_id) and self._key_file.exists()

    def _ensure_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            self._private_key = load_private_key(self._key_file)
        return self._private_key

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build authentication headers for a request."""
        if not self._has_auth:
            return {}
        key = self._ensure_key()
        ts_ms = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
        sig = sign_request(key, ts_ms, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        }

    # ---- low-level request -----------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_request_gap:
            time.sleep(self._min_request_gap - elapsed)

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        """Execute an API request with auth + throttle + retry."""
        url = self.base_url + path
        self._throttle()

        # The Kalshi signature must cover the full URL path (e.g.
        # /trade-api/v2/markets/trades), not just the relative path
        # (/markets/trades).  Extract it from the assembled URL.
        from urllib.parse import urlparse
        full_path = urlparse(url).path  # e.g. /trade-api/v2/markets/trades
        headers = self._auth_headers(method.upper(), full_path)
        for attempt in range(3):
            try:
                resp = self._session.request(
                    method, url, params=params, headers=headers, timeout=15
                )
                self._last_request_ts = time.monotonic()

                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 2))
                    logger.warning("Rate limited — sleeping %.1fs", wait)
                    time.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    logger.warning("Server error %d, retrying…", resp.status_code)
                    time.sleep(2 ** attempt)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.RequestException as exc:
                logger.warning("Request failed (attempt %d): %s", attempt + 1, exc)
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

        raise RuntimeError(f"Request to {path} failed after 3 attempts")

    # ---- public / read-only endpoints ------------------------------------

    def get_markets(
        self,
        *,
        status: str | None = "open",
        limit: int = 200,
        cursor: str = "",
        event_ticker: str | None = None,
        tickers: str | None = None,
    ) -> dict:
        """GET /markets — paginated list of markets."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if tickers:
            params["tickers"] = tickers
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        """GET /markets/{ticker} — single market detail."""
        return self._request("GET", f"/markets/{ticker}")

    def get_trades(
        self,
        *,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 1000,
        cursor: str = "",
    ) -> dict:
        """GET /markets/trades — public trade tape."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets/trades", params=params)

    def get_market_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """GET /markets/{ticker}/orderbook"""
        return self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # ---- convenience iterators -------------------------------------------

    def fetch_markets_batch(self, ticker_list: list[str], max_url_chars: int = 4000) -> dict[str, dict]:
        """
        Fetch multiple markets by ticker using the comma-separated tickers param.

        Batches by URL length (max_url_chars for the tickers value) to avoid
        414 Request-URI Too Large errors.  Returns dict mapping ticker → market data.
        """
        result: dict[str, dict] = {}
        # Build length-aware batches
        batches: list[list[str]] = []
        current_batch: list[str] = []
        current_len = 0
        for t in ticker_list:
            added = len(t) + (1 if current_batch else 0)   # +1 for comma
            if current_len + added > max_url_chars and current_batch:
                batches.append(current_batch)
                current_batch = [t]
                current_len = len(t)
            else:
                current_batch.append(t)
                current_len += added
        if current_batch:
            batches.append(current_batch)

        for batch in batches:
            tickers_csv = ",".join(batch)
            cursor = ""
            while True:
                data = self.get_markets(
                    status=None, tickers=tickers_csv, limit=1000, cursor=cursor
                )
                for m in data.get("markets", []):
                    result[m["ticker"]] = m
                cursor = data.get("cursor", "")
                if not cursor:
                    break
        return result

    def iter_all_markets(self, status: str = "open", limit: int = 200):
        """Yield all markets across pages."""
        cursor = ""
        while True:
            data = self.get_markets(status=status, limit=limit, cursor=cursor)
            for m in data.get("markets", []):
                yield m
            cursor = data.get("cursor", "")
            if not cursor:
                break

    def iter_recent_trades(
        self,
        *,
        ticker: str | None = None,
        since_ts: int | None = None,
        limit: int = 1000,
    ):
        """Yield trades, newest first, optionally filtered."""
        cursor = ""
        while True:
            data = self.get_trades(
                ticker=ticker, min_ts=since_ts, limit=limit, cursor=cursor
            )
            for t in data.get("trades", []):
                yield t
            cursor = data.get("cursor", "")
            if not cursor:
                break
