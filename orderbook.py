"""CLOB client: fetch order book, parse best ask/bid."""
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    last_trade_price: Optional[float] = None
    tick_size: str = "0.01"
    neg_risk: bool = False
    min_order_size: str = "1"

    @property
    def best_bid(self) -> Optional[float]:
        if not self.bids:
            return None
        return self.bids[0].price

    @property
    def best_ask(self) -> Optional[float]:
        if not self.asks:
            return None
        return self.asks[0].price


def fetch_book(token_id: str, base_url: Optional[str] = None) -> Optional[OrderBook]:
    """GET /book for one token_id. No auth required."""
    base_url = (base_url or Config.from_env().clob_api_base).rstrip("/")
    url = f"{base_url}/book"
    try:
        r = requests.get(url, params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("CLOB get book failed for %s: %s", token_id[:16], e)
        return None

    bids = []
    for b in data.get("bids") or []:
        try:
            bids.append(BookLevel(price=float(b["price"]), size=float(b.get("size", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    bids.sort(key=lambda x: -x.price)

    asks = []
    for a in data.get("asks") or []:
        try:
            asks.append(BookLevel(price=float(a["price"]), size=float(a.get("size", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    asks.sort(key=lambda x: x.price)

    last = data.get("last_trade_price")
    try:
        last_trade_price = float(last) if last is not None else None
    except (TypeError, ValueError):
        last_trade_price = None

    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        last_trade_price=last_trade_price,
        tick_size=str(data.get("tick_size", "0.01")),
        neg_risk=bool(data.get("neg_risk", False)),
        min_order_size=str(data.get("min_order_size", "1")),
    )


def get_btc_price_usd() -> Optional[float]:
    """
    Fetch current BTC/USD price for the guard. Uses a free API; for production
    use Chainlink (Polymarket resolution source) if available.
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        return float(data.get("bitcoin", {}).get("usd"))
    except Exception as e:
        logger.warning("BTC price fetch failed: %s", e)
        return None
