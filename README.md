# Polymarket Bitcoin 5-Minute Strategy Bot

Strategy:
- **Threshold lane**: In the first 2 minutes of each "Bitcoin Up or Down - 5 Minutes" window, buy whichever outcome (Up or Down) hits 25¢ first, then place a limit sell at 40¢.
- **Short MA lane**: In the same entry window, buy once when current ask dips below a short moving average by a configurable discount, then place the same 40¢ limit sell.
- **Advanced lanes (paper A/B)**:
  - ATR-scaled guard
  - Order-book imbalance trigger
  - Layered entry (passive-first approximation)
  - Adaptive exit (target ladder)
  - MA + orderflow fusion
  - End-window momentum
  - Mean-reversion extreme
  - Oracle-lag arbitrage proxy
  - Cross-market sentiment proxy
  - Micro market-making proxy
- Both lanes skip entries if BTC has already moved more than $100 from the window's reference price.

## Modes

- **Testing (paper)**: Default. No real money; simulated balance and fills. Use this to validate the strategy.
- **Live**: Real CLOB orders. Requires Polymarket API credentials.

## Setup

1. **Python 3.10+**
2. Copy `.env.example` to `.env` and optionally set:
   - `TRADING_MODE=paper` (default) or `live`
   - `PAPER_STARTING_BALANCE=1000`
   - Threshold knobs: `BUY_THRESHOLD_CENTS`, `BUY_AMOUNT_USD`, `SELL_LIMIT_CENTS`
   - SMA knobs: `SMA_WINDOW_TICKS`, `SMA_DISCOUNT_CENTS`, `SMA_MAX_ENTRY_CENTS`
   - External-feed flags (for strategies marked proxy/arb/sentiment/MM):  
     `ENABLE_EXTERNAL_DATA`, `ENABLE_BINANCE_WS`, `ENABLE_BINANCE_FUNDING`, `ENABLE_BINANCE_OPEN_INTEREST`, `ENABLE_BINANCE_DEPTH`
   - For live: `PRIVATE_KEY`, and optionally `POLYMARKET_API_KEY`, `POLYMARKET_SECRET`, `POLYMARKET_PASSPHRASE` (or derive from private key per [Polymarket auth](https://docs.polymarket.com/developers/CLOB/authentication))

3. Install and run:

```bash
pip install -r requirements.txt
python main.py
```

Backend runs at http://localhost:8000. Open the frontend (see below).

## Single command (backend + frontend)

**Option A — one window (dev):**  
Install frontend deps once (from **cmd.exe** if PowerShell blocks npm):

```bash
cd web
npm install
cd ..
```

Then start both:

```bash
python run.py
```

Or on Windows: **`start.bat`** (double‑click or `cmd /c start.bat`).  
Backend: http://127.0.0.1:8000 · Frontend: http://127.0.0.1:5173. Press Ctrl+C to stop both.

**Option B — backend only (production build):**  
Build React once, then a single command runs everything:

```bash
cd web && npm run build && cd ..
python main.py
```

Open **http://localhost:8000/app/** — FastAPI serves the built React app from `web/dist`. No separate frontend process.

## Frontend (React — recommended)

1. Start the API: `python main.py` (port **8000**).
2. In another terminal:

```bash
cd web
npm install
npm run dev
```

Open **http://127.0.0.1:5173** — Vite proxies `/api` to the backend. The UI **resolves the BTC 5m market before Start** (`/api/init-check`) so you see the correct Polymarket window without extra loop latency.

## API

- `GET /api/mode` — current mode (paper/live) and `is_testing`
- `POST /api/strategy/start` — body `{ "mode": "paper" }` or `"live"`. Start the strategy.
- `POST /api/strategy/stop` — stop the strategy.
- `GET /api/state` — balance, session/total P&L, equity curve, trades, running, last_error.

## Trade log files (on disk)

While the strategy is running, **every new fill is appended** to Markdown files in the **project root** (same folder as `trader.py`):

| File | When |
|------|------|
| `trades_log_threshold.md` | Paper threshold lane |
| `trades_log_sma.md` | Paper short MA lane |
| `trades_log_*.md` | Other paper strategy lanes (filename matches strategy suffix in UI/API) |
| `trades_log_live.md` | Live mode |

Files are created on **first trade** of a session. Columns include **Time, Side, Outcome (Up/Down), Price, Size, USD**.

> Note: with `ENABLE_EXTERNAL_DATA=true`, the arb/sentiment/MM proxy lanes automatically switch to live Binance-backed signals (trade websocket + funding/open-interest/depth REST), with safe fallback behavior if feeds are unavailable.

## Why a Python backend? (Backend vs doing everything in React)

| | Python backend | All in React (frontend-only) |
|---|----------------|------------------------------|
| **Live trading** | Private key and API secrets stay on the server; orders are signed safely. | Putting the key in the browser is unsafe (anyone can read it). |
| **Always-on** | Strategy loop runs even when the tab is closed. | Strategy stops when you close the tab. |
| **CORS** | Server calls Polymarket; no browser CORS issues. | Polymarket APIs may block or limit requests from your domain. |
| **Paper / testing** | Same code path; just swap executor. | Could be done in React (public APIs only), but you’d duplicate logic and still need a backend for live. |

**Summary:** For **live trading** you need a backend (or similar secure environment) to hold credentials and sign orders. For **paper-only** you could move more logic into React, but you’d lose “run in background” and risk CORS. Keeping the backend gives one codebase for both paper and live, always-on execution, and no secrets in the client.

## Project layout

- `config.py` — env config (mode, thresholds, API bases)
- `discovery.py` — Gamma API: fetch current BTC 5m event and token IDs
- `orderbook.py` — CLOB book + BTC price (CoinGecko for guard)
- `executor.py` — PaperExecutor (simulated) and LiveExecutor (real CLOB)
- `strategies/base.py` — strategy interface; `strategies/btc_5m.py` — this strategy
- `paper_engine.py` — reusable harness for testing any strategy
- `trader.py` — orchestration loop (discover → books → strategy tick)
- `api.py` — FastAPI for frontend
- `main.py` — run backend
