"""Strategy interface: pluggable for different strategies."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from executor import Executor


@dataclass
class MarketData:
    """Data passed to strategy each tick."""
    event_id: str
    token_ids: dict[str, str]  # outcome -> token_id
    books: dict[str, Any]  # token_id -> OrderBook-like (best_ask, best_bid)
    reference_btc_price: Optional[float] = None
    current_btc_price: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    seconds_to_window_end: Optional[float] = None
    tick_size: str = "0.01"
    neg_risk: bool = False
    event_slug: str = ""  # e.g. btc-updown-5m-1773788100; strategy uses this to detect new window
    btc_atr_1m_10m: Optional[float] = None
    btc_move_30s: Optional[float] = None
    binance_price: Optional[float] = None
    binance_move_30s: Optional[float] = None
    oracle_gap_usd: Optional[float] = None
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    open_interest_change_5m: Optional[float] = None
    binance_depth_imbalance: Optional[float] = None
    external_last_ws_at: Optional[str] = None


class Strategy(ABC):
    """Interface for strategies. Run by trader loop with market data and executor."""

    @abstractmethod
    def run_tick(self, data: MarketData, executor: Executor) -> Optional[dict]:
        """
        Process one tick. Return None or a dict describing action taken (for logging).
        Strategy may call executor.place_market_buy / place_limit_sell.
        """
        pass

    @property
    def name(self) -> str:
        return self.__class__.__name__
