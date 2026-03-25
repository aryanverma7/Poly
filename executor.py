"""Execution abstraction: Executor interface, PaperExecutor (simulated), LiveExecutor (real CLOB)."""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class Fill:
    token_id: str
    side: str  # "buy" | "sell"
    price: float
    size: float
    amount_usd: float
    outcome: str = ""  # "Up" | "Down" (empty for legacy fills)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: Optional[str] = None


@dataclass
class Position:
    token_id: str
    outcome: str  # "Up" | "Down"
    size: float
    avg_price: float


class Executor(ABC):
    """Interface for order execution (paper or live)."""

    @abstractmethod
    def get_balance(self) -> float:
        """Current USDC balance (or simulated)."""
        pass

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Current positions (token_id, outcome, size, avg_price)."""
        pass

    def get_invested_amount(self) -> float:
        """Current amount tied up in open positions (cost basis)."""
        return sum(p.size * p.avg_price for p in self.get_positions())

    @abstractmethod
    def place_market_buy(
        self,
        token_id: str,
        amount_usd: float,
        max_price: float,
        outcome: str = "",
        tick_size: str = "0.01",
        neg_risk: bool = False,
        fill_at_price: Optional[float] = None,
    ) -> Optional[Fill]:
        """Place market buy; return Fill if executed, else None."""
        pass

    @abstractmethod
    def place_limit_sell(
        self,
        token_id: str,
        size: float,
        price: float,
        outcome: str = "",
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> Optional[Fill]:
        """Place limit sell; for paper we may simulate fill when price is hit or at resolution."""
        pass

    def get_fill_history(self) -> list[Fill]:
        """Return list of fills (for P&L and frontend). Override in subclasses."""
        return []

    def replace_pending_sell(self, token_id: str, new_price: float, outcome: str = "") -> int:
        """Optional: update queued pending sells for token (paper only). Returns updated row count."""
        return 0


@dataclass
class PendingSell:
    token_id: str
    size: float
    price: float
    outcome: str


@dataclass
class SharedPaperBank:
    """Shared paper cash pool across multiple strategy executors."""

    balance: float
    starting_balance: float


class PaperExecutor(Executor):
    """Simulated executor: tracks balance, positions, and fill history. Limit sells stay pending until market hits price."""

    def __init__(self, starting_balance: float, shared_bank: SharedPaperBank | None = None):
        bank = shared_bank or SharedPaperBank(
            balance=float(starting_balance),
            starting_balance=float(starting_balance),
        )
        self._bank = bank
        self._starting_balance = float(bank.starting_balance)
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._pending_sells: list[PendingSell] = []

    def get_balance(self) -> float:
        return float(self._bank.balance)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_fill_history(self) -> list[Fill]:
        return list(self._fills)

    def place_market_buy(
        self,
        token_id: str,
        amount_usd: float,
        max_price: float,
        outcome: str = "",
        tick_size: str = "0.01",
        neg_risk: bool = False,
        fill_at_price: Optional[float] = None,
    ) -> Optional[Fill]:
        if amount_usd <= 0 or max_price <= 0:
            return None
        if fill_at_price is not None and fill_at_price > 0:
            fill_price = float(fill_at_price)
            if fill_price > max_price + 1e-9:
                return None
            fill_price = min(fill_price, max_price, 0.99)
        else:
            fill_price = min(max_price, 0.99)
        size = amount_usd / fill_price
        cost = size * fill_price
        if cost > self._bank.balance:
            logger.warning("Paper: insufficient balance %.2f for cost %.2f", self._bank.balance, cost)
            return None
        self._bank.balance -= cost
        pos = self._positions.get(token_id)
        if pos:
            total_size = pos.size + size
            pos.avg_price = (pos.avg_price * pos.size + fill_price * size) / total_size
            pos.size = total_size
        else:
            self._positions[token_id] = Position(token_id=token_id, outcome=outcome, size=size, avg_price=fill_price)
        fill = Fill(
            token_id=token_id,
            side="buy",
            price=fill_price,
            size=size,
            amount_usd=cost,
            outcome=outcome or "",
            order_id="paper-buy",
        )
        self._fills.append(fill)
        logger.info("Paper BUY %s @ %.2f size %.2f cost %.2f", token_id[:16], fill_price, size, cost)
        return fill

    def place_limit_sell(
        self,
        token_id: str,
        size: float,
        price: float,
        outcome: str = "",
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> Optional[Fill]:
        pos = self._positions.get(token_id)
        if not pos or pos.size < size:
            logger.warning("Paper: no position or insufficient size for %s", token_id[:16])
            return None
        # Don't fill immediately; add as pending. Filled only when market hits price (try_fill_pending_sells).
        self._pending_sells.append(
            PendingSell(token_id=token_id, size=size, price=price, outcome=outcome)
        )
        logger.info(
            "Paper: limit sell queued %s @ %.2f size %.2f (fills when best bid >= ~%.2f)",
            token_id[:16],
            price,
            size,
            price,
        )
        return None

    def _best_bid(self, book) -> Optional[float]:
        if book is None:
            return None
        if hasattr(book, "best_bid") and book.best_bid is not None:
            return float(book.best_bid)
        if isinstance(book, dict) and book.get("bids"):
            b = book["bids"][0]
            return float(b.get("price", b) if isinstance(b, dict) else b)
        return None

    def _sell_can_fill(self, book, limit_price: float) -> bool:
        """True only when best bid is at/above limit. Do not use last_trade_price — it often stays
        ~50¢ from opening prints while the outcome you hold trades cheap; that caused false fills
        and profit before bid ever reached the sell limit."""
        bid = self._best_bid(book)
        if bid is None:
            return False
        # One tick slack for float / book ordering quirks only
        return bid + 1e-9 >= limit_price - 0.005

    def try_fill_pending_sells(self, books: dict) -> None:
        """Fill pending paper sells when best bid reaches the limit price."""
        for _ in range(32):
            filled = False
            next_pending: list[PendingSell] = []
            for p in self._pending_sells:
                book = books.get(p.token_id)
                pos = self._positions.get(p.token_id)
                if not self._sell_can_fill(book, p.price) or not pos or pos.size <= 1e-9:
                    next_pending.append(p)
                    continue
                # Fill min(pending, position) so float drift doesn't strand a lot
                sz = min(float(p.size), float(pos.size))
                if sz < 1e-9:
                    next_pending.append(p)
                    continue
                revenue = sz * p.price
                self._bank.balance += revenue
                pos.size = round(pos.size - sz, 6)
                if pos.size <= 1e-6:
                    del self._positions[p.token_id]
                fill = Fill(
                    token_id=p.token_id,
                    side="sell",
                    price=p.price,
                    size=sz,
                    amount_usd=revenue,
                    outcome=p.outcome or "",
                    order_id="paper-sell",
                )
                self._fills.append(fill)
                filled = True
                logger.info("Paper SELL filled %s @ %.2f size %.2f revenue %.2f", p.token_id[:16], p.price, sz, revenue)
                remainder = round(float(p.size) - sz, 6)
                if remainder > 0.02:
                    next_pending.append(
                        PendingSell(token_id=p.token_id, size=remainder, price=p.price, outcome=p.outcome)
                    )
                elif remainder > 1e-6:
                    pass  # dust remainder dropped (rounding)
            self._pending_sells = next_pending
            if not filled:
                break

    def replace_pending_sell(self, token_id: str, new_price: float, outcome: str = "") -> int:
        """Reprice queued pending sells for one token; useful for adaptive exits."""
        if new_price <= 0:
            return 0
        updated = 0
        next_pending: list[PendingSell] = []
        for p in self._pending_sells:
            if p.token_id == token_id:
                next_pending.append(
                    PendingSell(
                        token_id=p.token_id,
                        size=p.size,
                        price=float(new_price),
                        outcome=outcome or p.outcome,
                    )
                )
                updated += 1
            else:
                next_pending.append(p)
        self._pending_sells = next_pending
        return updated

    def settle_unfilled_at_window_end(
        self, token_ids: list, resolve_prices: dict | None = None
    ) -> None:
        """At end of 5m window: close any open position at the resolution price.

        resolve_prices maps token_id -> last_trade_price fetched from the book
        just before the window flipped.  A price >= 0.90 means that side won
        ($1 payout); a price <= 0.10 means it lost ($0 payout).  When the map
        is absent we fall back to 0 (conservative / old behaviour).
        """
        resolve_prices = resolve_prices or {}
        # Cancel all pending sells for these tokens up front — avoids orphaned
        # entries when a position was already closed mid-window (e.g. limit hit).
        self._pending_sells = [p for p in self._pending_sells if p.token_id not in token_ids]
        for token_id in token_ids:
            pos = self._positions.pop(token_id, None)
            if pos:
                cost = pos.size * pos.avg_price
                # Use provided resolution price; clamp to [0, 1].
                raw_price = resolve_prices.get(token_id)
                if raw_price is not None:
                    settle_price = max(0.0, min(1.0, float(raw_price)))
                else:
                    settle_price = 0.0
                revenue = pos.size * settle_price
                tag = "paper-settle-win" if settle_price >= 0.90 else "paper-settle-loss"
                self._fills.append(
                    Fill(
                        token_id=token_id,
                        side="sell",
                        price=settle_price,
                        size=pos.size,
                        amount_usd=revenue,
                        outcome=pos.outcome or "",
                        order_id=tag,
                    )
                )
                self._bank.balance += revenue
                pnl = revenue - cost
                logger.info(
                    "Paper settle: %s @ %.2f → revenue $%.2f, P&L $%.2f for %s",
                    tag, settle_price, revenue, pnl, token_id[:16],
                )

    def realize_pnl(self) -> float:
        """Total realized P&L from fills (simplified: sum(sell revenue) - sum(buy cost)."""
        total_buy = sum(f.amount_usd for f in self._fills if f.side == "buy")
        total_sell = sum(f.amount_usd for f in self._fills if f.side == "sell")
        return total_sell - total_buy

    def get_equity_curve(self) -> list[tuple[datetime, float]]:
        """Balance after each fill (for chart). Reconstruct from start balance + fills."""
        # Shared-wallet paper executor: don't reference legacy `_balance`.
        if hasattr(self, "_starting_balance"):
            start = float(self._starting_balance)
        else:
            # Best-effort fallback: current balance plus realized pnl back out.
            start = float(self.get_balance()) + float(self.realize_pnl())
        if not self._fills:
            return [(datetime.utcnow(), start)]
        curve: list[tuple[datetime, float]] = []
        running = start
        for f in self._fills:
            if f.side == "buy":
                running -= f.amount_usd
            else:
                running += f.amount_usd
            curve.append((f.ts, running))
        # Do NOT append a trailing (now, balance) point — it creates a
        # phantom dip whenever an open buy position has deducted cash
        # but no matching sell fill exists yet.
        return curve

    def set_starting_balance(self, value: float) -> None:
        self._starting_balance = float(value)
        self._bank.starting_balance = float(value)


class LiveExecutor(Executor):
    """Live CLOB orders via py-clob-client. Requires API credentials."""

    def __init__(self):
        self._client = None
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._init_client()

    def _init_client(self) -> None:
        try:
            import os
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            key = os.getenv("PRIVATE_KEY")
            if not key:
                logger.warning("LiveExecutor: PRIVATE_KEY not set; live orders disabled")
                return
            self._client = ClobClient(
                host=Config.from_env().clob_api_base,
                chain_id=137,
                key=key,
            )
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            self._BUY = BUY
            self._SELL = SELL
            self._OrderType = OrderType
            self._AssetType = AssetType
            self._BalanceAllowanceParams = BalanceAllowanceParams
        except Exception as e:
            logger.warning("LiveExecutor init failed: %s", e)
            self._client = None

    def get_balance(self) -> float:
        if not self._client:
            return 0.0
        try:
            result = self._client.get_balance_allowance(
                self._BalanceAllowanceParams(asset_type=self._AssetType.COLLATERAL)
            )
            bal = result.get("balance", 0) or 0
            return int(bal) / 1e6
        except Exception as e:
            logger.warning("get_balance failed: %s", e)
            return 0.0

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_fill_history(self) -> list[Fill]:
        return list(self._fills)

    def place_market_buy(
        self,
        token_id: str,
        amount_usd: float,
        max_price: float,
        outcome: str = "",
        tick_size: str = "0.01",
        neg_risk: bool = False,
        fill_at_price: Optional[float] = None,
    ) -> Optional[Fill]:
        if not self._client:
            return None
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            order = self._client.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=amount_usd,
                    side=self._BUY,
                )
            )
            resp = self._client.post_order(order, self._OrderType.FOK)
            if resp.get("status") in ("matched", "live"):
                making = float(resp.get("makingAmount", 0) or 0)
                size = making
                cost = amount_usd
                fill = Fill(
                    token_id=token_id,
                    side="buy",
                    price=max_price,
                    size=size,
                    amount_usd=cost,
                    outcome=outcome or "",
                    order_id=resp.get("orderID"),
                )
                self._fills.append(fill)
                self._positions[token_id] = Position(token_id=token_id, outcome=outcome, size=size, avg_price=max_price)
                return fill
        except Exception as e:
            logger.warning("Live market buy failed: %s", e)
        return None

    def place_limit_sell(
        self,
        token_id: str,
        size: float,
        price: float,
        outcome: str = "",
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> Optional[Fill]:
        if not self._client:
            return None
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order = self._client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=self._SELL,
                ),
                options={"tick_size": tick_size, "neg_risk": neg_risk},
            )
            resp = self._client.post_order(order, self._OrderType.GTC)
            if resp.get("status") in ("live", "matched"):
                fill = Fill(
                    token_id=token_id,
                    side="sell",
                    price=price,
                    size=size,
                    amount_usd=size * price,
                    outcome=outcome or "",
                    order_id=resp.get("orderID"),
                )
                self._fills.append(fill)
                if token_id in self._positions:
                    self._positions[token_id].size -= size
                    if self._positions[token_id].size <= 0:
                        del self._positions[token_id]
                return fill
        except Exception as e:
            logger.warning("Live limit sell failed: %s", e)
        return None


def create_executor(
    config: Optional[Config] = None,
    mode_override: Optional[str] = None,
    shared_paper_bank: Optional[SharedPaperBank] = None,
) -> Executor:
    """Factory: PaperExecutor if mode is paper, else LiveExecutor. mode_override: 'paper'|'live'."""
    cfg = config or Config.from_env()
    mode = (mode_override or cfg.trading_mode).lower()
    if mode == "paper":
        ex = PaperExecutor(cfg.paper_starting_balance, shared_bank=shared_paper_bank)
        ex.set_starting_balance(cfg.paper_starting_balance)
        return ex
    return LiveExecutor()
