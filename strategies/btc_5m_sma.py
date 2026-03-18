"""
BTC 5m short moving-average strategy:
- Entry window: first N seconds (default 120)
- Build short MA on observed ask/last prices
- Buy once per window when current ask dips below MA by configured cents
- Place limit sell at configured sell limit
"""
import logging
from collections import deque
from typing import Optional

from executor import Executor
from strategies.base import MarketData, Strategy

logger = logging.getLogger(__name__)


class Btc5mSmaStrategy(Strategy):
    def __init__(
        self,
        sell_limit_cents: int = 40,
        max_btc_move_usd: float = 100,
        time_window_seconds: int = 120,
        buy_amount_usd: float = 5.0,
        sma_window_ticks: int = 10,
        sma_discount_cents: float = 2.0,
        sma_max_entry_cents: int = 35,
        max_trades_per_window: int = 1,
    ):
        self.sell_limit = sell_limit_cents / 100.0
        self.max_btc_move = max_btc_move_usd
        self.time_window_seconds = time_window_seconds
        self.buy_amount_usd = buy_amount_usd
        self.sma_window_ticks = max(3, int(sma_window_ticks))
        self.sma_discount = max(0.0, float(sma_discount_cents)) / 100.0
        self.max_entry = max(1, int(sma_max_entry_cents)) / 100.0
        self.max_trades_per_window = max(1, int(max_trades_per_window))
        self._last_window_slug: Optional[str] = None
        self._trades_this_window: int = 0
        self._history: dict[str, deque[float]] = {}
        self.last_rejection_reason: str = ""

    @property
    def name(self) -> str:
        return "Btc5mSmaStrategy"

    @staticmethod
    def _book_last(book) -> Optional[float]:
        if book is None:
            return None
        last = getattr(book, "last_trade_price", None)
        if last is not None:
            try:
                return float(last)
            except (TypeError, ValueError):
                pass
        return None

    @staticmethod
    def _best_ask(book) -> Optional[float]:
        if book is None:
            return None
        if hasattr(book, "best_ask") and book.best_ask is not None:
            return float(book.best_ask)
        if isinstance(book, dict) and book.get("asks"):
            a = book["asks"][0]
            return float(a.get("price", a) if isinstance(a, dict) else a)
        return None

    def _reset_window(self, token_ids: dict[str, str]) -> None:
        self._trades_this_window = 0
        self.last_rejection_reason = ""
        self._history = {token_id: deque(maxlen=self.sma_window_ticks) for token_id in token_ids.values()}

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        if slug and slug != self._last_window_slug:
            self._last_window_slug = slug
            self._reset_window(data.token_ids)

        if data.elapsed_seconds is not None and data.elapsed_seconds >= self.time_window_seconds:
            self.last_rejection_reason = "entry_window_closed"
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is not None and cur is not None and abs(cur - ref) > self.max_btc_move:
            logger.debug("SMA skip: BTC move |%.0f - %.0f| > %.0f", cur, ref, self.max_btc_move)
            self.last_rejection_reason = "btc_guard_blocked"
            return None

        if self._trades_this_window >= self.max_trades_per_window:
            self.last_rejection_reason = "window_trade_cap_reached"
            return None

        # Update history from current market snapshot.
        for token_id in data.token_ids.values():
            book = data.books.get(token_id)
            ask = self._best_ask(book)
            obs = ask if ask is not None else self._book_last(book)
            if obs is None:
                continue
            if token_id not in self._history:
                self._history[token_id] = deque(maxlen=self.sma_window_ticks)
            self._history[token_id].append(float(obs))

        candidates: list[tuple[float, float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            book = data.books.get(token_id)
            ask = self._best_ask(book)
            if ask is None:
                continue
            hist = self._history.get(token_id)
            if not hist or len(hist) < self.sma_window_ticks:
                continue
            sma = sum(hist) / len(hist)
            # Entry only when ask is meaningfully below short MA and not too expensive.
            if ask <= sma - self.sma_discount and ask <= self.max_entry:
                candidates.append((ask, sma, outcome, token_id))

        candidates.sort(key=lambda x: x[0])
        if not candidates:
            self.last_rejection_reason = "no_sma_trigger"
        for ask, sma, outcome, token_id in candidates:
            max_price = min(ask + 0.04, 0.99)
            fill = executor.place_market_buy(
                token_id=token_id,
                amount_usd=self.buy_amount_usd,
                max_price=max_price,
                outcome=outcome,
                tick_size=data.tick_size,
                neg_risk=data.neg_risk,
                fill_at_price=ask,
            )
            if not fill:
                self.last_rejection_reason = "buy_not_filled"
                continue
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
            return {
                "action": "buy_then_sell_sma",
                "token_id": token_id,
                "outcome": outcome,
                "buy_price": fill.price,
                "sma": round(sma, 4),
                "sell_limit": self.sell_limit,
                "size": fill.size,
            }
        return None
