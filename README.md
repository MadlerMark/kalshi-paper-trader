# Prediction Market Paper Trader

A long-running Python process that monitors **Kalshi** and **Polymarket** prediction markets for suspicious trading patterns, mirrors them as paper trades, and tracks outcomes over time. Built for research into market integrity — the findings from this tool are intended for publication.

## What it does

1. **Polls two prediction-market platforms** in parallel:
   - **Kalshi** — v2 REST API with RSA-PSS authentication
   - **Polymarket** — public Gamma and Data APIs (no auth required)
2. **Scores each trade** against four anomaly signals:
   - **Size** — unusually large notional value ($10 k+ flagged, $1 M+ max weight)
   - **Deadline proximity** — trades placed within 60 minutes of market close
   - **Price movement** — abnormal price change relative to recent snapshots
   - **Volume spike** — current volume exceeding historical baseline by 5×+
3. **Computes a composite suspicion score** (weighted sum, 0–1 scale)
4. **Opens a paper trade** mirroring the suspicious side when the score exceeds the threshold
5. **Resolves paper trades** automatically when markets settle
6. **Persists everything** to SQLite — anomalies, trades, snapshots, P&L, tagged by platform

The system is designed to run for months, accumulating a research dataset of every anomaly detected and every paper trade outcome, including false positives and losing strategies.

### Calibration reference

The detector is calibrated against the well-documented case of a **$553 000 purchase on "Will the US strike Iran?"** placed roughly **one hour before the strike occurred**. Under the current weights and threshold this trade scores **≈ 0.475**, comfortably above the 0.35 trigger threshold. The goal is a system that catches trades of that magnitude and timing while avoiding excessive false positives on routine large-but-legitimate orders.

## Quick start

```bash
# Clone and install
git clone <repo-url> && cd kalshi-paper-trader
pip install -e .

# Configure
cp .env.example .env
# Edit .env — see Configuration section below

# Run (monitors both platforms by default)
python -m kalshi_trader
```

## Configuration

All settings are controlled via environment variables or a `.env` file. See [.env.example](.env.example) for the full list.

### Platform toggles

| Variable | Default | Description |
|---|---|---|
| `KALSHI_ENABLED` | `true` | Enable Kalshi monitoring |
| `POLYMARKET_ENABLED` | `true` | Enable Polymarket monitoring |

### Kalshi settings

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | — | Your Kalshi API key ID |
| `KALSHI_KEY_FILE` | `./kalshi.key` | Path to RSA private key PEM |
| `KALSHI_API_BASE_URL` | `https://trading-api.kalshi.com/trade-api/v2` | API base URL |

### Polymarket settings

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_GAMMA_URL` | `https://gamma-api.polymarket.com` | Gamma API base URL (markets) |
| `POLYMARKET_DATA_URL` | `https://data-api.polymarket.com` | Data API base URL (trades) |
| `POLYMARKET_TRADES_LIMIT` | `200` | Max trades to fetch per cycle |

### Detection & trading

| Variable | Default | Description |
|---|---|---|
| `KALSHI_POLL_INTERVAL` | `60` | Seconds between scan cycles |
| `KALSHI_TRADES_LOOKBACK` | `300` | Seconds of trade history to scan per cycle |
| `KALSHI_TRIGGER_THRESHOLD` | `0.35` | Composite score threshold to open a paper trade |
| `KALSHI_LARGE_ORDER_USD` | `10000` | Dollar threshold for size signal |
| `KALSHI_MEGA_ORDER_USD` | `1000000` | Maximum-weight size threshold |
| `KALSHI_DEADLINE_MINUTES` | `60` | Minutes-to-close for deadline signal |
| `KALSHI_PRICE_MOVE_PCT` | `15.0` | Price change % for price signal |
| `KALSHI_VOLUME_SPIKE_X` | `5.0` | Volume multiple for spike signal |
| `KALSHI_MIRROR_FRACTION` | `0.01` | Fraction of suspicious order to mirror |
| `KALSHI_MAX_POSITION_USD` | `100` | Cap per paper position |
| `KALSHI_MIN_POSITION_USD` | `1` | Floor per paper position |

### Demo environment (Kalshi only)

To test without touching production data, use the Kalshi demo API:

```
KALSHI_API_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
```

Polymarket APIs are public and read-only — no demo mode is needed.

## Architecture

```
kalshi_trader/
├── __init__.py
├── __main__.py           # Entry point
├── config.py             # All tunables and thresholds
├── api_client.py         # Kalshi v2 REST client with RSA-PSS auth
├── polymarket_client.py  # Polymarket Gamma + Data API client
├── database.py           # SQLite persistence (anomalies, trades, snapshots)
├── detector.py           # Anomaly scoring engine (4 signals → composite)
├── trader.py             # Paper trade lifecycle (open → resolve → P&L)
├── orchestrator.py       # Main loop — dual-platform polling
├── ui.py                 # Terminal rendering with platform badges
└── report.py             # Offline reporting CLI with --platform filter
```

### Dual-platform polling

Each scan cycle:

1. **Kalshi** — fetches recent trades (trade-first approach, ~3 API calls/cycle), then fetches market data only for markets that appear in those trades.
2. **Polymarket** — fetches recent trades from the Data API, collects unique `conditionId`s, batch-fetches corresponding markets from the Gamma API.
3. **Merge** — all trades and markets are normalised into a common format with a `platform` tag (`"kalshi"` or `"polymarket"`).
4. **Score → Trade → Resolve → Render** — the rest of the pipeline is platform-agnostic.

Polymarket trades are normalised: `size` (USDC) maps to notional, `BUY`/`SELL` + `outcomeIndex` maps to `yes`/`no`, and `conditionId` serves as the market identifier.

### Anomaly scoring

Each trade is scored independently against four signals. Each signal produces a normalised sub-score in [0, 1], then they're combined with configurable weights:

```
composite = 0.35 × size + 0.25 × deadline + 0.20 × price_move + 0.20 × volume_spike
```

A trade triggers a paper position when `composite ≥ KALSHI_TRIGGER_THRESHOLD` (default **0.35**).

The scoring uses sigmoid functions for soft thresholds — there's no hard cutoff on any individual signal. This means a moderately large order placed near a deadline with a small price move can still trigger, even if no single signal is extreme.

**Calibration note:** Weights and threshold were tuned so that:
- A $553 000 trade 60 min before close → composite ≈ 0.475 → **triggers** (true positive)
- A $50 000 trade with no deadline/price/volume signal → composite ≈ 0.22 → **does not trigger**
- A $5 000 trade near deadline with volume spike → composite ≈ 0.30 → **does not trigger** (avoids noise)

### Paper trading rules

- Mirror **1% of the suspicious order's notional**, capped at **$100** per position
- Take the **same side** as the suspicious taker (yes or no)
- Track: entry price, side, market, platform, timestamp, triggering signals
- On market settlement: mark won/lost, record P&L = (exit − entry) × quantity
- **Nothing is pruned** — losing strategies and false positives are kept for the research dataset

### Persistence

Everything writes to a single SQLite database (`kalshi_trader.db` by default):

- **`anomalies`** — every scored trade above the noise floor, with full signal breakdown and platform tag
- **`paper_trades`** — every paper position with entry/exit/P&L, triggering signals, and platform tag
- **`market_snapshots`** — periodic market state for computing baselines, tagged by platform

The database uses schema version 2 with automatic migration from v1. The process is fully restartable. Trade deduplication is by `trade_id`. P&L can be recomputed from the database at any time.

## Reporting

While the main loop is running (or after stopping it), query the database:

```bash
# P&L summary
python -m kalshi_trader.report summary

# Per-signal win rates and P&L
python -m kalshi_trader.report signals

# Recent anomalies (optionally filter by platform)
python -m kalshi_trader.report anomalies -n 100
python -m kalshi_trader.report anomalies -n 100 --platform polymarket

# Paper trade history (optionally filter by platform)
python -m kalshi_trader.report trades -n 50 --platform kalshi

# Export to CSV (optionally filter by platform)
python -m kalshi_trader.report export -o my_trades.csv
python -m kalshi_trader.report export -o polymarket_trades.csv --platform polymarket
```

You can also query the SQLite database directly:

```sql
-- Total P&L by platform
SELECT platform, SUM(pnl) AS total_pnl, COUNT(*) AS trades
FROM paper_trades
WHERE status IN ('won', 'lost')
GROUP BY platform;

-- Win rate by signal
SELECT
    json_each.key AS signal,
    COUNT(*) AS trades,
    SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) AS wins,
    ROUND(AVG(pnl), 4) AS avg_pnl
FROM paper_trades, json_each(signals_json)
WHERE status IN ('won', 'lost')
GROUP BY json_each.key;
```

## Authentication

### Kalshi

The Kalshi v2 API uses RSA-PSS signed requests:

1. Generate an API key at [kalshi.com/account/profile](https://kalshi.com/account/profile)
2. Save the private key PEM file as `kalshi.key` (or wherever `KALSHI_KEY_FILE` points)
3. Set `KALSHI_API_KEY_ID` to your key ID

Each request is signed: `signature = RSA-PSS-SHA256(timestamp_ms + METHOD + path)`, sent in the `KALSHI-ACCESS-SIGNATURE` header.

### Polymarket

No authentication is required. The Gamma API (markets) and Data API (trades) are public read-only endpoints. Rate limiting is handled automatically (150 ms between requests, exponential backoff on 429s).

## License

MIT
