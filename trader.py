"""
Trader: resolve BTC 5m event once at start; loop uses books + strategy.
Re-resolves when the current window ends (no spam during a window).
"""
import logging
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import Config
from discovery import EventInfo, discover_btc_5m_event
from executor import Executor, PaperExecutor, SharedPaperBank, create_executor
from external_data import ExternalDataService
from orderbook import BookLevel, OrderBook, fetch_book, get_btc_price_usd
from paper_engine import StrategyRunState, run_strategy_tick
from strategies.base import MarketData, Strategy
from strategies.advanced import (
    AtrGuardThresholdStrategy,
    CascadeTrendLockStrategy,
    ConfirmedFlatScalperStrategy,
    ConfirmedMomentumCarryStrategy,
    CrossMarketSentimentProxyStrategy,
    EarlyBreakoutStrategy,
    EndWindowMomentumStrategy,
    ExhaustionFadeStrategy,
    FlatMarketMeanReversionStrategy,
    FundingTrendConfirmStrategy,
    HybridEarlyMomentumStrategy,
    LateHighConfidenceStrategy,
    LateFlatBetStrategy,
    MidWindowMomentumStrategy,
    MicroMarketMakingProxyStrategy,
    OpeningDiscountScalperStrategy,
    OracleLagArbProxyStrategy,
    OrderBookImbalanceStrategy,
    PriceSkewFadeStrategy,
    RebalancingArbStrategy,
    SignalFusionStrategy,
    SustainedTrendLockInStrategy,
    VolumeSurgeBreakoutStrategy,
    WindowMomentumCarryClassicStrategy,
    WindowMomentumCarryStrategy,
)
from strategies.btc_5m import Btc5mStrategy
from strategies.btc_5m_sma import Btc5mSmaStrategy

logger = logging.getLogger(__name__)

_runner: Optional["StrategyRunner"] = None
_runner_lock = threading.Lock()


def initialize_strategy(config: Optional[Config] = None) -> tuple[Optional[EventInfo], str]:
    """
    Call before starting the runner. Resolves Polymarket BTC 5m event + token IDs.
    Returns (EventInfo, message). EventInfo is None if resolution failed.
    """
    cfg = config or Config.from_env()
    return discover_btc_5m_event(cfg)


def _lane_tuple(strategy: Strategy, executor: Executor, state: StrategyRunState, label: str, log_suffix: str):
    """One lane: (strategy, executor, state, label, log_suffix)."""
    return (strategy, executor, state, label, log_suffix)


class StrategyRunner:
    def __init__(
        self,
        initial_event: EventInfo,
        config: Config,
        lanes: list[tuple],
    ):
        self.config = config
        self._lanes = lanes
        self.strategy, self.executor, self.state = lanes[0][0], lanes[0][1], lanes[0][2]
        self._event: Optional[EventInfo] = initial_event
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reference_btc: Optional[float] = None
        self._last_local_btc: Optional[float] = None
        self._last_poll_at: Optional[datetime] = None
        self._status_message: str = "Stopped"
        self._event_slug: str = initial_event.slug
        self._written_fill_keys: dict = {}  # log_suffix -> set of (ts, side, outcome, price, size)
        for _s, _e, _st, _label, suffix in lanes:
            self._written_fill_keys.setdefault(suffix, set())
        self._trades_log_dir: Path = Path(__file__).parent
        self._outcome_prices: dict = {}
        self._price_refresh_tick: int = 0
        self._btc_ticks: deque[tuple[datetime, float]] = deque(maxlen=12000)
        self._loop_cycle_ms_last: float = 0.0
        self._loop_cycle_ms_avg: float = 0.0
        self._loop_cycle_ms_samples: int = 0
        self._last_rest_fetches: int = 0
        # Per-lane risk runtime: realized baseline, loss streak and cooldown windows.
        self._lane_runtime: dict[str, dict] = {}
        for _s, ex, _st, _label, suffix in lanes:
            baseline = ex.realize_pnl() if isinstance(ex, PaperExecutor) else 0.0
            base_stake = float(getattr(_s, 'buy_amount_usd', self.config.buy_amount_usd))
            self._lane_runtime[suffix] = {
                "last_window_realized": float(baseline),
                "cooldown_windows_remaining": 0,
                "stake_base_usd": base_stake,
                "stake_usd": base_stake,
                "dynamic_stake_enabled": True,
                "last_confidence": 0.5,
                "stake_max_mult_override": None,
                "last_window_pnl": 0.0,
                "disabled_due_to_loss_cap": False,
                "disabled_reason": "",
                "loss_from_start_pct": 0.0,
            }
        self._external: Optional[ExternalDataService] = None
        if self.config.enable_external_data:
            self._external = ExternalDataService(
                enable_ws=self.config.enable_binance_ws,
                enable_funding=self.config.enable_binance_funding,
                enable_open_interest=self.config.enable_binance_open_interest,
                enable_depth=self.config.enable_binance_depth,
            )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("Runner already running")
            return
        self._stop.clear()
        for _s, _e, st, _label, _ in self._lanes:
            st.running = True
            st.session_start = datetime.utcnow()
        if self._external:
            self._external.start()
        self._status_message = "Running"
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Strategy runner started for %s", self._event_slug)

    def stop(self) -> None:
        self._stop.set()
        for _s, _e, st, _label, _ in self._lanes:
            st.running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._external:
            self._external.stop()
        logger.info("Strategy runner stopped")

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _refresh_outcome_prices(self, event: EventInfo) -> None:
        for m in event.markets:
            ob = fetch_book(m.token_id, self.config.clob_api_base)
            if ob:
                self._outcome_prices[m.outcome] = {
                    "best_ask": ob.best_ask,
                    "best_bid": ob.best_bid,
                    "last_trade": ob.last_trade_price,
                }

    @staticmethod
    def _book_from_cached(token_id: str, raw: dict) -> Optional[OrderBook]:
        if not raw:
            return None
        bid = raw.get("best_bid")
        ask = raw.get("best_ask")
        last = raw.get("last_trade")

        def _parse_levels(raw_levels) -> list[BookLevel]:
            levels = []
            for lvl in (raw_levels or []):
                try:
                    p = float(lvl.get("price", 0))
                    s = float(lvl.get("size", 0))
                    if p > 0 and s > 0:
                        levels.append(BookLevel(price=p, size=s))
                except (TypeError, ValueError, AttributeError):
                    pass
            return levels

        raw_bids = raw.get("bids") or []
        raw_asks = raw.get("asks") or []
        bids = _parse_levels(raw_bids) or ([BookLevel(price=float(bid), size=1.0)] if bid is not None else [])
        asks = _parse_levels(raw_asks) or ([BookLevel(price=float(ask), size=1.0)] if ask is not None else [])

        if not bids and not asks and last is None:
            return None
        try:
            last_trade = float(last) if last is not None else None
        except (TypeError, ValueError):
            last_trade = None
        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            last_trade_price=last_trade,
        )

    def _append_trades_to_log(self, lane: tuple) -> None:
        """Append any new fills for this lane to trades_log_{suffix}.md."""
        _strategy, executor, _state, label, suffix = lane
        written = self._written_fill_keys.get(suffix, set())
        try:
            fills = executor.get_fill_history()
            to_write: list[tuple] = []
            for f in fills:
                oc = getattr(f, "outcome", "") or "—"
                key = (f.ts.isoformat(), f.side, oc, round(f.price, 4), round(f.size, 4))
                if key not in written:
                    written.add(key)
                    self._written_fill_keys[suffix] = written
                    to_write.append((f.ts.isoformat(), f.side, oc, f.price, f.size, f.amount_usd))
            if not to_write:
                return
            path = self._trades_log_dir / f"trades_log_{suffix}.md"
            write_header = not path.exists()
            with open(path, "a", encoding="utf-8") as out:
                if write_header:
                    out.write(
                        f"## Trade log — {label}\n\n"
                        f"*Full append-only history on disk:* `{path.resolve()}`\n\n"
                        f"| Time (UTC) | Side | Outcome | Price | Size | USD |\n| --- | --- | --- | --- | --- | --- |\n"
                    )
                for ts, side, oc, price, size, amount_usd in to_write:
                    out.write(f"| {ts} | {side} | {oc} | {price:.2f} | {size:.2f} | {amount_usd:.2f} |\n")
        except Exception as e:
            logger.warning("Failed to write trades log: %s", e)

    def _window_ended(self) -> bool:
        if not self._event:
            return True
        now = self._now_utc()
        # Prefer slug-based end (start_ts + 300s) so we don't switch window too early
        m = re.match(r"^btc-updown-5m-(\d+)$", self._event.slug)
        if m:
            try:
                end_ts = int(m.group(1)) + 300
                end = datetime.fromtimestamp(end_ts, tz=timezone.utc).replace(tzinfo=None)
                return now >= end
            except (ValueError, OSError):
                pass
        if self._event.end_date:
            return now >= self._event.end_date
        return False

    def _record_btc_tick(self, now: datetime, price: Optional[float]) -> None:
        if price is None:
            return
        self._btc_ticks.append((now, float(price)))
        cutoff = now.timestamp() - 15 * 60
        while self._btc_ticks and self._btc_ticks[0][0].timestamp() < cutoff:
            self._btc_ticks.popleft()

    def _btc_move_30s(self, now: datetime, current_price: Optional[float]) -> Optional[float]:
        if current_price is None or not self._btc_ticks:
            return None
        target = now.timestamp() - 30.0
        base = None
        for ts, p in self._btc_ticks:
            if ts.timestamp() <= target:
                base = p
            else:
                break
        if base is None:
            return None
        return float(current_price) - float(base)

    def _atr_1m_10m(self, now: datetime) -> Optional[float]:
        if len(self._btc_ticks) < 10:
            return None
        cutoff = now.timestamp() - 11 * 60
        ticks = [(t, p) for (t, p) in self._btc_ticks if t.timestamp() >= cutoff]
        if len(ticks) < 10:
            return None
        buckets: dict[int, dict] = {}
        for ts, px in ticks:
            m = int(ts.timestamp() // 60)
            if m not in buckets:
                buckets[m] = {"high": px, "low": px, "close": px}
            else:
                b = buckets[m]
                b["high"] = max(b["high"], px)
                b["low"] = min(b["low"], px)
                b["close"] = px
        mins = sorted(buckets.keys())
        if len(mins) < 2:
            return None
        mins = mins[-11:]
        trs: list[float] = []
        prev_close = buckets[mins[0]]["close"]
        for m in mins[1:]:
            h = buckets[m]["high"]
            l = buckets[m]["low"]
            c = buckets[m]["close"]
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(float(tr))
            prev_close = c
        if not trs:
            return None
        return sum(trs) / len(trs)

    def _loop(self) -> None:
        poll_active = 0.5
        poll_wait_window = 5.0

        while not self._stop.is_set():
            cycle_t0 = time.perf_counter()
            rest_fetches = 0
            try:
                self._last_poll_at = datetime.utcnow()

                if self._window_ended() or not self._event:
                    old_token_map = {m.outcome: m.token_id for m in self._event.markets} if self._event else {}

                    resolve_prices: dict[str, float] = {}
                    if old_token_map:
                        end_chainlink = get_btc_price_usd()
                        ref_btc = self._reference_btc
                        if end_chainlink is not None and ref_btc is not None:
                            winner = "Up" if end_chainlink >= ref_btc else "Down"
                            logger.info(
                                "Chainlink resolution: ref=%.2f  end=%.2f  winner=%s",
                                ref_btc, end_chainlink, winner,
                            )
                            for outcome, tid in old_token_map.items():
                                resolve_prices[tid] = 1.0 if outcome == winner else 0.0
                        else:
                            logger.warning(
                                "Resolution unavailable (ref_btc=%s, chainlink=%s) — defaulting to loss",
                                ref_btc, end_chainlink,
                            )
                            for outcome, tid in old_token_map.items():
                                resolve_prices[tid] = 0.0

                    self._status_message = "Resolving next 5m window..."
                    ev, msg = discover_btc_5m_event(self.config)
                    if not ev:
                        self._status_message = msg[:120] if msg else "Waiting for next event..."
                        time.sleep(poll_wait_window)
                        continue
                    for _s, executor, _st, _label, suffix in self._lanes:
                        if isinstance(executor, PaperExecutor) and old_token_map:
                            executor.settle_unfilled_at_window_end(
                                list(old_token_map.values()),
                                resolve_prices=resolve_prices or None,
                            )
                        rt = self._lane_runtime.get(suffix)
                        if not rt:
                            continue
                        # One window has elapsed.
                        if rt["cooldown_windows_remaining"] > 0:
                            rt["cooldown_windows_remaining"] -= 1
                        if isinstance(executor, PaperExecutor):
                            cur_realized = float(executor.realize_pnl())
                            prev_realized = float(rt["last_window_realized"])
                            window_pnl = cur_realized - prev_realized
                            rt["last_window_realized"] = cur_realized
                            # Update stake for next window (double on win, half on loss).
                            stake = float(rt.get("stake_usd", self.config.buy_amount_usd))
                            base = float(rt.get("stake_base_usd", self.config.buy_amount_usd))
                            # Hard floor requested: never stake below $1.00 while lane is active.
                            min_stake = max(1.0, base * float(self.config.stake_min_mult))
                            max_mult = rt.get("stake_max_mult_override") or self.config.stake_max_mult
                            max_stake = max(min_stake, base * float(max_mult))
                            confidence = float(rt.get("last_confidence", 0.5))
                            win_mult = 1.0 + (0.5 * confidence)
                            loss_mult = 1.0 - (0.4 * confidence)
                            if bool(rt.get("dynamic_stake_enabled", True)):
                                if window_pnl > 1e-9:
                                    stake = min(max_stake, stake * win_mult)
                                elif window_pnl < -1e-9:
                                    stake = max(min_stake, stake * loss_mult)
                                rt["stake_usd"] = float(stake)
                            else:
                                rt["stake_usd"] = float(max(1.0, base))
                            rt["last_window_pnl"] = window_pnl
                            start_bal = float(getattr(executor, "_starting_balance", 0.0) or 0.0)
                            if start_bal > 1e-9:
                                loss_pct = max(0.0, (-cur_realized / start_bal) * 100.0)
                                rt["loss_from_start_pct"] = float(loss_pct)
                                max_loss_pct = float(getattr(self.config, "strategy_max_loss_pct", 20.0))
                                if loss_pct >= max_loss_pct and not bool(rt.get("disabled_due_to_loss_cap", False)):
                                    rt["disabled_due_to_loss_cap"] = True
                                    rt["disabled_reason"] = (
                                        f"disabled_loss_cap:{loss_pct:.2f}%>={max_loss_pct:.2f}%"
                                    )
                                    setattr(_s, "last_rejection_reason", rt["disabled_reason"])
                                    logger.warning(
                                        "Disabling strategy lane %s after %.2f%% loss (cap %.2f%%)",
                                        suffix,
                                        loss_pct,
                                        max_loss_pct,
                                    )
                    _CIRCUIT_EXEMPT = {"oracle_lag_proxy", "oracle_lag_early", "late_confidence"}
                    loss_count = sum(
                        1 for _, _, _, _, sfx in self._lanes
                        if self._lane_runtime.get(sfx, {}).get("last_window_pnl", 0) < -1e-9
                    )
                    if loss_count >= 3:
                        logger.warning("Trending regime detected: %d lanes lost. Pausing direction-dependent for 2 windows.", loss_count)
                        for _, _, _, _, sfx in self._lanes:
                            if sfx not in _CIRCUIT_EXEMPT:
                                rt2 = self._lane_runtime.get(sfx, {})
                                rt2["cooldown_windows_remaining"] = max(
                                    rt2.get("cooldown_windows_remaining", 0), 2
                                )
                    # Feed previous window resolution to WindowMomentumCarry strategies
                    if old_token_map and hasattr(self, "_last_books"):
                        for _s, _ex, _st, _lb, _sfx in self._lanes:
                            if hasattr(_s, "set_last_resolution"):
                                for out_name, tid in old_token_map.items():
                                    book = self._last_books.get(tid)
                                    if book is None:
                                        continue
                                    lt = getattr(book, "last_trade_price", None)
                                    if lt is not None and lt > 0.5:
                                        _s.set_last_resolution(out_name, lt)
                                        break
                    prev_slug = getattr(self, "_event_slug", "")
                    self._event = ev
                    self._event_slug = ev.slug
                    self._reference_btc = None
                    if prev_slug != self._event_slug:
                        logger.info("Switched to next 5m window: %s — %s", self._event_slug, getattr(ev, "title", ""))

                event = self._event
                now = self._now_utc()
                # Ensure we have a reference BTC even if the runner starts late in a window.
                if self._reference_btc is None:
                    px = self._external.get_local_btc_price() if self._external else get_btc_price_usd()
                    if px is not None:
                        self._reference_btc = float(px)
                        self._last_local_btc = float(px)
                # Use slug timestamp as canonical window start (API start_date can be wrong/missing)
                window_start = event.start_date
                m = re.match(r"^btc-updown-5m-(\d+)$", event.slug)
                if m:
                    try:
                        ts = int(m.group(1))
                        window_start = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
                    except (ValueError, OSError):
                        pass
                elapsed = None
                if window_start:
                    elapsed = (now - window_start).total_seconds()
                elif event.end_date:
                    elapsed = max(0, 300 - (event.end_date - now).total_seconds())

                in_entry = elapsed is None or elapsed < self.config.time_window_seconds
                if in_entry:
                    self._status_message = (
                        f"Watching {event.slug} — first {self.config.time_window_seconds // 60}m for 25¢ entry"
                    )
                else:
                    self._status_message = (
                        f"Entry window closed ({event.slug[-10:]}…)"
                    )
                    self._price_refresh_tick += 1
                    if self._price_refresh_tick % 15 == 0:
                        self._refresh_outcome_prices(event)

                books = {}
                token_ids = {}
                self._outcome_prices = {}
                for m in event.markets:
                    token_ids[m.outcome] = m.token_id
                    ob = None
                    if self._external:
                        cached = self._external.get_clob_book(m.token_id)
                        ob = self._book_from_cached(m.token_id, cached or {})
                    if ob is None:
                        rest_fetches += 1
                        ob = fetch_book(m.token_id, self.config.clob_api_base)
                    if ob:
                        books[m.token_id] = ob
                        self._outcome_prices[m.outcome] = {
                            "best_ask": ob.best_ask,
                            "best_bid": ob.best_bid,
                            "last_trade": ob.last_trade_price,
                        }
                if self._external:
                    self._external.set_clob_tokens(list(token_ids.values()))

                self._last_books = dict(books)
                self._last_rest_fetches = int(rest_fetches)
                if in_entry:
                    ref_btc = self._reference_btc
                    if ref_btc is None:
                        ref_btc = self._external.get_local_btc_price() if self._external else get_btc_price_usd()
                        self._reference_btc = ref_btc
                    cur_btc = self._external.get_local_btc_price() if self._external else get_btc_price_usd()
                    if cur_btc is not None:
                        self._last_local_btc = float(cur_btc)
                    self._record_btc_tick(now, cur_btc)
                    atr_1m_10m = self._atr_1m_10m(now)
                    move_30s = self._btc_move_30s(now, cur_btc)
                    ext = self._external.snapshot(self._last_local_btc) if self._external else None
                    data = MarketData(
                        event_id=event.event_id,
                        token_ids=token_ids,
                        books=books,
                        reference_btc_price=ref_btc,
                        current_btc_price=cur_btc,
                        elapsed_seconds=elapsed,
                        seconds_to_window_end=(300.0 - float(elapsed)) if elapsed is not None else None,
                        event_slug=event.slug,
                        btc_atr_1m_10m=atr_1m_10m,
                        btc_move_30s=move_30s,
                        binance_price=getattr(ext, "binance_price", None),
                        binance_move_30s=getattr(ext, "binance_move_30s", None),
                        oracle_gap_usd=getattr(ext, "oracle_gap_usd", None),
                        funding_rate=getattr(ext, "funding_rate", None),
                        open_interest=getattr(ext, "open_interest", None),
                        open_interest_change_5m=getattr(ext, "open_interest_change_5m", None),
                        binance_depth_imbalance=getattr(ext, "binance_depth_imbalance", None),
                        external_last_ws_at=getattr(ext, "last_ws_at", None),
                    )
                else:
                    cur_btc = self._external.get_local_btc_price() if self._external else get_btc_price_usd()
                    if cur_btc is not None:
                        self._last_local_btc = float(cur_btc)
                    self._record_btc_tick(now, cur_btc)
                    ext = self._external.snapshot(self._last_local_btc) if self._external else None
                    data = MarketData(
                        event_id=event.event_id,
                        token_ids=token_ids,
                        books=books,
                        reference_btc_price=self._reference_btc,
                        current_btc_price=cur_btc if cur_btc is not None else self._last_local_btc,
                        elapsed_seconds=elapsed,
                        seconds_to_window_end=(300.0 - float(elapsed)) if elapsed is not None else None,
                        event_slug=event.slug,
                        btc_atr_1m_10m=self._atr_1m_10m(now),
                        btc_move_30s=self._btc_move_30s(now, cur_btc),
                        binance_price=getattr(ext, "binance_price", None),
                        binance_move_30s=getattr(ext, "binance_move_30s", None),
                        oracle_gap_usd=getattr(ext, "oracle_gap_usd", None),
                        funding_rate=getattr(ext, "funding_rate", None),
                        open_interest=getattr(ext, "open_interest", None),
                        open_interest_change_5m=getattr(ext, "open_interest_change_5m", None),
                        binance_depth_imbalance=getattr(ext, "binance_depth_imbalance", None),
                        external_last_ws_at=getattr(ext, "last_ws_at", None),
                    )

                for strategy, executor, state, _label, suffix in self._lanes:
                    rt = self._lane_runtime.get(suffix) or {}
                    if bool(rt.get("disabled_due_to_loss_cap", False)):
                        continue
                    if int(rt.get("cooldown_windows_remaining", 0)) > 0:
                        continue
                    # Apply per-lane dynamic stake (paper sizing)
                    stake = rt.get("stake_usd")
                    if stake is not None and hasattr(strategy, "buy_amount_usd"):
                        try:
                            desired_stake = float(stake)
                            if isinstance(executor, PaperExecutor):
                                bal = float(executor.get_balance())
                                # Keep lane trading while it has at least $1 available.
                                if bal < 1.0:
                                    continue
                                effective_stake = min(desired_stake, bal)
                                if effective_stake < 1.0:
                                    continue
                                rt["stake_usd"] = float(max(1.0, effective_stake))
                                setattr(strategy, "buy_amount_usd", float(rt["stake_usd"]))
                            else:
                                setattr(strategy, "buy_amount_usd", float(max(1.0, desired_stake)))
                        except Exception:
                            pass
                    run_strategy_tick(strategy, data, executor, state)
                    rt["last_confidence"] = float(getattr(strategy, 'last_confidence', 0.5))

                for strategy, executor, state, _label, _ in self._lanes:
                    if isinstance(executor, PaperExecutor):
                        executor.try_fill_pending_sells(books)
                        state.session_profit = executor.realize_pnl()
                        state.total_profit = state.session_profit
                        curve = executor.get_equity_curve()
                        state.equity_curve = [(t.isoformat(), b) for t, b in curve]
                        state.trades = [
                            {
                                "ts": f.ts.isoformat(),
                                "side": f.side,
                                "outcome": getattr(f, "outcome", "") or "—",
                                "price": f.price,
                                "size": f.size,
                                "amount_usd": f.amount_usd,
                            }
                            for f in executor.get_fill_history()
                        ]
                for lane in self._lanes:
                    self._append_trades_to_log(lane)
            except Exception as e:
                logger.exception("Loop error: %s", e)
                self.state.last_error = str(e)
            if self._external:
                self._external.wait_for_urgent_signal(timeout_sec=poll_active)
            else:
                time.sleep(poll_active)

            cycle_ms = (time.perf_counter() - cycle_t0) * 1000.0
            self._loop_cycle_ms_last = float(cycle_ms)
            self._loop_cycle_ms_avg = (
                cycle_ms if self._loop_cycle_ms_samples <= 0 else (self._loop_cycle_ms_avg * 0.9 + cycle_ms * 0.1)
            )
            self._loop_cycle_ms_samples += 1

    def _find_lane(self, strategy_id: str) -> Optional[tuple]:
        sid = (strategy_id or "").strip().lower()
        for lane in self._lanes:
            if lane[4] == sid:
                return lane
        return None

    @staticmethod
    def _window_key_from_iso(iso_ts: str) -> str:
        try:
            ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()
            window_start = int(math.floor(ts / 300.0) * 300)
            return str(window_start)
        except Exception:
            return ""

    @staticmethod
    def _build_roundtrips(trades: list[dict]) -> list[dict]:
        """Pair buys and sells (FIFO) into combined lifecycle rows."""
        open_lots: dict[str, list[dict]] = {}
        rows: list[dict] = []
        seq = 0
        for t in trades:
            side = str(t.get("side") or "").lower()
            outcome = str(t.get("outcome") or "—")
            price = float(t.get("price") or 0.0)
            size = float(t.get("size") or 0.0)
            ts = str(t.get("ts") or "")
            if size <= 0:
                continue
            lots = open_lots.setdefault(outcome, [])
            if side == "buy":
                lots.append({"remaining": size, "price": price, "ts": ts, "window_key": StrategyRunner._window_key_from_iso(ts)})
                continue
            if side != "sell":
                continue
            remaining_sell = size
            while remaining_sell > 1e-9 and lots:
                lot = lots[0]
                matched = min(remaining_sell, float(lot["remaining"]))
                buy_price = float(lot["price"])
                pnl = (price - buy_price) * matched
                seq += 1
                rows.append(
                    {
                        "id": seq,
                        "outcome": outcome,
                        "window_key": lot.get("window_key") or StrategyRunner._window_key_from_iso(ts),
                        "buy_ts": lot["ts"],
                        "sell_ts": ts,
                        "buy_price": buy_price,
                        "sell_price": price,
                        "size": matched,
                        "buy_usd": buy_price * matched,
                        "sell_usd": price * matched,
                        "pnl_usd": pnl,
                    }
                )
                lot["remaining"] = float(lot["remaining"]) - matched
                remaining_sell -= matched
                if lot["remaining"] <= 1e-9:
                    lots.pop(0)
        rows.reverse()
        return rows

    def get_strategy_trades(self, strategy_id: str, offset: int = 0, limit: int = 100) -> dict:
        lane = self._find_lane(strategy_id)
        if lane is None:
            return {"id": strategy_id, "total": 0, "items": []}
        _strategy, _executor, state, _label, suffix = lane
        all_trades = list(state.trades or [])
        all_trades.reverse()
        start = max(0, int(offset))
        end = max(start, start + max(1, min(int(limit), 10000)))
        return {
            "id": suffix,
            "total": len(all_trades),
            "offset": start,
            "limit": int(limit),
            "items": all_trades[start:end],
        }

    def get_strategy_roundtrips(self, strategy_id: str, offset: int = 0, limit: int = 100) -> dict:
        lane = self._find_lane(strategy_id)
        if lane is None:
            return {"id": strategy_id, "total": 0, "items": []}
        _strategy, _executor, state, _label, suffix = lane
        rows = self._build_roundtrips(list(state.trades or []))
        start = max(0, int(offset))
        end = max(start, start + max(1, min(int(limit), 10000)))
        return {
            "id": suffix,
            "total": len(rows),
            "offset": start,
            "limit": int(limit),
            "items": rows[start:end],
        }

    def get_state(self) -> dict:
        status = self._status_message if self.state.running else "Stopped"
        last_poll = self._last_poll_at.isoformat() if self._last_poll_at else None
        event_title = ""
        if self._event:
            event_title = getattr(self._event, "title", "") or ""
        strategies = []
        for strategy, executor, state, _label, suffix in self._lanes:
            if isinstance(executor, PaperExecutor):
                session_pnl = executor.realize_pnl()
                starting_balance = float(getattr(executor, "_starting_balance", 0.0) or 0.0)
                balance = float(executor.get_balance())
            else:
                session_pnl = state.session_profit
                starting_balance = 0.0
                balance = executor.get_balance()
            invested = executor.get_invested_amount()
            positions = list(executor.get_positions() or [])
            current_window_outcome = ""
            current_window_entry_price = None
            if positions:
                # Pick the largest open leg if multiple are present.
                top_pos = max(positions, key=lambda p: float(getattr(p, "size", 0.0) or 0.0))
                current_window_outcome = str(getattr(top_pos, "outcome", "") or "")
                try:
                    current_window_entry_price = float(getattr(top_pos, "avg_price", None))
                except (TypeError, ValueError):
                    current_window_entry_price = None
            rt = self._lane_runtime.get(suffix, {})
            roi_pct = (session_pnl / starting_balance * 100.0) if starting_balance > 1e-9 else 0.0
            disabled_due_to_loss_cap = bool(rt.get("disabled_due_to_loss_cap", False))
            loss_from_start_pct = float(rt.get("loss_from_start_pct", 0.0))
            max_loss_pct = float(getattr(self.config, "strategy_max_loss_pct", 20.0))
            strategies.append({
                "balance": balance,
                "invested_amount": invested,
                "session_profit": session_pnl,
                "total_profit": state.total_profit,
                "session_trade_count": state.session_trade_count,
                "trade_count": state.trade_count,
                "current_window_outcome": current_window_outcome,
                "current_window_entry_price": current_window_entry_price,
                "equity_curve": state.equity_curve[-100:],
                "trades": state.trades[-50:],
                "session_start": state.session_start.isoformat() if state.session_start else None,
                "last_error": state.last_error,
                "strategy_name": getattr(strategy, "name", strategy.__class__.__name__),
                "max_trades_per_window": int(getattr(strategy, "max_trades_per_window", 1)),
                "last_rejection_reason": str(getattr(strategy, "last_rejection_reason", "")),
                "cooldown_windows_remaining": int(rt.get("cooldown_windows_remaining", 0)),
                "stake_usd": float(rt.get("stake_usd", self.config.buy_amount_usd)),
                "dynamic_stake_enabled": bool(rt.get("dynamic_stake_enabled", True)),
                "staking_mode": "dynamic" if bool(rt.get("dynamic_stake_enabled", True)) else "fixed",
                "safe_mode_enabled": suffix in {"momentum_carry", "momentum_carry_classic", "opening_scalper", "price_skew_fade"},
                "safe_mode": "safe" if suffix in {"momentum_carry", "momentum_carry_classic", "opening_scalper", "price_skew_fade"} else "unsafe",
                "starting_balance": starting_balance,
                "roi_pct": float(roi_pct),
                "active": not disabled_due_to_loss_cap,
                "disabled_due_to_loss_cap": disabled_due_to_loss_cap,
                "disabled_reason": str(rt.get("disabled_reason", "")),
                "loss_from_start_pct": loss_from_start_pct,
                "max_loss_pct": max_loss_pct,
            })
        first = strategies[0] if strategies else {}
        lane_labels = [lane[3] for lane in self._lanes]
        lane_suffixes = [lane[4] for lane in self._lanes]
        for i, label in enumerate(lane_labels):
            if i < len(strategies):
                strategies[i]["id"] = lane_suffixes[i]
                strategies[i]["label"] = label
        trades_log_files = {
            lane_suffixes[i]: str((self._trades_log_dir / f"trades_log_{lane_suffixes[i]}.md").resolve())
            for i in range(len(lane_suffixes))
        }
        ext = self._external.snapshot() if self._external else None
        return {
            "running": self.state.running,
            "mode": self.state.mode,
            "balance": first.get("balance", 0),
            "invested_amount": first.get("invested_amount", 0),
            "session_profit": first.get("session_profit", 0),
            "total_profit": first.get("total_profit", 0),
            "session_trade_count": first.get("session_trade_count", 0),
            "trade_count": first.get("trade_count", 0),
            "equity_curve": first.get("equity_curve", []),
            "trades": first.get("trades", []),
            "session_start": first.get("session_start"),
            "last_error": first.get("last_error"),
            "status_message": status,
            "last_poll_at": last_poll,
            "event_slug": getattr(self, "_event_slug", ""),
            "event_title": event_title,
            "strategies": strategies,
            "outcome_prices": dict(self._outcome_prices),
            "trades_log_dir": str(self._trades_log_dir.resolve()),
            "trades_log_files": trades_log_files,
            "external_data_enabled": bool(self._external),
            "external_data_last_ws_at": (
                ext.last_ws_at if ext else None
            ),
            "main_loop_cycle_ms_avg": float(self._loop_cycle_ms_avg),
            "main_loop_cycle_ms_last": float(self._loop_cycle_ms_last),
            "clob_ws_connected": bool(self._external.clob_ws_is_connected()) if self._external else False,
            "clob_last_update_age_sec": (
                self._external.clob_last_update_age_sec() if self._external else None
            ),
            "clob_last_error_msg": (
                self._external.clob_last_error_msg() if self._external else None
            ),
            "urgent_wake_count_60s": (
                self._external.urgent_wake_count_last_60s() if self._external else 0
            ),
            "last_rest_fetches": int(self._last_rest_fetches),
            "external_snapshot": (
                {
                    "binance_price": ext.binance_price,
                    "binance_move_30s": ext.binance_move_30s,
                    "oracle_gap_usd": ext.oracle_gap_usd,
                    "funding_rate": ext.funding_rate,
                    "open_interest": ext.open_interest,
                    "open_interest_change_5m": ext.open_interest_change_5m,
                    "binance_depth_imbalance": ext.binance_depth_imbalance,
                }
                if ext
                else {}
            ),
        }


def get_runner() -> Optional[StrategyRunner]:
    with _runner_lock:
        return _runner


def start_runner(mode: str = "paper") -> StrategyRunner:
    global _runner
    cfg = Config.from_env()
    event, msg = initialize_strategy(cfg)
    if not event:
        raise RuntimeError(msg or "Could not resolve Bitcoin 5m event")

    lanes: list[tuple] = []
    if mode == "paper":
        paper_bank = SharedPaperBank(
            balance=float(cfg.paper_starting_balance),
            starting_balance=float(cfg.paper_starting_balance),
        )

        def _paper_exec() -> Executor:
            return create_executor(
                cfg,
                mode_override="paper",
                shared_paper_bank=paper_bank,
            )

        sma_strategy = Btc5mSmaStrategy(
            sell_limit_cents=cfg.sell_limit_cents,
            max_btc_move_usd=cfg.max_btc_move_usd,
            time_window_seconds=180,
            buy_amount_usd=cfg.buy_amount_usd,
            sma_window_ticks=cfg.sma_window_ticks,
            sma_discount_cents=0.5,
            sma_max_entry_cents=cfg.sma_max_entry_cents,
            max_trades_per_window=1,
        )
        lanes.append(
            _lane_tuple(
                sma_strategy,
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Short MA Dip",
                "sma",
            )
        )
        lanes.append(
            _lane_tuple(
                WindowMomentumCarryStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Trend Momentum Scalper",
                "momentum_carry",
            )
        )
        lanes.append(
            _lane_tuple(
                WindowMomentumCarryClassicStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Window Momentum Carry",
                "momentum_carry_classic",
            )
        )
        lanes.append(
            _lane_tuple(
                RebalancingArbStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    combined_ask_max=0.985,
                    entry_end_sec=260.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Rebalancing Arb",
                "rebalancing_arb",
            )
        )
        lanes.append(
            _lane_tuple(
                OpeningDiscountScalperStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    max_entry_cents=30,
                    sell_target_cents=55,
                    entry_end_sec=90.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Opening Discount Scalper",
                "opening_scalper",
            )
        )
        lanes.append(
            _lane_tuple(
                VolumeSurgeBreakoutStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    min_depth_imbalance=1.8,
                    move_30s_trigger_usd=40.0,
                    max_entry_cents=40,
                    sell_target_cents=78,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Volume Surge Breakout",
                "volume_breakout",
            )
        )
        lanes.append(
            _lane_tuple(
                FundingTrendConfirmStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    min_abs_funding=0.00002,
                    min_window_move_usd=25.0,
                    min_move_30s_usd=12.0,
                    max_entry_cents=45,
                    sell_target_cents=68,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Funding Trend Confirm",
                "funding_confirm",
            )
        )
        lanes.append(
            _lane_tuple(
                SignalFusionStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    sma_window_ticks=cfg.sma_window_ticks,
                    sma_discount_cents=0.5,
                    imbalance_ratio=1.2,
                    entry_end_sec=200.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "MA + Orderflow Fusion (Constant)",
                "fusion_const",
            )
        )
        lanes.append(
            _lane_tuple(
                OracleLagArbProxyStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    use_external=cfg.enable_external_data and cfg.enable_binance_ws,
                    move_30s_trigger_usd=cfg.oracle_lag_move_trigger_usd,
                    oracle_gap_trigger_usd=cfg.oracle_lag_gap_trigger_usd,
                    max_entry_cents=cfg.oracle_lag_max_entry_cents,
                    min_profit_margin=cfg.oracle_lag_min_profit_margin,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Oracle Lag Arb" if (cfg.enable_external_data and cfg.enable_binance_ws) else "Oracle Lag Arb (Proxy)",
                "oracle_lag_proxy",
            )
        )
        lanes.append(
            _lane_tuple(
                LateHighConfidenceStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    late_start_sec=220.0,
                    btc_move_trigger_usd=60.0,
                    max_entry_cents=78,
                    sell_target_cents=93,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Late High Confidence",
                "late_confidence",
            )
        )
        lanes.append(
            _lane_tuple(
                HybridEarlyMomentumStrategy(
                    buy_threshold_cents=cfg.buy_threshold_cents,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    momentum_trigger_usd=cfg.hybrid_momentum_trigger_usd,
                    atr_min_usd=cfg.hybrid_atr_min_usd,
                    max_entry_cents=cfg.hybrid_max_entry_cents,
                    entry_end_sec=200.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Hybrid <=25c + Momentum",
                "hybrid_momentum",
            )
        )
        lanes.append(
            _lane_tuple(
                AtrGuardThresholdStrategy(
                    buy_threshold_cents=cfg.buy_threshold_cents,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    atr_multiplier=2.5,
                    entry_end_sec=180.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "ATR Guard Threshold",
                "atr_guard",
            )
        )
        lanes.append(
            _lane_tuple(
                MidWindowMomentumStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    move_30s_min_usd=7.0,
                    window_move_min_usd=10.0,
                    max_entry_cents=40,
                    sell_target_cents=65,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Mid-Window Momentum",
                "mid_momentum",
            )
        )
        lanes.append(
            _lane_tuple(
                FlatMarketMeanReversionStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    max_atr_usd=40.0,
                    max_window_move_usd=25.0,
                    max_move_30s_usd=20.0,
                    max_entry_cents=44,
                    sell_target_cents=68,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Flat Market Mean Reversion",
                "flat_mean_rev",
            )
        )
        lanes.append(
            _lane_tuple(
                ConfirmedFlatScalperStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    max_atr_usd=30.0,
                    max_window_move_usd=18.0,
                    max_move_30s_usd=12.0,
                    max_entry_cents=46,
                    sell_target_cents=72,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Confirmed Flat Scalper",
                "confirmed_flat",
            )
        )
        lanes.append(
            _lane_tuple(
                PriceSkewFadeStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    min_skew_cents=10.0,
                    max_atr_usd=30.0,
                    max_window_move_usd=25.0,
                    max_move_30s_usd=18.0,
                    max_entry_cents=47,
                    sell_target_cents=65,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Price Skew Fade",
                "price_skew_fade",
            )
        )
        lanes.append(
            _lane_tuple(
                EarlyBreakoutStrategy(
                    move_30s_trigger_usd=40.0,
                    max_entry_cents=40,
                    sell_target_cents=70,
                    buy_amount_usd=cfg.buy_amount_usd,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Early Breakout",
                "early_breakout",
            )
        )
        lanes.append(
            _lane_tuple(
                SustainedTrendLockInStrategy(
                    min_window_move_usd=60.0,
                    min_move_30s_usd=15.0,
                    min_entry_cents=15,
                    max_entry_cents=75,
                    sell_target_cents=90,
                    buy_amount_usd=cfg.buy_amount_usd,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Sustained Trend Lock-In",
                "sustained_trend",
            )
        )
        lanes.append(
            _lane_tuple(
                CascadeTrendLockStrategy(
                    min_window_move_usd=60.0,
                    min_move_30s_usd=15.0,
                    min_abs_funding=0.00002,
                    min_entry_cents=40,
                    max_entry_cents=72,
                    sell_target_cents=92,
                    buy_amount_usd=cfg.buy_amount_usd,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Cascade Trend Lock",
                "cascade_trend",
            )
        )
    else:
        live_strategy = Btc5mStrategy(
            buy_threshold_cents=cfg.buy_threshold_cents,
            sell_limit_cents=cfg.sell_limit_cents,
            max_btc_move_usd=cfg.max_btc_move_usd,
            time_window_seconds=cfg.time_window_seconds,
            buy_amount_usd=cfg.buy_amount_usd,
        )
        executor = create_executor(cfg, mode_override=mode)
        state = StrategyRunState(mode=mode, session_start=datetime.utcnow())
        lanes.append(_lane_tuple(live_strategy, executor, state, "Live", "live"))

    if mode == "paper":
        # Enforce one shared simulated wallet across all strategy lanes.
        for idx, lane in enumerate(lanes):
            strategy, executor, state, label, suffix = lane
            if isinstance(executor, PaperExecutor):
                executor._bank = paper_bank
                executor._starting_balance = float(paper_bank.starting_balance)
                lanes[idx] = _lane_tuple(strategy, executor, state, label, suffix)

    with _runner_lock:
        if _runner and _runner.state.running:
            _runner.stop()
        _runner = StrategyRunner(initial_event=event, config=cfg, lanes=lanes)
        _stake_overrides = {
            "sma": 3.0,
            "rebalancing_arb": 2.0,
            "opening_scalper": 2.0,
            "oracle_lag_proxy": 2.0,
            "late_confidence": 3.0,
            "mid_momentum": 2.0,
        }
        for suffix, mult in _stake_overrides.items():
            if suffix in _runner._lane_runtime:
                _runner._lane_runtime[suffix]["stake_max_mult_override"] = mult
        # Stake mode policy:
        # Keep dynamic staking enabled only for selected high-conviction lanes.
        _dynamic_stake_suffixes = {"early_breakout", "sustained_trend", "rebalancing_arb", "cascade_trend"}
        for suffix, rt in _runner._lane_runtime.items():
            if suffix in _dynamic_stake_suffixes:
                rt["dynamic_stake_enabled"] = True
                rt["stake_usd"] = float(max(1.0, rt.get("stake_base_usd", cfg.buy_amount_usd)))
            else:
                rt["dynamic_stake_enabled"] = False
                rt["stake_usd"] = float(max(1.0, rt.get("stake_base_usd", cfg.buy_amount_usd)))
        _runner.start()
    return _runner


def stop_runner() -> None:
    with _runner_lock:
        if _runner:
            _runner.stop()
