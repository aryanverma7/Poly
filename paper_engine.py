"""
Reusable strategy tester: run any strategy with PaperExecutor, record trades and equity.
Used for testing without real money; data consumed by frontend when in testing mode.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from config import Config
from executor import Executor, PaperExecutor
from strategies.base import MarketData, Strategy

logger = logging.getLogger(__name__)


@dataclass
class StrategyRunState:
    """State of a strategy run for frontend / API."""
    running: bool = False
    mode: str = "paper"  # "paper" | "live"
    session_start: Optional[datetime] = None
    total_profit: float = 0.0
    session_profit: float = 0.0
    trade_count: int = 0
    session_trade_count: int = 0
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    last_error: Optional[str] = None


def run_strategy_tick(
    strategy: Strategy,
    data: MarketData,
    executor: Executor,
    state: StrategyRunState,
) -> None:
    """
    Run one tick of the strategy; update state (trades, equity, P&L).
    Reusable for any strategy.
    """
    try:
        result = strategy.run_tick(data, executor)
        if result:
            state.session_trade_count += 1
            state.trade_count += 1
            state.trades.append(
                {
                    "ts": datetime.utcnow().isoformat(),
                    "result": result,
                }
            )
        if isinstance(executor, PaperExecutor):
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
    except Exception as e:
        logger.exception("Strategy tick failed: %s", e)
        state.last_error = str(e)


def create_paper_engine(
    strategy_factory: Callable[[], Strategy],
    config: Optional[Config] = None,
) -> tuple[PaperExecutor, StrategyRunState]:
    """
    Create a PaperExecutor and StrategyRunState for the given strategy.
    strategy_factory: callable that returns a Strategy instance (e.g. lambda: Btc5mStrategy(...)).
    """
    cfg = config or Config.from_env()
    balance = cfg.paper_starting_balance
    executor = PaperExecutor(balance)
    executor.set_starting_balance(balance)
    state = StrategyRunState(mode="paper", session_start=datetime.utcnow())
    return executor, state
