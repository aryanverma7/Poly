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
from executor import Executor, PaperExecutor, create_executor
from external_data import ExternalDataService
from orderbook import fetch_book, get_btc_price_usd
from paper_engine import StrategyRunState, run_strategy_tick
from strategies.base import MarketData, Strategy
from strategies.advanced import (
    AdaptiveExitStrategy,
    AtrGuardThresholdStrategy,
    CrossMarketSentimentProxyStrategy,
    EndWindowMomentumStrategy,
    HybridEarlyMomentumStrategy,
    LayeredLimitEntryStrategy,
    MeanReversionExtremeStrategy,
    MicroMarketMakingProxyStrategy,
    OracleLagArbProxyStrategy,
    OrderBookImbalanceStrategy,
    SignalFusionStrategy,
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
        # Per-lane risk runtime: realized baseline, loss streak and cooldown windows.
        self._lane_runtime: dict[str, dict] = {}
        for _s, ex, _st, _label, suffix in lanes:
            baseline = ex.realize_pnl() if isinstance(ex, PaperExecutor) else 0.0
            base_stake = float(getattr(_s, 'buy_amount_usd', self.config.buy_amount_usd))
            self._lane_runtime[suffix] = {
                "last_window_realized": float(baseline),
                "consecutive_losses": 0,
                "cooldown_windows_remaining": 0,
                "stake_base_usd": base_stake,
                "stake_usd": base_stake,
                "last_confidence": 0.5,
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
        if len(self._btc_ticks) < 30:
            return None
        cutoff = now.timestamp() - 11 * 60
        ticks = [(t, p) for (t, p) in self._btc_ticks if t.timestamp() >= cutoff]
        if len(ticks) < 20:
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
        if len(mins) < 11:
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
        if len(trs) < 10:
            return None
        return sum(trs[-10:]) / 10.0

    def _loop(self) -> None:
        poll_active = 0.08
        poll_wait_window = 5.0

        while not self._stop.is_set():
            try:
                self._last_poll_at = datetime.utcnow()

                if self._window_ended() or not self._event:
                    old_token_ids = [m.token_id for m in self._event.markets] if self._event else []
                    self._status_message = "Resolving next 5m window..."
                    ev, msg = discover_btc_5m_event(self.config)
                    if not ev:
                        self._status_message = msg[:120] if msg else "Waiting for next event..."
                        time.sleep(poll_wait_window)
                        continue
                    for _s, executor, _st, _label, suffix in self._lanes:
                        if isinstance(executor, PaperExecutor) and old_token_ids:
                            executor.settle_unfilled_at_window_end(old_token_ids)
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
                            min_stake = max(0.01, base * float(self.config.stake_min_mult))
                            max_stake = max(min_stake, base * float(self.config.stake_max_mult))
                            confidence = float(rt.get("last_confidence", 0.5))
                            win_mult = 1.0 + (0.5 * confidence)
                            loss_mult = 1.0 - (0.4 * confidence)
                            if window_pnl > 1e-9:
                                stake = min(max_stake, stake * win_mult)
                            elif window_pnl < -1e-9:
                                stake = max(min_stake, stake * loss_mult)
                            rt["stake_usd"] = float(stake)
                            if window_pnl < -1e-9:
                                rt["consecutive_losses"] += 1
                            else:
                                rt["consecutive_losses"] = 0
                            if rt["consecutive_losses"] >= self.config.max_consecutive_losses:
                                rt["cooldown_windows_remaining"] = self.config.cooldown_windows_after_losses
                                rt["consecutive_losses"] = 0
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
                    px = get_btc_price_usd()
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
                    ob = fetch_book(m.token_id, self.config.clob_api_base)
                    if ob:
                        books[m.token_id] = ob
                        self._outcome_prices[m.outcome] = {
                            "best_ask": ob.best_ask,
                            "best_bid": ob.best_bid,
                            "last_trade": ob.last_trade_price,
                        }

                if in_entry:
                    ref_btc = self._reference_btc
                    if ref_btc is None:
                        ref_btc = get_btc_price_usd()
                        self._reference_btc = ref_btc
                    cur_btc = get_btc_price_usd()
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
                    cur_btc = get_btc_price_usd()
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
                    if int(rt.get("cooldown_windows_remaining", 0)) > 0:
                        continue
                    # Apply per-lane dynamic stake (paper sizing)
                    stake = rt.get("stake_usd")
                    if stake is not None and hasattr(strategy, "buy_amount_usd"):
                        try:
                            setattr(strategy, "buy_amount_usd", float(stake))
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
            time.sleep(poll_active)

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
        end = max(start, start + max(1, min(int(limit), 500)))
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
        end = max(start, start + max(1, min(int(limit), 500)))
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
            balance = executor.get_balance()
            if isinstance(executor, PaperExecutor):
                session_pnl = executor.realize_pnl()
                starting_balance = float(getattr(executor, "_starting_balance", 0.0) or 0.0)
            else:
                session_pnl = state.session_profit
                starting_balance = 0.0
            invested = executor.get_invested_amount()
            rt = self._lane_runtime.get(suffix, {})
            roi_pct = (session_pnl / starting_balance * 100.0) if starting_balance > 1e-9 else 0.0
            strategies.append({
                "balance": balance,
                "invested_amount": invested,
                "session_profit": session_pnl,
                "total_profit": state.total_profit,
                "session_trade_count": state.session_trade_count,
                "trade_count": state.trade_count,
                "equity_curve": state.equity_curve[-100:],
                "trades": state.trades[-50:],
                "session_start": state.session_start.isoformat() if state.session_start else None,
                "last_error": state.last_error,
                "strategy_name": getattr(strategy, "name", strategy.__class__.__name__),
                "max_trades_per_window": int(getattr(strategy, "max_trades_per_window", 1)),
                "last_rejection_reason": str(getattr(strategy, "last_rejection_reason", "")),
                "consecutive_losses": int(rt.get("consecutive_losses", 0)),
                "cooldown_windows_remaining": int(rt.get("cooldown_windows_remaining", 0)),
                "stake_usd": float(rt.get("stake_usd", self.config.buy_amount_usd)),
                "starting_balance": starting_balance,
                "roi_pct": float(roi_pct),
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
        threshold_strategy = Btc5mStrategy(
            buy_threshold_cents=cfg.buy_threshold_cents,
            sell_limit_cents=cfg.sell_limit_cents,
            max_btc_move_usd=cfg.max_btc_move_usd,
            time_window_seconds=cfg.time_window_seconds,
            buy_amount_usd=cfg.buy_amount_usd,
        )
        sma_strategy = Btc5mSmaStrategy(
            sell_limit_cents=cfg.sell_limit_cents,
            max_btc_move_usd=cfg.max_btc_move_usd,
            time_window_seconds=cfg.time_window_seconds,
            buy_amount_usd=cfg.buy_amount_usd,
            sma_window_ticks=cfg.sma_window_ticks,
            sma_discount_cents=cfg.sma_discount_cents,
            sma_max_entry_cents=cfg.sma_max_entry_cents,
            max_trades_per_window=1,
        )
        lanes.append(
            _lane_tuple(
                threshold_strategy,
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Threshold (<=25c)",
                "threshold",
            )
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
                AtrGuardThresholdStrategy(
                    buy_threshold_cents=cfg.buy_threshold_cents,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    atr_multiplier=1.5,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "ATR Guard",
                "atr_guard",
            )
        )
        lanes.append(
            _lane_tuple(
                OrderBookImbalanceStrategy(
                    buy_threshold_cents=cfg.buy_threshold_cents,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    imbalance_ratio=3.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Imbalance Trigger",
                "imbalance",
            )
        )
        lanes.append(
            _lane_tuple(
                LayeredLimitEntryStrategy(
                    buy_threshold_cents=cfg.buy_threshold_cents,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    passive_offset_cents=1.0,
                    timeout_sec=20.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Layered Entry",
                "layered",
            )
        )
        lanes.append(
            _lane_tuple(
                AdaptiveExitStrategy(
                    buy_threshold_cents=cfg.buy_threshold_cents,
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Adaptive Exit",
                "adaptive_exit",
            )
        )
        lanes.append(
            _lane_tuple(
                SignalFusionStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    sma_window_ticks=cfg.sma_window_ticks,
                    sma_discount_cents=cfg.sma_discount_cents,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "MA + Orderflow Fusion",
                "fusion",
            )
        )
        lanes.append(
            _lane_tuple(
                EndWindowMomentumStrategy(
                    sell_limit_cents=cfg.end_window_sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    late_start_sec=210.0,
                    btc_move_trigger_usd=cfg.end_window_move_trigger_usd,
                    max_entry_cents=cfg.end_window_max_entry_cents,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "End-Window Momentum",
                "end_window",
            )
        )
        lanes.append(
            _lane_tuple(
                MeanReversionExtremeStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    extreme_entry_cents=12,
                    max_move_for_contrarian_usd=25.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Mean Reversion",
                "mean_reversion",
            )
        )
        lanes.append(
            _lane_tuple(
                OracleLagArbProxyStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.oracle_lag_base_stake_usd,
                    use_external=cfg.enable_external_data and cfg.enable_binance_ws,
                    move_30s_trigger_usd=cfg.oracle_lag_move_trigger_usd,
                    oracle_gap_trigger_usd=cfg.oracle_lag_gap_trigger_usd,
                    stale_mid_band=cfg.oracle_lag_stale_mid_band,
                    max_entry_cents=cfg.oracle_lag_max_entry_cents,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Oracle Lag Arb" if (cfg.enable_external_data and cfg.enable_binance_ws) else "Oracle Lag Arb (Proxy)",
                "oracle_lag_proxy",
            )
        )
        _use_ext_oracle = cfg.enable_external_data and cfg.enable_binance_ws
        lanes.append(
            _lane_tuple(
                OracleLagArbProxyStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.oracle_lag_base_stake_usd,
                    use_external=_use_ext_oracle,
                    entry_start_sec=0.0,
                    entry_end_sec=30.0,
                    move_30s_trigger_usd=30.0,
                    oracle_gap_trigger_usd=3.0,
                    stale_mid_band=cfg.oracle_lag_stale_mid_band,
                    max_entry_cents=cfg.oracle_lag_max_entry_cents,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Oracle Lag Early" if _use_ext_oracle else "Oracle Lag Early (Proxy)",
                "oracle_lag_early",
            )
        )
        lanes.append(
            _lane_tuple(
                CrossMarketSentimentProxyStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    use_external=False,
                    entry_start_sec=0.0,
                    entry_end_sec=120.0,
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Cross-Market Bias (Proxy)",
                "cross_market_proxy",
            )
        )
        lanes.append(
            _lane_tuple(
                MicroMarketMakingProxyStrategy(
                    sell_limit_cents=cfg.sell_limit_cents,
                    max_btc_move_usd=cfg.max_btc_move_usd,
                    buy_amount_usd=cfg.buy_amount_usd,
                    use_external=cfg.enable_external_data and cfg.enable_binance_depth,
                    entry_end_sec=300.0,
                    max_trades_per_window=5,
                    max_entry_cents=55,
                    spread_capture_cents=5.0,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Micro MM" if (cfg.enable_external_data and cfg.enable_binance_depth) else "Micro MM (Proxy)",
                "micro_mm_proxy",
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
                    max_trades_per_window=1,
                ),
                create_executor(cfg, mode_override="paper"),
                StrategyRunState(mode="paper", session_start=datetime.utcnow()),
                "Hybrid <=25c + Momentum",
                "hybrid_momentum",
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

    with _runner_lock:
        if _runner and _runner.state.running:
            _runner.stop()
        _runner = StrategyRunner(initial_event=event, config=cfg, lanes=lanes)
        _runner.start()
    return _runner


def stop_runner() -> None:
    with _runner_lock:
        if _runner:
            _runner.stop()
