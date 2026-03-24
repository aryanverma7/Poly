"""
Bitcoin Up or Down 5m strategy: in first 2 min, buy whichever outcome hits 25¢ first (once per window),
then place limit sell at 40¢. Do not buy if BTC has moved > $100 from reference.
"""
import logging
from typing import Optional

from executor import Executor
from strategies.base import MarketData, Strategy

logger = logging.getLogger(__name__)


class Btc5mStrategy(Strategy):
    def __init__(
        self,
        buy_threshold_cents: int = 25,
        sell_limit_cents: int = 40,
        max_btc_move_usd: float = 100,
        time_window_seconds: int = 120,
        buy_amount_usd: float = 10.0,
    ):
        self.buy_threshold = buy_threshold_cents / 100.0
        self._buy_trigger = self.buy_threshold + 0.012
        self.sell_limit = sell_limit_cents / 100.0
        self.max_btc_move = max_btc_move_usd
        self.time_window_seconds = time_window_seconds
        self.buy_amount_usd = buy_amount_usd
        self._last_window_slug: Optional[str] = None
        self._traded_this_window: bool = False
        self.last_rejection_reason: str = ""

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

    @property
    def name(self) -> str:
        return "Btc5mStrategy"

    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        slug = getattr(data, "event_slug", "") or ""
        if slug and slug != self._last_window_slug:
            self._last_window_slug = slug
            self._traded_this_window = False
            self.last_rejection_reason = ""

        if data.elapsed_seconds is not None and data.elapsed_seconds >= self.time_window_seconds:
            self.last_rejection_reason = "entry_window_closed"
            return None

        ref = data.reference_btc_price
        cur = data.current_btc_price
        if ref is not None and cur is not None:
            if abs(cur - ref) > self.max_btc_move:
                logger.debug("Skip: BTC move |%.0f - %.0f| > %.0f", cur, ref, self.max_btc_move)
                self.last_rejection_reason = "btc_guard_blocked"
                return None

        if self._traded_this_window:
            self.last_rejection_reason = "already_traded_window"
            return None

        def _asks_empty(book) -> bool:
            if book is None:
                return True
            asks = getattr(book, "asks", None)
            if asks is None and isinstance(book, dict):
                asks = book.get("asks")
            return not asks

        candidates: list[tuple[float, str, str]] = []
        for outcome, token_id in data.token_ids.items():
            book = data.books.get(token_id)
            if not book:
                continue
            ask = self._best_ask(book)
            last = self._book_last(book)
            entry: Optional[float] = None
            if ask is not None and float(ask) <= self._buy_trigger:
                entry = float(ask)
            elif _asks_empty(book) and last is not None and last <= self._buy_trigger:
                entry = float(last)
            if entry is not None:
                candidates.append((entry, outcome, token_id))

        move30 = getattr(data, 'binance_move_30s', None)
        if move30 is None:
            move30 = getattr(data, 'btc_move_30s', None)
        if move30 is not None and abs(move30) > 20:
            direction = "Up" if move30 > 0 else "Down"
            candidates = [(e, o, t) for e, o, t in candidates if o == direction]
        candidates.sort(key=lambda x: x[0])
        if not candidates:
            self.last_rejection_reason = "no_price_trigger"
        for entry, outcome, token_id in candidates:
            max_price = min(entry + 0.04, 0.99)
            fill = executor.place_market_buy(
                token_id=token_id,
                amount_usd=self.buy_amount_usd,
                max_price=max_price,
                outcome=outcome,
                tick_size=data.tick_size,
                neg_risk=data.neg_risk,
                fill_at_price=entry,
            )
            if fill:
                self._traded_this_window = True
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
                    "action": "buy_then_sell",
                    "token_id": token_id,
                    "outcome": outcome,
                    "buy_price": fill.price,
                    "sell_limit": self.sell_limit,
                    "size": fill.size,
                }
            self.last_rejection_reason = "buy_not_filled"
        return None
