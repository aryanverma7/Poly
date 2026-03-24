"""FastAPI app: start/stop strategy, get state (balance, P&L, mode), equity/trades for frontend."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import Config
from trader import get_runner, start_runner, stop_runner

logger = logging.getLogger(__name__)


class StartRequest(BaseModel):
    mode: str = "paper"  # "paper" | "live"


class ModeResponse(BaseModel):
    mode: str
    is_testing: bool


class StrategyStateItem(BaseModel):
    """Per-strategy state for comparison view."""
    id: str = ""
    label: str = ""
    balance: float = 0.0
    invested_amount: float = 0.0
    session_profit: float = 0.0
    total_profit: float = 0.0
    session_trade_count: int = 0
    trade_count: int = 0
    equity_curve: list[list] = []
    trades: list = []
    session_start: str | None = None
    last_error: str | None = None
    max_trades_per_window: int = 1
    last_rejection_reason: str = ""
    consecutive_losses: int = 0
    cooldown_windows_remaining: int = 0
    stake_usd: float = 0.0
    starting_balance: float = 0.0
    roi_pct: float = 0.0


class StateResponse(BaseModel):
    running: bool
    mode: str
    balance: float
    invested_amount: float
    session_profit: float
    total_profit: float
    session_trade_count: int
    trade_count: int
    equity_curve: list[list]
    trades: list
    session_start: str | None
    last_error: str | None
    status_message: str
    last_poll_at: str | None
    event_slug: str = ""
    event_title: str = ""
    strategies: list[StrategyStateItem] = []
    outcome_prices: dict = {}
    trades_log_dir: str = ""
    trades_log_files: dict = {}
    external_data_enabled: bool = False
    external_data_last_ws_at: str | None = None
    main_loop_cycle_ms_avg: float = 0.0
    main_loop_cycle_ms_last: float = 0.0
    clob_ws_connected: bool = False
    clob_last_update_age_sec: float | None = None
    clob_last_error_msg: str | None = None
    urgent_wake_count_60s: int = 0
    last_rest_fetches: int = 0
    external_snapshot: dict = {}

class PaginatedResponse(BaseModel):
    id: str
    total: int
    offset: int
    limit: int
    items: list


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    stop_runner()


app = FastAPI(title="Polymarket Strategy API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Health check at root (avoids some proxies/static quirks on /api/* GET)."""
    return {"ok": True, "message": "pong"}


@app.get("/api/ping")
def ping():
    """Health check under /api (same as /health)."""
    return {"ok": True, "message": "pong"}


@app.get("/api/mode")
def get_mode() -> ModeResponse:
    """Current trading mode (paper = testing, live = real money)."""
    cfg = Config.from_env()
    mode = cfg.trading_mode
    return ModeResponse(mode=mode, is_testing=(mode == "paper"))


@app.post("/api/strategy/start")
def strategy_start(body: Optional[StartRequest] = None):
    """Start the strategy. Default mode is paper (testing)."""
    mode = ((body and body.mode) or "paper").lower()
    if mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    try:
        start_runner(mode=mode)
    except Exception as e:
        logger.exception("Failed to start strategy")
        raise HTTPException(500, f"Failed to start: {e!s}")
    return {"ok": True, "mode": mode}


@app.post("/api/strategy/stop")
def strategy_stop():
    """Stop the strategy."""
    stop_runner()
    return {"ok": True}


@app.get("/api/state")
def get_state() -> StateResponse:
    """Full state for frontend: balance, P&L, equity curve, trades."""
    runner = get_runner()
    if not runner:
        cfg = Config.from_env()
        return StateResponse(
            running=False,
            mode=cfg.trading_mode,
            balance=cfg.paper_starting_balance if cfg.is_paper else 0.0,
            invested_amount=0.0,
            session_profit=0.0,
            total_profit=0.0,
            session_trade_count=0,
            trade_count=0,
            equity_curve=[],
            trades=[],
            session_start=None,
            last_error=None,
            status_message="Stopped",
            last_poll_at=None,
            event_slug="",
            event_title="",
            strategies=[],
            outcome_prices={},
            trades_log_dir=str(Path(__file__).parent.resolve()),
            trades_log_files={
                "threshold": str((Path(__file__).parent / "trades_log_threshold.md").resolve()),
                "sma": str((Path(__file__).parent / "trades_log_sma.md").resolve()),
            },
            external_data_enabled=cfg.enable_external_data,
            external_data_last_ws_at=None,
        )
    s = runner.get_state()
    strategies_out = [
        StrategyStateItem(
            id=st.get("id", ""),
            label=st.get("label", ""),
            balance=st.get("balance", 0),
            invested_amount=st.get("invested_amount", 0),
            session_profit=st.get("session_profit", 0),
            total_profit=st.get("total_profit", 0),
            session_trade_count=st.get("session_trade_count", 0),
            trade_count=st.get("trade_count", 0),
            equity_curve=st.get("equity_curve", []),
            trades=st.get("trades", []),
            session_start=st.get("session_start"),
            last_error=st.get("last_error"),
            max_trades_per_window=st.get("max_trades_per_window", 1),
            last_rejection_reason=st.get("last_rejection_reason", ""),
            consecutive_losses=st.get("consecutive_losses", 0),
            cooldown_windows_remaining=st.get("cooldown_windows_remaining", 0),
            stake_usd=st.get("stake_usd", 0.0),
            starting_balance=st.get("starting_balance", 0.0),
            roi_pct=st.get("roi_pct", 0.0),
        )
        for st in s.get("strategies", [])
    ]
    return StateResponse(
        running=s["running"],
        mode=s["mode"],
        balance=s["balance"],
        invested_amount=s.get("invested_amount", 0.0),
        session_profit=s["session_profit"],
        total_profit=s["total_profit"],
        session_trade_count=s["session_trade_count"],
        trade_count=s["trade_count"],
        equity_curve=s["equity_curve"],
        trades=s["trades"],
        session_start=s["session_start"],
        last_error=s["last_error"],
        status_message=s.get("status_message", ""),
        last_poll_at=s.get("last_poll_at"),
        event_slug=s.get("event_slug", ""),
        event_title=s.get("event_title", ""),
        strategies=strategies_out,
        outcome_prices=s.get("outcome_prices") or {},
        trades_log_dir=s.get("trades_log_dir") or str(Path(__file__).parent.resolve()),
        trades_log_files=s.get("trades_log_files") or {},
        external_data_enabled=bool(s.get("external_data_enabled")),
        external_data_last_ws_at=s.get("external_data_last_ws_at"),
        main_loop_cycle_ms_avg=float(s.get("main_loop_cycle_ms_avg", 0.0)),
        main_loop_cycle_ms_last=float(s.get("main_loop_cycle_ms_last", 0.0)),
        clob_ws_connected=bool(s.get("clob_ws_connected", False)),
        clob_last_update_age_sec=s.get("clob_last_update_age_sec"),
        clob_last_error_msg=s.get("clob_last_error_msg"),
        urgent_wake_count_60s=int(s.get("urgent_wake_count_60s", 0)),
        last_rest_fetches=int(s.get("last_rest_fetches", 0)),
        external_snapshot=s.get("external_snapshot") or {},
    )

@app.get("/api/strategy/{strategy_id}/trades")
def get_strategy_trades(
    strategy_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=10000),
) -> PaginatedResponse:
    runner = get_runner()
    if not runner:
        return PaginatedResponse(id=strategy_id, total=0, offset=offset, limit=limit, items=[])
    data = runner.get_strategy_trades(strategy_id=strategy_id, offset=offset, limit=limit)
    return PaginatedResponse(**data)


@app.get("/api/strategy/{strategy_id}/roundtrips")
def get_strategy_roundtrips(
    strategy_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=10000),
) -> PaginatedResponse:
    runner = get_runner()
    if not runner:
        return PaginatedResponse(id=strategy_id, total=0, offset=offset, limit=limit, items=[])
    data = runner.get_strategy_roundtrips(strategy_id=strategy_id, offset=offset, limit=limit)
    return PaginatedResponse(**data)


_outcome_prices_event_cache: dict = {"ev": None, "ts": 0.0}

@app.get("/api/outcome-prices")
def outcome_prices():
    """Live best bid/ask/last for Up & Down on current BTC 5m window (no strategy start required)."""
    import time

    from orderbook import fetch_book
    from trader import initialize_strategy

    cfg = Config.from_env()
    now = time.time()
    ev = _outcome_prices_event_cache.get("ev")
    if ev is None or now - float(_outcome_prices_event_cache.get("ts") or 0) > 8.0:
        ev, msg = initialize_strategy(cfg)
        if not ev:
            return {"ok": False, "message": msg, "slug": None, "outcomes": {}}
        _outcome_prices_event_cache["ev"] = ev
        _outcome_prices_event_cache["ts"] = now
    outcomes = {}
    for m in ev.markets:
        ob = fetch_book(m.token_id, cfg.clob_api_base)
        if ob:
            outcomes[m.outcome] = {
                "best_ask": ob.best_ask,
                "best_bid": ob.best_bid,
                "last_trade": ob.last_trade_price,
            }
    return {"ok": True, "slug": ev.slug, "title": ev.title, "outcomes": outcomes}


@app.get("/api/safe-mode")
def safe_mode_get():
    """Return current safe-mode state and which strategies it affects."""
    from strategies.advanced import get_safe_mode, SAFE_MODE_STRATEGIES
    return {"safe_mode": get_safe_mode(), "strategies": sorted(SAFE_MODE_STRATEGIES)}


class SafeModeRequest(BaseModel):
    enabled: bool


@app.post("/api/safe-mode")
def safe_mode_set(body: SafeModeRequest):
    """Enable or disable the minimum-ask floor across all strategies."""
    from strategies.advanced import set_safe_mode, get_safe_mode, SAFE_MODE_STRATEGIES
    set_safe_mode(body.enabled)
    logger.info("Safe mode set to %s", body.enabled)
    return {"safe_mode": get_safe_mode(), "strategies": sorted(SAFE_MODE_STRATEGIES)}


@app.get("/api/verify-window")
def verify_window(slug: str = Query(...)):
    """
    Proxy: resolve the true winner for a 5-min window slug.
    The verifier calls this locally so it doesn't need direct internet access.
    """
    import json as _json
    from orderbook import _http_session, fetch_book

    cfg = Config.from_env()
    result: dict = {"slug": slug, "winner": None, "method": None,
                    "token_map": {}, "books": {}, "error": None}

    # Pass 1 — Gamma API winner field
    try:
        r = _http_session.get(
            f"{cfg.gamma_api_base.rstrip('/')}/events/slug/{slug}", timeout=12
        )
        if r.status_code == 200:
            ev = r.json()
            token_map: dict[str, str] = {}
            gamma_winner: str | None = None
            for market in (ev.get("markets") or []):
                raw = market.get("clobTokenIds") or market.get("tokens") or []
                if isinstance(raw, str):
                    try:
                        raw = _json.loads(raw)
                    except Exception:
                        raw = []
                outcomes = market.get("outcomes") or ["Up", "Down"]
                if isinstance(outcomes, str):
                    try:
                        outcomes = _json.loads(outcomes)
                    except Exception:
                        outcomes = ["Up", "Down"]
                for i, item in enumerate(raw[:2]):
                    tid = item.get("token_id") if isinstance(item, dict) else str(item)
                    out = outcomes[i] if i < len(outcomes) else ("Up" if i == 0 else "Down")
                    if tid:
                        token_map[out] = tid
                # Gamma often sets resolved outcomes through outcomePrices ["1","0"]
                # while `winner` remains null. Use this as primary resolved signal.
                op = market.get("outcomePrices")
                prices = None
                if isinstance(op, str):
                    try:
                        prices = _json.loads(op)
                    except Exception:
                        prices = None
                elif isinstance(op, list):
                    prices = op
                if isinstance(prices, list) and len(prices) >= 2:
                    try:
                        p0 = float(prices[0])
                        p1 = float(prices[1])
                        if p0 >= 0.99 and p1 <= 0.01:
                            gamma_winner = outcomes[0] if len(outcomes) > 0 else "Up"
                        elif p1 >= 0.99 and p0 <= 0.01:
                            gamma_winner = outcomes[1] if len(outcomes) > 1 else "Down"
                    except (TypeError, ValueError):
                        pass
                w = market.get("winner")
                if w and not gamma_winner:
                    if w in token_map:
                        gamma_winner = w
                    else:
                        for out, tid in token_map.items():
                            if tid == w:
                                gamma_winner = out
                                break
            result["token_map"] = token_map
            if gamma_winner:
                result.update(winner=gamma_winner, method="gamma")
                return result
        else:
            result["error"] = f"Gamma HTTP {r.status_code}"
    except Exception as e:
        result["error"] = str(e)

    # Pass 2 — CLOB books
    token_map = result.get("token_map") or {}
    if not token_map:
        return result
    books: dict = {}
    for out, tid in token_map.items():
        book = fetch_book(tid, cfg.clob_api_base)
        if book:
            books[out] = {
                "last_trade": book.last_trade_price,
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
            }
    result["books"] = books
    # Strict: last_trade >= 0.95 AND ask collapsed
    for out, b in books.items():
        lt, ask = b.get("last_trade"), b.get("best_ask")
        if lt is not None and lt >= 0.95 and (ask is None or ask <= 0.05):
            result.update(winner=out, method="clob_strict")
            return result
    # Loose: last_trade >= 0.95 only
    for out, b in books.items():
        lt = b.get("last_trade")
        if lt is not None and lt >= 0.95:
            result.update(winner=out, method="clob_loose")
            return result

    result["error"] = (result.get("error") or "") + " | no winner found in CLOB"
    return result


@app.get("/api/init-check")
def init_check():
    """Resolve BTC 5m event without starting (for UI before Start)."""
    from trader import initialize_strategy
    ev, msg = initialize_strategy()
    return {
        "ok": ev is not None,
        "message": msg,
        "slug": ev.slug if ev else None,
        "title": ev.title if ev else None,
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Avoid 404 in browser console when no favicon is present."""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/")
def root():
    return RedirectResponse(url="/app/")


# Serve React production build at /app
frontend_dir = Path(__file__).parent / "web" / "dist"
if frontend_dir.exists():
    app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
