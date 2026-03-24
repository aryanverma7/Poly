"""
Additional experimental strategies for paper comparison.

These are intentionally simple, testable rule implementations of the ideas
discussed in research notes. A few signals that need external feeds in
production (oracle lag, funding/cross-market sentiment) are represented by
internal proxies so they can be A/B tested immediately in paper mode.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from executor import Executor
from strategies.base import MarketData, Strategy

logger = logging.getLogger(__name__)

_SAFE_MODE: bool = True  # toggled via /api/safe-mode


def set_safe_mode(enabled: bool) -> None:
    global _SAFE_MODE
    _SAFE_MODE = bool(enabled)


def get_safe_mode() -> bool:
    return _SAFE_MODE


SAFE_MODE_STRATEGIES = [
    "ConfirmedMomentumCarryStrategy",
    "EarlyBreakoutStrategy",
    "ExhaustionFadeStrategy",
    "HybridEarlyMomentumStrategy",
    "LateHighConfidenceStrategy",
    "MidWindowMomentumStrategy",
    "OpeningDiscountScalperStrategy",
    "OracleLagArbProxyStrategy",
    "OrderBookImbalanceStrategy",
    "PriceSkewFadeStrategy",
]


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
        self.last_confidence: float = 0.5

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
        if atr is None or atr < 30.0:
            self._reject("atr_regime_flat")
            return None
        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask <= self.buy_trigger:
                candidates.append((ask, outcome, token_id))
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        if move30 is not None and abs(move30) > 20:
            direction = "Up" if move30 > 0 else "Down"
            candidates = [(e, o, t) for e, o, t in candidates if o == direction]
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
        entry_end_sec: float = 200.0,
        **kwargs,
    ):
        super().__init__(
            entry_start_sec=0.0,
            entry_end_sec=entry_end_sec,
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
        if _SAFE_MODE and ask < 0.13:
            self._reject("ask_below_floor")
            return None
        allowed = self.buy_trigger
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
            if ask is None or _SAFE_MODE and ask < 0.13 or ask > self.buy_trigger:
                continue
            bid_depth, ask_depth = _depth_near(book, width=0.02)
            ratio = (bid_depth + 1e-9) / (ask_depth + 1e-9)
            if ratio >= self.imbalance_ratio:
                candidates.append((ask, ratio, outcome, token_id))
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        if move30 is not None and abs(move30) > 20:
            direction = "Up" if move30 > 0 else "Down"
            candidates = [(a, r, o, t) for a, r, o, t in candidates if o == direction]
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

    def __init__(self, buy_threshold_cents: int = 25, passive_offset_cents: float = 1.0, timeout_sec: float = 20.0, **kwargs):
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
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        allowed_dir = None
        if move30 is not None and abs(move30) > 20:
            allowed_dir = "Up" if move30 > 0 else "Down"
        for outcome, token_id in data.token_ids.items():
            if allowed_dir and outcome != allowed_dir:
                continue
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
        stage1_after_sec: float = 60.0,
        stage1_target_cents: int = 38,
        stage2_after_sec: float = 120.0,
        stage2_target_cents: int = 30,
        stage3_after_sec: float = 150.0,
        stage3_target_cents: int = 20,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.buy_trigger = buy_threshold_cents / 100.0 + 0.012
        self.stage1_after = stage1_after_sec
        self.stage2_after = stage2_after_sec
        self.stage3_after = stage3_after_sec
        self.stage1_target = stage1_target_cents / 100.0
        self.stage2_target = stage2_target_cents / 100.0
        self.stage3_target = stage3_target_cents / 100.0
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
            if self._position_meta.get("stage", 0) == 2 and elapsed >= self.stage3_after:
                if executor.replace_pending_sell(token_id, self.stage3_target, self._position_meta["outcome"]) > 0:
                    self._position_meta["stage"] = 3

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
            if ask >= 0.13 and ask <= self.max_entry and ask <= sma - self.sma_discount and ratio >= self.imbalance_ratio:
                candidates.append((ask, sma, ratio, outcome, token_id))
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        if move30 is not None and abs(move30) > 15:
            direction = "Up" if move30 > 0 else "Down"
            candidates = [(a, s, r, o, t) for a, s, r, o, t in candidates if o == direction]
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

    def __init__(self, extreme_entry_cents: int = 12, max_move_for_contrarian_usd: float = 25.0, **kwargs):
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
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        cheap: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask <= self.extreme:
                if move30 is not None and abs(move30) > 15 and ref is not None and cur is not None:
                    contra = "Down" if (cur - ref) > 0 else "Up"
                    if outcome != contra:
                        continue
                cheap.append((ask, outcome, token_id))
        cheap.sort(key=lambda x: x[0])
        if not cheap:
            self._reject("no_reversal_setup")
            return None
        ask, outcome, token_id = cheap[0]
        return self._buy_and_queue_sell(data, executor, token_id, outcome, ask, "buy_then_sell_mean_reversion")


class RebalancingArbStrategy(_SingleEntryBase):
    """Buy cheaper side when Up+Down combined ask < threshold. Direction-neutral."""

    def __init__(self, combined_ask_max: float = 0.96, **kwargs):
        super().__init__(**kwargs)
        self.combined_max = combined_ask_max

    @property
    def name(self) -> str:
        return "Rebalancing Arb"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        asks: dict[str, tuple[float, str]] = {}
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None:
                asks[outcome] = (ask, token_id)

        if len(asks) < 2:
            self._reject("missing_book")
            return None

        combined = sum(a for a, _ in asks.values())
        if combined >= self.combined_max:
            self._reject("no_arb_gap")
            return None

        cheapest = min(asks.items(), key=lambda x: x[1][0])
        outcome, (ask, token_id) = cheapest
        other_ask = [a for o, (a, _) in asks.items() if o != outcome][0]
        sell_target = min(0.97, max(self.sell_limit, 1.0 - other_ask - 0.01))

        result = self._buy_and_queue_sell(
            data, executor, token_id, outcome, ask,
            "buy_then_sell_rebalancing_arb",
            {"combined_asks": round(combined, 4), "arb_gap": round(1.0 - combined, 4)},
        )
        if result:
            executor.replace_pending_sell(token_id, sell_target, outcome)
            result["sell_limit"] = round(sell_target, 4)
        return result


class OpeningDiscountScalperStrategy(_SingleEntryBase):
    """First 45s only. Buy <=20c, sell at 55c. Tighter entry, bigger margin."""

    def __init__(self, max_entry_cents: int = 20, sell_target_cents: int = 55, **kwargs):
        kwargs.setdefault("entry_end_sec", 45.0)
        super().__init__(**kwargs)
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Opening Discount Scalper"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None
        if not self._btc_guard_ok(data):
            self._reject("btc_guard_blocked")
            return None

        move30 = getattr(data, "binance_move_30s", None) or getattr(data, "btc_move_30s", None)
        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is None or _SAFE_MODE and ask < 0.08 or ask > self.max_entry:
                continue
            if move30 is not None and abs(move30) > 15:
                direction = "Up" if move30 > 0 else "Down"
                if outcome != direction:
                    continue
            candidates.append((ask, outcome, token_id))

        if not candidates:
            self._reject("no_discount_found")
            return None

        ask, outcome, token_id = min(candidates, key=lambda x: x[0])
        result = self._buy_and_queue_sell(
            data, executor, token_id, outcome, ask,
            "buy_then_sell_opening_discount",
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, outcome)
            result["sell_limit"] = self.sell_target
        return result


class ExhaustionFadeStrategy(_SingleEntryBase):
    """
    BTC moved far from window reference AND the last 30s has reversed.
    Only enters on confirmed reversal, not just cheapness.
    """

    def __init__(self, min_exhaustion_usd: float = 60.0, max_entry_cents: int = 30, **kwargs):
        super().__init__(**kwargs)
        self.min_exhaustion = abs(float(min_exhaustion_usd))
        self.max_entry = max_entry_cents / 100.0

    @property
    def name(self) -> str:
        return "Exhaustion Fade"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = getattr(data, "binance_move_30s", None) or getattr(data, "btc_move_30s", None)

        if ref is None or cur is None or move30 is None:
            self._reject("missing_data")
            return None

        window_move = cur - ref
        if abs(window_move) < self.min_exhaustion:
            self._reject("not_exhausted")
            return None

        if window_move > 0 and move30 >= 0:
            self._reject("no_reversal_yet")
            return None
        if window_move < 0 and move30 <= 0:
            self._reject("no_reversal_yet")
            return None

        fade_direction = "Down" if window_move > 0 else "Up"
        token_id = data.token_ids.get(fade_direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None or ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None
        if _SAFE_MODE and ask < 0.10:
            self._reject("ask_too_cheap_reversal_unlikely")
            return None

        return self._buy_and_queue_sell(
            data, executor, token_id, fade_direction, ask,
            "buy_then_sell_exhaustion_fade",
            {"window_move": round(window_move, 2), "reversal_30s": round(move30, 2)},
        )


class WindowMomentumCarryStrategy(_SingleEntryBase):
    """
    If previous window resolved strongly (one side > 80c),
    bet same direction in next window opening. Markets trend.
    """

    def __init__(self, min_resolve_price: float = 0.80, max_entry_cents: int = 35, **kwargs):
        kwargs.setdefault("entry_end_sec", 60.0)
        super().__init__(**kwargs)
        self.min_resolve = min_resolve_price
        self.max_entry = max_entry_cents / 100.0
        self._last_resolved_direction: Optional[str] = None
        self._last_resolved_price: float = 0.0

    @property
    def name(self) -> str:
        return "Window Momentum Carry"

    def set_last_resolution(self, direction: str, price: float):
        if price >= self.min_resolve:
            self._last_resolved_direction = direction
            self._last_resolved_price = price
        else:
            self._last_resolved_direction = None

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        direction = self._last_resolved_direction
        if not direction:
            self._reject("no_prior_momentum")
            return None

        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None or ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None
        if not self._btc_guard_ok(data):
            self._reject("btc_guard_blocked")
            return None

        return self._buy_and_queue_sell(
            data, executor, token_id, direction, ask,
            "buy_then_sell_momentum_carry",
            {"prior_direction": direction, "prior_price": self._last_resolved_price},
        )


class FundingTrendFollowerStrategy(_SingleEntryBase):
    """
    Triple-confirmed: funding sign + OI change + 30s move all agree.
    Enter at <=40c, target 65c.
    """

    def __init__(
        self,
        min_abs_funding: float = 0.00001,
        min_oi_change_5m: float = 0.0001,
        max_entry_cents: int = 40,
        sell_target_cents: int = 65,
        **kwargs,
    ):
        kwargs.setdefault("entry_end_sec", 240.0)
        super().__init__(**kwargs)
        self.min_funding = abs(float(min_abs_funding))
        self.min_oi = abs(float(min_oi_change_5m))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Funding Trend Follower"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        funding = data.funding_rate
        oi5 = data.open_interest_change_5m
        move30 = getattr(data, "binance_move_30s", None) or getattr(data, "btc_move_30s", None)

        if move30 is None:
            self._reject("missing_move_data")
            return None

        direction: Optional[str] = None
        if funding is not None:
            # Full external signal path
            if abs(funding) < self.min_funding:
                self._reject("funding_too_weak")
                return None
            if funding > 0 and move30 > 5:
                direction = "Up"
            elif funding < 0 and move30 < -5:
                direction = "Down"
            else:
                self._reject("direction_mismatch")
                return None
            if oi5 is not None and abs(oi5) < self.min_oi:
                self._reject("oi_too_flat")
                return None
        else:
            # Internal fallback: ATR + window move + 30s move all confirm same direction
            atr = data.btc_atr_1m_10m
            ref = data.reference_btc_price
            cur = data.current_btc_price
            if atr is None or atr < 30.0:
                self._reject("fallback_atr_too_low")
                return None
            if ref is None or cur is None or abs(cur - ref) < 20.0:
                self._reject("fallback_window_move_weak")
                return None
            if abs(move30) < 20.0:
                self._reject("fallback_move_30s_weak")
                return None
            window_move = cur - ref
            if (move30 > 0) != (window_move > 0):
                self._reject("fallback_direction_conflict")
                return None
            direction = "Up" if move30 > 0 else "Down"

        if not self._btc_guard_ok(data):
            self._reject("btc_guard_blocked")
            return None

        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None or ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None

        result = self._buy_and_queue_sell(
            data, executor, token_id, direction, ask,
            "buy_then_sell_funding_trend",
            {"funding": round(funding, 7), "oi5": round(oi5, 5) if oi5 else None, "move30": round(move30, 2)},
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, direction)
            result["sell_limit"] = self.sell_target
        return result


class OracleLagArbProxyStrategy(_SingleEntryBase):
    """
    Oracle lag arbitrage: BTC has spiked, Polymarket hasn't repriced yet.

    Fixes vs original:
    - Entry window T=60-200s only (lag can't exist after T=200s — market has
      had 3+ min to reprice; late cheap tokens are cheap for a reason)
    - Requires window move (from reference BTC) to agree with 30s direction
    - Price fairness check: token must be >= 18c below model fair price
    - Removed stale-band check (was allowing 67c Down buys at T=250s)
    """

    def __init__(
        self,
        move_30s_trigger_usd: float = 45.0,
        max_entry_cents: int = 55,
        use_external: bool = False,
        oracle_gap_trigger_usd: float = 5.0,
        entry_start_sec: float = 60.0,
        entry_end_sec: float = 200.0,
        min_profit_margin: float = 0.20,
        **kwargs,
    ):
        super().__init__(entry_start_sec=entry_start_sec, entry_end_sec=entry_end_sec, **kwargs)
        self.move_trigger = abs(float(move_30s_trigger_usd))
        self.max_entry = max_entry_cents / 100.0
        self.use_external = bool(use_external)
        self.oracle_gap_trigger = abs(float(oracle_gap_trigger_usd))
        self.min_profit_margin = max(0.05, float(min_profit_margin))

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

        # Bug 2 fix: window move must exist and agree with 30s direction
        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is None or cur is None:
            self._reject("missing_btc_ref")
            return None
        window_move = cur - ref
        if abs(window_move) < 30.0:
            self._reject("window_move_too_small")
            return None
        if (window_move > 0) != (move30 > 0):
            self._reject("window_and_30s_disagree")
            return None

        gap = data.oracle_gap_usd
        if self.use_external and gap is not None and abs(gap) < self.oracle_gap_trigger:
            self._reject("oracle_gap_too_small")
            return None

        # Direction from window move (authoritative), not just 30s spike
        direction = "Up" if window_move > 0 else "Down"
        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None:
            ask = _last_trade(data.books.get(token_id))
        if ask is None:
            self._reject("missing_entry_price")
            return None
        if _SAFE_MODE and ask < 0.13:
            self._reject("ask_below_floor")
            return None
        if ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None

        # Bug 3 fix: price fairness — token must be genuinely cheap vs window move
        # Model: fair_price ≈ 0.50 + |window_move| / 350  (capped 0.05-0.95)
        expected_price = min(0.95, max(0.05, 0.50 + abs(window_move) / 350.0))
        if ask > expected_price - 0.18:
            self._reject("market_already_repriced")
            return None

        conf = min(1.0, max(0.5, abs(gap) / 20.0)) if gap is not None else 0.7
        self.last_confidence = conf
        dynamic_sell = min(0.97, max(self.sell_limit, ask + self.min_profit_margin))
        result = self._buy_and_queue_sell(
            data,
            executor,
            token_id,
            direction,
            ask,
            "buy_then_sell_oracle_lag",
            {
                "btc_move_30s": round(move30, 2),
                "window_move": round(window_move, 2),
                "expected_price": round(expected_price, 3),
                "oracle_gap_usd": round(gap, 2) if gap is not None else None,
                "confidence": round(conf, 2),
                "dynamic_sell": round(dynamic_sell, 4),
            },
        )
        if result:
            executor.replace_pending_sell(token_id, dynamic_sell, direction)
            result["sell_limit"] = round(dynamic_sell, 4)
        return result


class CrossMarketSentimentProxyStrategy(_SingleEntryBase):
    """Funding/cross-market proxy using BTC short-term momentum and ATR regime."""

    def __init__(
        self,
        min_regime_atr_usd: float = 15.0,
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
        if self.use_external and data.external_last_ws_at is not None:
            from datetime import datetime as _dt, timezone as _tz
            try:
                age = (_dt.now(_tz.utc) - _dt.fromisoformat(data.external_last_ws_at)).total_seconds()
                if age > 60:
                    self._reject("ws_data_stale")
                    return None
            except Exception:
                pass
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
            if move30 is None:
                self._reject("momentum_missing")
                return None
            if atr is not None and atr < self.min_regime_atr:
                self._reject("atr_regime_low")
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



class LateHighConfidenceStrategy(_SingleEntryBase):
    """
    Late-window high-confidence oracle-lag play.
    - Entry: T=220-290s (only last 70 seconds)
    - BTC must have moved > $60 from window start (clear directional conviction)
    - Buy the winning side even at up to 78¢ — it resolves at $1.00 in seconds
    - Chainlink oracle lags 30min; market makers price stale. 70¢ when fair is 90¢ = edge.
    """

    def __init__(
        self,
        late_start_sec: float = 220.0,
        btc_move_trigger_usd: float = 60.0,
        max_entry_cents: int = 78,
        sell_target_cents: int = 93,
        **kwargs,
    ):
        super().__init__(entry_start_sec=late_start_sec, entry_end_sec=290.0, **kwargs)
        self.move_trigger = abs(float(btc_move_trigger_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Late High Confidence"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is None or cur is None:
            self._reject("missing_btc_price")
            return None

        window_move = cur - ref
        if abs(window_move) < self.move_trigger:
            self._reject("move_trigger_not_met")
            return None

        # Also check 30s momentum still agrees (not reversing)
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        if move30 is not None and abs(move30) > 10 and (move30 > 0) != (window_move > 0):
            self._reject("momentum_reversing")
            return None

        direction = "Up" if window_move > 0 else "Down"
        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None:
            self._reject("missing_price")
            return None
        # Must be in oracle-lag zone: cheap enough to have edge, not so cheap market disagrees
        if _SAFE_MODE and ask < 0.35:
            self._reject("ask_too_low_reversal_risk")
            return None
        if ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None

        result = self._buy_and_queue_sell(
            data, executor, token_id, direction, ask,
            "buy_then_sell_late_confidence",
            {"btc_move": round(window_move, 2), "elapsed": round(float(data.elapsed_seconds or 0), 1)},
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, direction)
            result["sell_limit"] = self.sell_target
        return result


class MidWindowMomentumStrategy(_SingleEntryBase):
    """
    Mid-window dual momentum confirmation.
    Both the 30s BTC move AND total window move must agree in direction.
    Enters when market likely lags the established trend.
    - Entry: T=60-180s
    - Need 30s move > $20 AND window move > $25 in same direction
    - Buy cheap side (≤ 40¢), target 65¢
    """

    def __init__(
        self,
        move_30s_min_usd: float = 20.0,
        window_move_min_usd: float = 25.0,
        max_entry_cents: int = 40,
        sell_target_cents: int = 65,
        **kwargs,
    ):
        super().__init__(entry_start_sec=60.0, entry_end_sec=180.0, **kwargs)
        self.move_30s_min = abs(float(move_30s_min_usd))
        self.window_move_min = abs(float(window_move_min_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Mid-Window Momentum"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        if ref is None or cur is None or move30 is None:
            self._reject("missing_data")
            return None

        window_move = cur - ref
        if abs(window_move) < self.window_move_min:
            self._reject("window_move_too_small")
            return None
        if abs(move30) < self.move_30s_min:
            self._reject("move_30s_too_small")
            return None
        # Both must agree in direction (trend continuation, not reversal)
        if (window_move > 0) != (move30 > 0):
            self._reject("direction_conflict")
            return None

        direction = "Up" if window_move > 0 else "Down"
        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None or ask > self.max_entry:
            self._reject("ask_too_expensive")
            return None
        if _SAFE_MODE and ask < 0.13:
            self._reject("ask_below_floor")
            return None

        result = self._buy_and_queue_sell(
            data, executor, token_id, direction, ask,
            "buy_then_sell_mid_momentum",
            {"window_move": round(window_move, 2), "move_30s": round(move30, 2)},
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, direction)
            result["sell_limit"] = self.sell_target
        return result


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
            atr = data.btc_atr_1m_10m
            dynamic_capture = max(self.capture, (atr * 0.0005) if atr else self.capture)
            target = min(0.99, ask + dynamic_capture)
            executor.replace_pending_sell(token_id, target, outcome)
            result["mm_target"] = round(target, 4)
        return result


class FlatMarketMeanReversionStrategy(_SingleEntryBase):
    """
    Flat/range-bound market edge: BTC is barely moving, but market makers have
    priced one side cheap (<= 44c). In a true sideways regime, the cheap side
    is mispriced — BTC is equally likely to go either way.

    Three flatness guards must pass:
      1. ATR < max_atr_usd (low-vol regime)
      2. |window_move| < max_window_move_usd (BTC hasn't committed to a direction)
      3. |move_30s| < max_move_30s_usd (no recent momentum)

    Entry: T=0–220s | Buy ≤ 44¢ | Sell target: 68¢
    """

    def __init__(
        self,
        max_atr_usd: float = 40.0,
        max_window_move_usd: float = 25.0,
        max_move_30s_usd: float = 20.0,
        max_entry_cents: int = 44,
        sell_target_cents: int = 68,
        **kwargs,
    ):
        kwargs.setdefault("entry_end_sec", 220.0)
        super().__init__(**kwargs)
        self.max_atr = abs(float(max_atr_usd))
        self.max_window_move = abs(float(max_window_move_usd))
        self.max_move_30s = abs(float(max_move_30s_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Flat Market Mean Reversion"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        atr = data.btc_atr_1m_10m
        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s

        # Reject if ATR is high (we're in a trending/volatile regime)
        if atr is not None and atr > self.max_atr:
            self._reject("atr_too_high")
            return None

        # BTC must not have moved much from window start
        if ref is not None and cur is not None and abs(cur - ref) > self.max_window_move:
            self._reject("window_move_too_large")
            return None

        # No recent 30s momentum
        if move30 is not None and abs(move30) > self.max_move_30s:
            self._reject("momentum_too_strong")
            return None

        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask >= 0.10 and ask <= self.max_entry:
                candidates.append((ask, outcome, token_id))

        if not candidates:
            self._reject("no_cheap_side")
            return None

        candidates.sort(key=lambda x: x[0])
        ask, outcome, token_id = candidates[0]

        window_move = round(cur - ref, 2) if ref is not None and cur is not None else None
        result = self._buy_and_queue_sell(
            data, executor, token_id, outcome, ask,
            "buy_flat_market_mean_reversion",
            {
                "atr": round(atr, 2) if atr is not None else None,
                "window_move": window_move,
                "move_30s": round(move30, 2) if move30 is not None else None,
            },
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, outcome)
            result["sell_limit"] = self.sell_target
        return result


class ConfirmedFlatScalperStrategy(_SingleEntryBase):
    """
    Waits for mid-window confirmation that BTC is genuinely range-bound,
    then scalps the mispriced side with a higher sell target.

    Unlike FlatMarketMeanReversion (which fires early), this strategy waits
    until T=60s to confirm BTC hasn't trended. By mid-window, sustained
    flatness is strong evidence the cheap side is worth buying.

    All three flatness conditions are tighter than FlatMarketMeanReversion:
      - ATR < 30 (very flat regime)
      - |window_move| < 18 (nearly zero directional bias)
      - |move_30s| < 12 (no micro-momentum)

    Entry: T=60–200s | Buy ≤ 46¢ | Sell target: 72¢
    """

    def __init__(
        self,
        max_atr_usd: float = 30.0,
        max_window_move_usd: float = 18.0,
        max_move_30s_usd: float = 12.0,
        max_entry_cents: int = 46,
        sell_target_cents: int = 72,
        **kwargs,
    ):
        super().__init__(entry_start_sec=60.0, entry_end_sec=200.0, **kwargs)
        self.max_atr = abs(float(max_atr_usd))
        self.max_window_move = abs(float(max_window_move_usd))
        self.max_move_30s = abs(float(max_move_30s_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Confirmed Flat Scalper"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        atr = data.btc_atr_1m_10m
        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s

        # All three flatness guards — tighter than FlatMarketMeanReversion
        if atr is not None and atr > self.max_atr:
            self._reject("atr_too_high")
            return None

        if ref is not None and cur is not None and abs(cur - ref) > self.max_window_move:
            self._reject("window_move_too_large")
            return None

        if move30 is None:
            self._reject("missing_move_data")
            return None
        if abs(move30) > self.max_move_30s:
            self._reject("momentum_too_strong")
            return None

        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask >= 0.10 and ask <= self.max_entry:
                candidates.append((ask, outcome, token_id))

        if not candidates:
            self._reject("no_cheap_side")
            return None

        candidates.sort(key=lambda x: x[0])
        ask, outcome, token_id = candidates[0]

        window_move = round(cur - ref, 2) if ref is not None and cur is not None else None
        result = self._buy_and_queue_sell(
            data, executor, token_id, outcome, ask,
            "buy_confirmed_flat_scalper",
            {
                "atr": round(atr, 2) if atr is not None else None,
                "window_move": window_move,
                "move_30s": round(move30, 2),
                "elapsed": round(float(data.elapsed_seconds or 0), 1),
            },
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, outcome)
            result["sell_limit"] = self.sell_target
        return result



class PriceSkewFadeStrategy(_SingleEntryBase):
    """
    Flat-market strategy: fades unjustified directional price skew.

    In a flat BTC window, Up and Down contracts should trade close to 50/50.
    When one side is significantly more expensive (skew >= min_skew_cents),
    the market is mis-pricing directional confidence. Buy the cheap side.

    Entry: 30-200s into window.
    Condition: |Up_ask - Down_ask| >= min_skew_cents AND market is flat.
    """

    max_trades_per_window: int = 1

    def __init__(
        self,
        *,
        min_skew_cents: float = 10.0,
        max_atr_usd: float = 40.0,
        max_window_move_usd: float = 25.0,
        max_move_30s_usd: float = 18.0,
        max_entry_cents: int = 47,
        sell_target_cents: int = 65,
        **kwargs,
    ):
        super().__init__(entry_start_sec=30.0, entry_end_sec=200.0, **kwargs)
        self.min_skew = min_skew_cents / 100.0
        self.max_atr = abs(float(max_atr_usd))
        self.max_window_move = abs(float(max_window_move_usd))
        self.max_move_30s = abs(float(max_move_30s_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Price Skew Fade"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        atr = data.btc_atr_1m_10m
        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s

        if atr is not None and atr > self.max_atr:
            self._reject("atr_too_high")
            return None

        if ref is not None and cur is not None and abs(cur - ref) > self.max_window_move:
            self._reject("window_move_too_large")
            return None

        if move30 is None:
            self._reject("missing_move_data")
            return None
        if abs(move30) > self.max_move_30s:
            self._reject("momentum_too_strong")
            return None

        outcome_asks: dict[str, float] = {}
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None:
                outcome_asks[outcome] = ask

        if len(outcome_asks) < 2:
            self._reject("missing_book")
            return None

        asks_list = list(outcome_asks.items())
        skew = abs(asks_list[0][1] - asks_list[1][1])

        if skew < self.min_skew:
            self._reject("skew_too_small")
            return None

        cheap_outcome, cheap_ask = min(asks_list, key=lambda x: x[1])
        expensive_outcome, expensive_ask = max(asks_list, key=lambda x: x[1])

        if _SAFE_MODE and cheap_ask < 0.10:
            self._reject("ask_too_cheap_no_edge")
            return None
        if cheap_ask > self.max_entry:
            self._reject("cheap_side_too_expensive")
            return None

        token_id = data.token_ids[cheap_outcome]
        result = self._buy_and_queue_sell(
            data, executor, token_id, cheap_outcome, cheap_ask,
            "buy_price_skew_fade",
            {
                "skew": round(skew * 100, 1),
                "cheap_ask": round(cheap_ask * 100, 1),
                "expensive_ask": round(expensive_ask * 100, 1),
                "cheap_outcome": cheap_outcome,
                "move_30s": round(move30, 2),
                "elapsed": round(float(data.elapsed_seconds or 0), 1),
            },
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, cheap_outcome)
            result["sell_limit"] = self.sell_target
        return result


class LateFlatBetStrategy(_SingleEntryBase):
    """
    Late-window end-game strategy for confirmed flat BTC windows.

    By T=240s with a flat BTC window, 4+ minutes of confirmed flatness is strong
    evidence the window resolves near the reference price. The cheap side (<=50c)
    has positive EV since neither Up nor Down is favored, but one will pay $1.

    Entry: 240-285s (last ~1 minute of the 5-min window).
    Condition: BTC has moved < max_window_move_usd for entire window AND
               recent 30s move < max_move_30s_usd.
    """

    max_trades_per_window: int = 1

    def __init__(
        self,
        *,
        max_window_move_usd: float = 15.0,
        max_move_30s_usd: float = 10.0,
        max_entry_cents: int = 50,
        sell_target_cents: int = 90,
        **kwargs,
    ):
        super().__init__(entry_start_sec=240.0, entry_end_sec=285.0, **kwargs)
        self.max_window_move = abs(float(max_window_move_usd))
        self.max_move_30s = abs(float(max_move_30s_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Late Flat Bet"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s

        if ref is not None and cur is not None and abs(cur - ref) > self.max_window_move:
            self._reject("window_move_too_large")
            return None

        if move30 is None:
            self._reject("missing_move_data")
            return None
        if abs(move30) > self.max_move_30s:
            self._reject("momentum_too_strong")
            return None

        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            ask = _best_ask(data.books.get(token_id))
            if ask is not None and ask >= 0.15 and ask <= self.max_entry:
                candidates.append((ask, outcome, token_id))

        if not candidates:
            self._reject("no_cheap_side")
            return None

        candidates.sort(key=lambda x: x[0])
        ask, outcome, token_id = candidates[0]

        window_move = round(cur - ref, 2) if ref is not None and cur is not None else None
        result = self._buy_and_queue_sell(
            data, executor, token_id, outcome, ask,
            "buy_late_flat_bet",
            {
                "window_move": window_move,
                "move_30s": round(move30, 2),
                "elapsed": round(float(data.elapsed_seconds or 0), 1),
            },
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, outcome)
            result["sell_limit"] = self.sell_target
        return result


class EarlyBreakoutStrategy(_SingleEntryBase):
    """
    Catches BTC breakout momentum in the first 75s of a window.

    When BTC spikes hard (>$40 in 30s) within the first minute, Polymarket
    market makers take 30-90 seconds to fully reprice. Enter before they do.
    Requires 30s move AND window move to agree (rules out isolated spikes).

    Entry: T=0-75s | |move_30s| > $40 | window move agrees | Buy <= 40c | Sell 70c
    """

    def __init__(
        self,
        move_30s_trigger_usd: float = 40.0,
        max_entry_cents: int = 40,
        sell_target_cents: int = 70,
        **kwargs,
    ):
        kwargs.setdefault("entry_end_sec", 75.0)
        super().__init__(entry_start_sec=0.0, **kwargs)
        self.move_trigger = abs(float(move_30s_trigger_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Early Breakout"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s
        if move30 is None or abs(move30) < self.move_trigger:
            self._reject("move_too_weak")
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is not None and cur is not None and data.elapsed_seconds and data.elapsed_seconds > 10:
            window_move = cur - ref
            if abs(window_move) > 5 and (window_move > 0) != (move30 > 0):
                self._reject("early_reversal_signal")
                return None

        direction = "Up" if move30 > 0 else "Down"
        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None or _SAFE_MODE and ask < 0.13 or ask > self.max_entry:
            self._reject("ask_out_of_range")
            return None

        result = self._buy_and_queue_sell(
            data, executor, token_id, direction, ask,
            "buy_early_breakout",
            {"move_30s": round(move30, 2), "elapsed": round(float(data.elapsed_seconds or 0), 1)},
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, direction)
            result["sell_limit"] = self.sell_target
        return result


class ConfirmedMomentumCarryStrategy(_SingleEntryBase):
    """
    Mid-window momentum confirmation: BTC moved >$35 from window start AND
    30s move still agrees -- the trend is established, not just a spike.

    Enter the trending side if still underpriced relative to the move.
    Fair-price model: 0.50 + |window_move| / 350 -- must have >= 10c discount.

    Entry: T=90-180s | window_move > $35 | 30s agrees | Buy <= 55c | Sell 80c
    """

    def __init__(
        self,
        min_window_move_usd: float = 35.0,
        min_move_30s_usd: float = 10.0,
        max_entry_cents: int = 55,
        sell_target_cents: int = 80,
        **kwargs,
    ):
        super().__init__(entry_start_sec=90.0, entry_end_sec=180.0, **kwargs)
        self.min_window_move = abs(float(min_window_move_usd))
        self.min_move_30s = abs(float(min_move_30s_usd))
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Confirmed Momentum Carry"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s

        if ref is None or cur is None or move30 is None:
            self._reject("missing_data")
            return None

        window_move = cur - ref
        if abs(window_move) < self.min_window_move:
            self._reject("window_move_too_small")
            return None
        if abs(move30) < self.min_move_30s:
            self._reject("move_30s_too_weak")
            return None
        if (window_move > 0) != (move30 > 0):
            self._reject("trend_reversing")
            return None

        direction = "Up" if window_move > 0 else "Down"
        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None or _SAFE_MODE and ask < 0.13 or ask > self.max_entry:
            self._reject("ask_out_of_range")
            return None

        expected = min(0.95, 0.50 + abs(window_move) / 350.0)
        if ask > expected - 0.10:
            self._reject("market_already_repriced")
            return None

        result = self._buy_and_queue_sell(
            data, executor, token_id, direction, ask,
            "buy_confirmed_momentum_carry",
            {"window_move": round(window_move, 2), "move_30s": round(move30, 2),
             "expected_price": round(expected, 3)},
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, direction)
            result["sell_limit"] = self.sell_target
        return result


class SustainedTrendLockInStrategy(_SingleEntryBase):
    """
    Late-window sustained trend: BTC moved >$60 from window start AND still
    trending (30s move agrees). Market sometimes lags at 60-75c when fair
    value is 80-90c.

    Requires both FULL window trend AND recent momentum to be large and aligned.
    Refuses when trend is stalling or reversing.

    Entry: T=180-260s | window_move > $60 | 30s agrees | Buy 0.40-0.75 | Sell 0.90
    """

    def __init__(
        self,
        min_window_move_usd: float = 60.0,
        min_move_30s_usd: float = 15.0,
        min_entry_cents: int = 40,
        max_entry_cents: int = 75,
        sell_target_cents: int = 90,
        **kwargs,
    ):
        super().__init__(entry_start_sec=180.0, entry_end_sec=260.0, **kwargs)
        self.min_window_move = abs(float(min_window_move_usd))
        self.min_move_30s = abs(float(min_move_30s_usd))
        self.min_entry = min_entry_cents / 100.0
        self.max_entry = max_entry_cents / 100.0
        self.sell_target = sell_target_cents / 100.0

    @property
    def name(self) -> str:
        return "Sustained Trend Lock-In"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        self._reset_if_new_window(slug)
        if not self._can_trade() or not self._entry_window_open(data.elapsed_seconds):
            self._reject("entry_window_closed")
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        move30 = data.binance_move_30s if data.binance_move_30s is not None else data.btc_move_30s

        if ref is None or cur is None or move30 is None:
            self._reject("missing_data")
            return None

        window_move = cur - ref
        if abs(window_move) < self.min_window_move:
            self._reject("window_move_too_small")
            return None
        if abs(move30) < self.min_move_30s:
            self._reject("trend_stalling")
            return None
        if (window_move > 0) != (move30 > 0):
            self._reject("trend_reversing")
            return None

        direction = "Up" if window_move > 0 else "Down"
        token_id = data.token_ids.get(direction)
        if not token_id:
            return None

        ask = _best_ask(data.books.get(token_id))
        if ask is None or ask < self.min_entry or ask > self.max_entry:
            self._reject("ask_out_of_range")
            return None

        result = self._buy_and_queue_sell(
            data, executor, token_id, direction, ask,
            "buy_sustained_trend_lock_in",
            {"window_move": round(window_move, 2), "move_30s": round(move30, 2)},
        )
        if result:
            executor.replace_pending_sell(token_id, self.sell_target, direction)
            result["sell_limit"] = self.sell_target
        return result
