"""
Additional experimental strategies for paper comparison.

These are intentionally simple, testable rule implementations of the ideas
discussed in research notes. A few signals that need external feeds in
production (oracle lag, funding/cross-market sentiment) are represented by
internal proxies so they can be A/B tested immediately in paper mode.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from executor import Executor
from strategies.base import MarketData, Strategy

logger = logging.getLogger(__name__)


def _best_ask(book) -> Optional[float]:
    if book is None:
        return None
    if hasattr(book, "best_ask") and book.best_ask is not None:
        return float(book.best_ask)
    asks = getattr(book, "asks", None)
    if asks is None and isinstance(book, dict):
        asks = book.get("asks")
    if asks:
        a = asks[0]
        return float(a.get("price", a) if isinstance(a, dict) else a)
    return None


def _best_bid(book) -> Optional[float]:
    if book is None:
        return None
    if hasattr(book, "best_bid") and book.best_bid is not None:
        return float(book.best_bid)
    bids = getattr(book, "bids", None)
    if bids is None and isinstance(book, dict):
        bids = book.get("bids")
    if bids:
        b = bids[0]
        return float(b.get("price", b) if isinstance(b, dict) else b)
    return None


def _last_trade(book) -> Optional[float]:
    if book is None:
        return None
    v = getattr(book, "last_trade_price", None)
    if v is None and isinstance(book, dict):
        v = book.get("last_trade_price")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _levels(book, side: str):
    vals = getattr(book, side, None)
    if vals is not None:
        return vals
    if isinstance(book, dict):
        return book.get(side) or []
    return []


def _depth_near(book, width: float = 0.02) -> tuple[float, float]:
    """Return (bid_depth_near_best, ask_depth_near_best)."""
    bid = _best_bid(book)
    ask = _best_ask(book)
    if bid is None or ask is None:
        return 0.0, 0.0
    bid_depth = 0.0
    for lvl in _levels(book, "bids"):
        p = float(getattr(lvl, "price", lvl.get("price", 0) if isinstance(lvl, dict) else 0))
        s = float(getattr(lvl, "size", lvl.get("size", 0) if isinstance(lvl, dict) else 0))
        if p >= bid - width:
            bid_depth += max(0.0, s)
    ask_depth = 0.0
    for lvl in _levels(book, "asks"):
        p = float(getattr(lvl, "price", lvl.get("price", 0) if isinstance(lvl, dict) else 0))
        s = float(getattr(lvl, "size", lvl.get("size", 0) if isinstance(lvl, dict) else 0))
        if p <= ask + width:
            ask_depth += max(0.0, s)
    return bid_depth, ask_depth


class _SingleEntryBase(Strategy):
    """Shared state helpers for once-per-window strategies."""

    def __init__(
        self,
        sell_limit_cents: int = 40,
        max_btc_move_usd: float = 100,
        buy_amount_usd: float = 5.0,
        entry_start_sec: float = 0.0,
        entry_end_sec: float = 120.0,
        max_trades_per_window: int = 1,
    ):
        self.sell_limit = sell_limit_cents / 100.0
        self.max_btc_move = max_btc_move_usd
        self.buy_amount_usd = buy_amount_usd
        self.entry_start_sec = entry_start_sec
        self.entry_end_sec = entry_end_sec
        self.max_trades_per_window = max(1, int(max_trades_per_window))
        self._last_window_slug: str = ""
        self._trades_this_window = 0
        self.last_rejection_reason: str = ""

    def _on_new_window(self):
        self._trades_this_window = 0
        self.last_rejection_reason = ""

    def _reset_if_new_window(self, slug: str):
        if slug and slug != self._last_window_slug:
            self._last_window_slug = slug
            self._on_new_window()

    def _entry_window_open(self, elapsed: Optional[float]) -> bool:
        if elapsed is None:
            return True
        return self.entry_start_sec <= elapsed < self.entry_end_sec

    def _can_trade(self) -> bool:
        return self._trades_this_window < self.max_trades_per_window

    def _reject(self, reason: str) -> None:
        self.last_rejection_reason = reason

    def _btc_guard_ok(self, data: MarketData, guard_override: Optional[float] = None) -> bool:
        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is None or cur is None:
            return True
        cap = guard_override if guard_override is not None else self.max_btc_move
        return abs(cur - ref) <= cap

    def _buy_and_queue_sell(
        self,
        data: MarketData,
        executor: Executor,
        token_id: str,
        outcome: str,
        entry_price: float,
        action: str,
        extra: Optional[dict] = None,
    ) -> Optional[dict]:
        max_price = min(entry_price + 0.04, 0.99)
        fill = executor.place_market_buy(
            token_id=token_id,
            amount_usd=self.buy_amount_usd,
            max_price=max_price,
            outcome=outcome,
            tick_size=data.tick_size,
            neg_risk=data.neg_risk,
            fill_at_price=entry_price,
        )
        if not fill:
            self.last_rejection_reason = "buy_not_filled"
            return None
        self._trades_this_window += 1
        self.last_rejection_reason = ""
        executor.place_limit_sell(
            token_id=token_id,
            size=fill.size,
            price=self.sell_limit,
            outcome=outcome,
            tick_size=data.tick_size,
            neg_risk=data.neg_risk,
        )
        payload = {
            "action": action,
            "token_id": token_id,
            "outcome": outcome,
            "buy_price": fill.price,
            "sell_limit": self.sell_limit,
            "size": fill.size,
        }
        if extra:
            payload.update(extra)
        return payload


class AtrGuardThresholdStrategy(_SingleEntryBase):
    """Threshold entry but guard scales with ATR instead of fixed USD move."""

    def __init__(
        self,
        buy_threshold_cents: int = 25,
        atr_multiplier: float = 3.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.buy_trigger = buy_threshold_cents / 100.0 + 0.012
        self.atr_multiplier = max(0.5, float(atr_multiplier))

    @property
    def name(self) -> str:
        return "ATR Guard Threshold"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade():
            self._reject("window_trade_cap_reached")
            return None
        if not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None
        atr = data.btc_atr_1m_10m
        guard = self.max_btc_move if atr is None else self.atr_multiplier * atr
        if not self._btc_guard_ok(data, guard_override=guard):
            self._reject("btc_guard_blocked")
            return None
        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask <= self.buy_trigger:
                candidates.append((ask, outcome, token_id))
        candidates.sort(key=lambda x: x[0])
        if not candidates:
            self._reject("no_price_trigger")
            return None
        ask, outcome, token_id = candidates[0]
        return self._buy_and_queue_sell(
            data, executor, token_id, outcome, ask, "buy_then_sell_atr_guard", {"guard_usd": round(guard, 2)}
        )


class HybridEarlyMomentumStrategy(_SingleEntryBase):
    """
    Hybrid: early undervaluation + momentum confirmation.
    - time < 120s
    - ask <= ~25c
    - momentum in same direction
    - ATR filter + BTC move guard
    """

    def __init__(
        self,
        buy_threshold_cents: int = 25,
        momentum_trigger_usd: float = 30.0,
        atr_min_usd: float = 80.0,
        max_entry_cents: int = 35,
        max_trades_per_window: int = 1,
        **kwargs,
    ):
        super().__init__(
            entry_start_sec=0.0,
            entry_end_sec=120.0,
            max_trades_per_window=max_trades_per_window,
            **kwargs,
        )
        self.buy_trigger = buy_threshold_cents / 100.0 + 0.012
        self.momentum_trigger = abs(float(momentum_trigger_usd))
        self.atr_min = abs(float(atr_min_usd))
        self.max_entry = max(1, int(max_entry_cents)) / 100.0

    @property
    def name(self) -> str:
        return "Hybrid <=25c + Momentum"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade():
            self._reject("window_trade_cap_reached")
            return None
        if not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None
        if not self._btc_guard_ok(data):
            self._reject("btc_guard_blocked")
            return None

        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        if move30 is None or abs(move30) < self.momentum_trigger:
            self._reject("momentum_too_weak")
            return None
        # ATR is a quality filter, not a hard blocker when feed/history is sparse.
        atr = data.btc_atr_1m_10m
        if atr is not None and atr < self.atr_min:
            # Allow exceptionally strong momentum to pass even in low measured ATR.
            if abs(move30) < self.momentum_trigger * 1.5:
                self._reject("atr_too_low")
                return None
        direction = "Up" if move30 > 0 else "Down"

        # If both sides are equally cheap we skip (ambiguous / possible rebalancing state).
        cheap_sides = 0
        for token_id in data.token_ids.values():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask <= self.buy_trigger:
                cheap_sides += 1
        if cheap_sides >= 2:
            self._reject("both_sides_cheap")
            return None

        token_id = data.token_ids.get(direction)
        if not token_id:
            return None
        book = data.books.get(token_id)
        ask = _best_ask(book)
        if ask is None:
            ask = _last_trade(book)
        if ask is None:
            self._reject("missing_entry_price")
            return None
        # Allow paying up (to max_entry) only when momentum is strong.
        allowed = self.buy_trigger if abs(move30) < self.momentum_trigger * 1.5 else self.max_entry
        if ask > allowed:
            self._reject("ask_not_cheap_enough")
            return None
        return self._buy_and_queue_sell(
            data,
            executor,
            token_id,
            direction,
            ask,
            "buy_then_sell_hybrid",
            {"atr": round(atr, 2) if atr is not None else None, "move_30s": round(move30, 2), "allowed_entry": round(allowed, 4)},
        )


class OrderBookImbalanceStrategy(_SingleEntryBase):
    """Entry requires threshold price and depth imbalance confirmation."""

    def __init__(self, buy_threshold_cents: int = 25, imbalance_ratio: float = 2.0, **kwargs):
        super().__init__(**kwargs)
        self.buy_trigger = buy_threshold_cents / 100.0 + 0.012
        self.imbalance_ratio = max(1.0, float(imbalance_ratio))

    @property
    def name(self) -> str:
        return "Orderbook Imbalance"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade():
            self._reject("window_trade_cap_reached")
            return None
        if not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None
        if not self._btc_guard_ok(data):
            return None
        candidates: list[tuple[float, float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            book = data.books.get(token_id)
            ask = _best_ask(book)
            if ask is None or ask > self.buy_trigger:
                continue
            bid_depth, ask_depth = _depth_near(book, width=0.02)
            ratio = (bid_depth + 1e-9) / (ask_depth + 1e-9)
            if ratio >= self.imbalance_ratio:
                candidates.append((ask, ratio, outcome, token_id))
        candidates.sort(key=lambda x: x[0])
        if not candidates:
            self._reject("no_imbalance_trigger")
            return None
        ask, ratio, outcome, token_id = candidates[0]
        return self._buy_and_queue_sell(
            data, executor, token_id, outcome, ask, "buy_then_sell_imbalance", {"imbalance": round(ratio, 2)}
        )


class LayeredLimitEntryStrategy(_SingleEntryBase):
    """
    Passive-first approximation:
    - Start a passive intent at target price
    - Wait up to timeout
    - If not filled, fallback to market-style buy at threshold (still within entry window)
    """

    def __init__(self, buy_threshold_cents: int = 25, passive_offset_cents: float = 0.1, timeout_sec: float = 30.0, **kwargs):
        super().__init__(**kwargs)
        self.buy_trigger = buy_threshold_cents / 100.0 + 0.012
        self.passive_price = max(0.01, buy_threshold_cents / 100.0 - passive_offset_cents / 100.0)
        self.timeout_sec = max(2.0, float(timeout_sec))
        self._pending: dict[str, tuple[str, float]] = {}

    @property
    def name(self) -> str:
        return "Layered Entry"

    def _on_new_window(self):
        super()._on_new_window()
        self._pending = {}

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if (not self._can_trade()) or not self._entry_window_open(data.elapsed_seconds):
            return None
        if not self._btc_guard_ok(data):
            return None
        elapsed = float(data.elapsed_seconds or 0.0)
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is None:
                continue
            if ask <= self.buy_trigger and token_id not in self._pending:
                self._pending[token_id] = (outcome, elapsed)
            if token_id not in self._pending:
                continue
            pending_outcome, started = self._pending[token_id]
            # Passive-style fill proxy.
            if ask <= self.passive_price:
                return self._buy_and_queue_sell(
                    data,
                    executor,
                    token_id,
                    pending_outcome,
                    self.passive_price,
                    "buy_then_sell_layered_passive",
                )
            # Timeout -> fallback to marketable threshold entry.
            if elapsed - started >= self.timeout_sec and ask <= self.buy_trigger:
                return self._buy_and_queue_sell(
                    data,
                    executor,
                    token_id,
                    pending_outcome,
                    ask,
                    "buy_then_sell_layered_fallback",
                )
        return None


class AdaptiveExitStrategy(_SingleEntryBase):
    """Threshold entry with staged limit repricing when momentum stalls."""

    def __init__(
        self,
        buy_threshold_cents: int = 25,
        stage1_after_sec: float = 45.0,
        stage1_target_cents: int = 38,
        stage2_after_sec: float = 75.0,
        stage2_target_cents: int = 30,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.buy_trigger = buy_threshold_cents / 100.0 + 0.012
        self.stage1_after = stage1_after_sec
        self.stage2_after = stage2_after_sec
        self.stage1_target = stage1_target_cents / 100.0
        self.stage2_target = stage2_target_cents / 100.0
        self._position_meta: Optional[dict] = None

    @property
    def name(self) -> str:
        return "Adaptive Exit"

    def _on_new_window(self):
        super()._on_new_window()
        self._position_meta = None

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)

        # Reprice pending sell if we have an open managed position.
        if self._position_meta and data.elapsed_seconds is not None:
            token_id = self._position_meta["token_id"]
            managed = self._position_meta.get("stage", 0)
            elapsed = float(data.elapsed_seconds)
            if managed == 0 and elapsed >= self.stage1_after:
                if executor.replace_pending_sell(token_id, self.stage1_target, self._position_meta["outcome"]) > 0:
                    self._position_meta["stage"] = 1
            if self._position_meta.get("stage", 0) == 1 and elapsed >= self.stage2_after:
                if executor.replace_pending_sell(token_id, self.stage2_target, self._position_meta["outcome"]) > 0:
                    self._position_meta["stage"] = 2

        if (not self._can_trade()) or not self._entry_window_open(data.elapsed_seconds):
            return None
        if not self._btc_guard_ok(data):
            return None
        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask <= self.buy_trigger:
                candidates.append((ask, outcome, token_id))
        candidates.sort(key=lambda x: x[0])
        if not candidates:
            return None
        ask, outcome, token_id = candidates[0]
        result = self._buy_and_queue_sell(data, executor, token_id, outcome, ask, "buy_then_sell_adaptive")
        if result:
            self._position_meta = {"token_id": token_id, "outcome": outcome, "stage": 0}
        return result


class SignalFusionStrategy(_SingleEntryBase):
    """Short MA dip + orderbook imbalance confirmation."""

    def __init__(
        self,
        sma_window_ticks: int = 10,
        sma_discount_cents: float = 2.0,
        imbalance_ratio: float = 1.8,
        max_entry_cents: int = 35,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.sma_window = max(3, int(sma_window_ticks))
        self.sma_discount = max(0.0, float(sma_discount_cents)) / 100.0
        self.imbalance_ratio = max(1.0, float(imbalance_ratio))
        self.max_entry = max_entry_cents / 100.0
        self._hist: dict[str, deque[float]] = {}

    @property
    def name(self) -> str:
        return "Signal Fusion"

    def _on_new_window(self):
        super()._on_new_window()
        self._hist = {}

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if (not self._can_trade()) or not self._entry_window_open(data.elapsed_seconds):
            return None
        if not self._btc_guard_ok(data):
            return None

        for token_id in data.token_ids.values():
            book = data.books.get(token_id)
            ask = _best_ask(book)
            obs = ask if ask is not None else _last_trade(book)
            if obs is None:
                continue
            if token_id not in self._hist:
                self._hist[token_id] = deque(maxlen=self.sma_window)
            self._hist[token_id].append(obs)

        candidates: list[tuple[float, float, float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            book = data.books.get(token_id)
            ask = _best_ask(book)
            hist = self._hist.get(token_id)
            if ask is None or not hist or len(hist) < self.sma_window:
                continue
            sma = sum(hist) / len(hist)
            bid_depth, ask_depth = _depth_near(book, width=0.02)
            ratio = (bid_depth + 1e-9) / (ask_depth + 1e-9)
            if ask <= self.max_entry and ask <= sma - self.sma_discount and ratio >= self.imbalance_ratio:
                candidates.append((ask, sma, ratio, outcome, token_id))
        candidates.sort(key=lambda x: x[0])
        if not candidates:
            return None
        ask, sma, ratio, outcome, token_id = candidates[0]
        return self._buy_and_queue_sell(
            data,
            executor,
            token_id,
            outcome,
            ask,
            "buy_then_sell_fusion",
            {"sma": round(sma, 4), "imbalance": round(ratio, 2)},
        )


class EndWindowMomentumStrategy(_SingleEntryBase):
    """Late-window momentum strategy (last 30-60s style)."""

    def __init__(self, late_start_sec: float = 240.0, btc_move_trigger_usd: float = 80.0, max_entry_cents: int = 60, **kwargs):
        super().__init__(entry_start_sec=late_start_sec, entry_end_sec=300.0, **kwargs)
        self.move_trigger = abs(float(btc_move_trigger_usd))
        self.max_entry = max_entry_cents / 100.0

    @property
    def name(self) -> str:
        return "End-Window Momentum"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if (not self._can_trade()) or not self._entry_window_open(data.elapsed_seconds):
            return None
        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is None or cur is None:
            self._reject("missing_reference_or_price")
            return None
        move = cur - ref
        if abs(move) < self.move_trigger:
            self._reject("move_trigger_not_met")
            return None
        desired = "Up" if move > 0 else "Down"
        token_id = data.token_ids.get(desired)
        if not token_id:
            return None
        ask = _best_ask(data.books.get(token_id))
        if ask is None or ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None
        return self._buy_and_queue_sell(
            data,
            executor,
            token_id,
            desired,
            ask,
            "buy_then_sell_end_window",
            {"btc_move": round(move, 2)},
        )


class MeanReversionExtremeStrategy(_SingleEntryBase):
    """Contrarian: buy very cheap side early if BTC move not yet extreme."""

    def __init__(self, extreme_entry_cents: int = 15, max_move_for_contrarian_usd: float = 60.0, **kwargs):
        super().__init__(**kwargs)
        self.extreme = extreme_entry_cents / 100.0
        self.max_move = abs(float(max_move_for_contrarian_usd))

    @property
    def name(self) -> str:
        return "Mean Reversion Extreme"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if (not self._can_trade()) or not self._entry_window_open(data.elapsed_seconds):
            return None
        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is not None and cur is not None and abs(cur - ref) > self.max_move:
            return None
        cheap: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask <= self.extreme:
                cheap.append((ask, outcome, token_id))
        cheap.sort(key=lambda x: x[0])
        if not cheap:
            return None
        ask, outcome, token_id = cheap[0]
        return self._buy_and_queue_sell(data, executor, token_id, outcome, ask, "buy_then_sell_mean_reversion")


class OracleLagArbProxyStrategy(_SingleEntryBase):
    """
    Oracle lag arbitrage proxy:
    - Use fast BTC 30s move from internal feed
    - Enter when move is large but market mid is still near 50/50 (stale)
    """

    def __init__(
        self,
        move_30s_trigger_usd: float = 45.0,
        stale_mid_band: float = 0.07,
        max_entry_cents: int = 75,
        use_external: bool = False,
        oracle_gap_trigger_usd: float = 5.0,
        **kwargs,
    ):
        super().__init__(entry_start_sec=210.0, entry_end_sec=300.0, **kwargs)
        self.move_trigger = abs(float(move_30s_trigger_usd))
        self.stale_band = max(0.01, float(stale_mid_band))
        self.max_entry = max_entry_cents / 100.0
        self.use_external = bool(use_external)
        self.oracle_gap_trigger = abs(float(oracle_gap_trigger_usd))

    @property
    def name(self) -> str:
        return "Oracle Lag Arb" if self.use_external else "Oracle Lag Arb (Proxy)"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade():
            self._reject("window_trade_cap_reached")
            return None
        if not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None
        move30 = data.binance_move_30s if self.use_external and data.binance_move_30s is not None else data.btc_move_30s
        if move30 is None or abs(move30) < self.move_trigger:
            self._reject("momentum_too_weak")
            return None
        gap = data.oracle_gap_usd
        if self.use_external:
            # Use oracle gap when available, but don't hard-fail if local feed lags/misses.
            if gap is not None and abs(gap) < self.oracle_gap_trigger:
                self._reject("oracle_gap_too_small")
                return None
        direction = "Up" if move30 > 0 else "Down"
        token_id = data.token_ids.get(direction)
        opp_token_id = data.token_ids.get("Down" if direction == "Up" else "Up")
        if not token_id or not opp_token_id:
            return None
        ask = _best_ask(data.books.get(token_id))
        if ask is None:
            ask = _last_trade(data.books.get(token_id))
        opp_ask = _best_ask(data.books.get(opp_token_id))
        if opp_ask is None:
            opp_ask = _last_trade(data.books.get(opp_token_id))
        if ask is None:
            self._reject("missing_entry_price")
            return None
        if opp_ask is not None:
            mid = (ask + (1.0 - opp_ask)) / 2.0
            # If market is already strongly repriced away from 50/50, only allow very strong move.
            if abs(mid - 0.5) > self.stale_band and abs(move30) < self.move_trigger * 1.5:
                self._reject("market_not_stale")
                return None
        if ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None
        return self._buy_and_queue_sell(
            data,
            executor,
            token_id,
            direction,
            ask,
            "buy_then_sell_oracle_lag",
            {
                "btc_move_30s": round(move30, 2),
                "oracle_gap_usd": round(gap, 2) if gap is not None else None,
            },
        )


class CrossMarketSentimentProxyStrategy(_SingleEntryBase):
    """Funding/cross-market proxy using BTC short-term momentum and ATR regime."""

    def __init__(
        self,
        min_regime_atr_usd: float = 35.0,
        max_entry_cents: int = 45,
        use_external: bool = False,
        min_oi_change_5m: float = 0.0002,
        min_abs_funding: float = 0.00001,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.min_regime_atr = max(1.0, float(min_regime_atr_usd))
        self.max_entry = max_entry_cents / 100.0
        self.use_external = bool(use_external)
        self.min_oi_change_5m = abs(float(min_oi_change_5m))
        self.min_abs_funding = abs(float(min_abs_funding))

    @property
    def name(self) -> str:
        return "Cross-Market Sentiment" if self.use_external else "Cross-Market Sentiment (Proxy)"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade():
            self._reject("window_trade_cap_reached")
            return None
        if not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None
        direction: Optional[str] = None
        if self.use_external:
            funding = data.funding_rate
            oi5 = data.open_interest_change_5m
            move30 = data.binance_move_30s
            if funding is None or move30 is None:
                self._reject("external_signal_missing")
                return None
            if abs(funding) < self.min_abs_funding:
                self._reject("funding_filter")
                return None
            # Directional alignment: funding sign and 30s move sign.
            if funding > 0 and move30 > 0:
                direction = "Up"
            elif funding < 0 and move30 < 0:
                direction = "Down"
            else:
                self._reject("direction_mismatch")
                return None
            # OI filter is optional when feed is warming up.
            if oi5 is not None and abs(oi5) < self.min_oi_change_5m:
                self._reject("oi_filter")
                return None
        else:
            atr = data.btc_atr_1m_10m
            move30 = data.btc_move_30s
            if atr is None or move30 is None or atr < self.min_regime_atr:
                self._reject("atr_or_momentum_filter")
                return None
            direction = "Up" if move30 > 0 else "Down"
        token_id = data.token_ids.get(direction)
        if not token_id:
            return None
        ask = _best_ask(data.books.get(token_id))
        if ask is None or ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None
        return self._buy_and_queue_sell(
            data,
            executor,
            token_id,
            direction,
            ask,
            "buy_then_sell_cross_market",
            {
                "atr": round(data.btc_atr_1m_10m, 2) if data.btc_atr_1m_10m is not None else None,
                "btc_move_30s": round(data.btc_move_30s, 2) if data.btc_move_30s is not None else None,
                "binance_move_30s": round(data.binance_move_30s, 2) if data.binance_move_30s is not None else None,
                "funding_rate": data.funding_rate,
                "oi_change_5m": data.open_interest_change_5m,
            },
        )


class MicroMarketMakingProxyStrategy(_SingleEntryBase):
    """
    Passive MM proxy:
    - only trade when spread is wide
    - enter side with stronger depth support near best levels
    - target modest spread-capture exit
    """

    def __init__(
        self,
        min_spread_cents: float = 2.0,
        spread_capture_cents: float = 4.0,
        max_entry_cents: int = 70,
        use_external: bool = False,
        min_binance_imbalance: float = 1.05,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.min_spread = min_spread_cents / 100.0
        self.capture = spread_capture_cents / 100.0
        self.max_entry = max_entry_cents / 100.0
        self.use_external = bool(use_external)
        self.min_binance_imbalance = max(1.0, float(min_binance_imbalance))

    @property
    def name(self) -> str:
        return "Micro MM" if self.use_external else "Micro MM (Proxy)"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade():
            self._reject("window_trade_cap_reached")
            return None
        if not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None
        if not self._btc_guard_ok(data):
            self._reject("btc_guard_blocked")
            return None
        if self.use_external:
            imb = data.binance_depth_imbalance
            if imb is None or imb < self.min_binance_imbalance:
                self._reject("depth_imbalance_filter")
                return None
        best: Optional[tuple[float, str, str, float]] = None
        for outcome, token_id in data.token_ids.items():
            book = data.books.get(token_id)
            ask = _best_ask(book)
            bid = _best_bid(book)
            if ask is None or bid is None or ask > self.max_entry:
                continue
            spread = ask - bid
            if spread < self.min_spread:
                continue
            bid_depth, ask_depth = _depth_near(book, width=0.02)
            score = spread + 0.01 * ((bid_depth + 1e-9) / (ask_depth + 1e-9))
            if best is None or score > best[0]:
                best = (score, outcome, token_id, ask)
        if not best:
            self._reject("no_mm_setup")
            return None
        _, outcome, token_id, ask = best
        result = self._buy_and_queue_sell(data, executor, token_id, outcome, ask, "buy_then_sell_mm_proxy")
        if result:
            # Override queued sell to a tighter spread-capture target for MM style.
            target = min(0.99, ask + self.capture)
            executor.replace_pending_sell(token_id, target, outcome)
            result["mm_target"] = round(target, 4)
        return result
