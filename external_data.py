"""External market data service (Binance WS + REST) for strategy signals."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
import websockets
from orderbook import get_btc_price_usd

logger = logging.getLogger(__name__)


@dataclass
class ExternalSnapshot:
    binance_price: Optional[float] = None
    binance_move_30s: Optional[float] = None
    oracle_gap_usd: Optional[float] = None
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    open_interest_change_5m: Optional[float] = None
    binance_depth_imbalance: Optional[float] = None
    last_ws_at: Optional[str] = None


class ExternalDataService:
    """Maintains external signals in background with safe fallbacks."""

    def __init__(
        self,
        enable_ws: bool = True,
        enable_funding: bool = True,
        enable_open_interest: bool = True,
        enable_depth: bool = True,
    ):
        self.enable_ws = enable_ws
        self.enable_funding = enable_funding
        self.enable_open_interest = enable_open_interest
        self.enable_depth = enable_depth
        self._stop = threading.Event()
        self._ws_thread: Optional[threading.Thread] = None
        self._clob_ws_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._urgent_signal = threading.Event()
        self._price_ticks: deque[tuple[float, float]] = deque(maxlen=12000)
        self._last_ws_at: Optional[datetime] = None
        self._funding_rate: Optional[float] = None
        self._open_interest: Optional[float] = None
        self._oi_hist: deque[tuple[float, float]] = deque(maxlen=2000)
        self._depth_imbalance: Optional[float] = None
        self._last_local_btc: Optional[float] = None
        self._chainlink_price: Optional[float] = None
        self._chainlink_last_fetch: float = 0.0
        self._chainlink_ttl: float = 10.0
        self._clob_tokens: list[str] = []
        self._clob_books: dict[str, dict] = {}
        self._clob_sub_version: int = 0
        self._urgent_wake_times: deque[float] = deque(maxlen=2000)
        self._last_urgent_mark: float = 0.0
        self._clob_ws_connected: bool = False
        self._clob_last_update_at: Optional[float] = None
        self._clob_last_error: Optional[str] = None

    def start(self) -> None:
        if self.enable_ws:
            self._ws_thread = threading.Thread(target=self._run_ws_thread, daemon=True)
            self._ws_thread.start()
            self._clob_ws_thread = threading.Thread(target=self._run_clob_ws_thread, daemon=True)
            self._clob_ws_thread.start()
        self._poll_thread = threading.Thread(target=self._run_poll_thread, daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws_thread:
            self._ws_thread.join(timeout=3)
        if self._clob_ws_thread:
            self._clob_ws_thread.join(timeout=3)
        if self._poll_thread:
            self._poll_thread.join(timeout=3)

    def _run_ws_thread(self) -> None:
        asyncio.run(self._ws_loop())

    def _run_clob_ws_thread(self) -> None:
        asyncio.run(self._clob_ws_loop())

    async def _ws_loop(self) -> None:
        url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("ExternalData: connected Binance trade WS")
                    while not self._stop.is_set():
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        p = data.get("p")
                        if p is None:
                            continue
                        try:
                            px = float(p)
                        except (TypeError, ValueError):
                            continue
                        ts = time.time()
                        with self._lock:
                            self._price_ticks.append((ts, px))
                            self._last_ws_at = datetime.now(timezone.utc)
                            cutoff = ts - 15 * 60
                            while self._price_ticks and self._price_ticks[0][0] < cutoff:
                                self._price_ticks.popleft()
                            base30 = None
                            cutoff30 = ts - 30.0
                            for pts, ppx in reversed(self._price_ticks):
                                if pts <= cutoff30:
                                    base30 = ppx
                                    break
                            if base30 is not None and abs(px - base30) >= 40.0:
                                now_ts = time.time()
                                # De-dupe urgent marks so the UI counter doesn't explode
                                if now_ts - self._last_urgent_mark > 0.5:
                                    self._urgent_wake_times.append(now_ts)
                                    self._last_urgent_mark = now_ts
                                self._urgent_signal.set()
            except Exception as e:
                logger.warning("ExternalData WS error: %s", e)
                await asyncio.sleep(2.0)

    async def _clob_ws_loop(self) -> None:
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        while not self._stop.is_set():
            try:
                with self._lock:
                    tokens = list(self._clob_tokens)
                    version = self._clob_sub_version
                if not tokens:
                    await asyncio.sleep(0.5)
                    continue
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    with self._lock:
                        self._clob_ws_connected = True
                        self._clob_last_error = None
                    logger.info("ExternalData: connected Polymarket CLOB WS (%d tokens)", len(tokens))
                    # Polymarket CLOB "market" channel subscription.
                    # Docs: send a single message with `type`, `assets_ids`, and `custom_feature_enabled`.
                    await ws.send(
                        json.dumps(
                            {
                                "type": "market",
                                "assets_ids": tokens,
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    while not self._stop.is_set():
                        with self._lock:
                            if version != self._clob_sub_version:
                                break
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        events = data if isinstance(data, list) else [data]
                        updated_any = False
                        with self._lock:
                            for event in events:
                                event_type = event.get("event_type")

                                # best_bid_ask event (requires custom_feature_enabled=true)
                                if event_type == "best_bid_ask":
                                    asset_id = event.get("asset_id")
                                    if not asset_id or asset_id not in self._clob_books:
                                        continue
                                    try:
                                        if event.get("best_bid") is not None:
                                            self._clob_books[asset_id]["best_bid"] = float(event.get("best_bid"))
                                            updated_any = True
                                        if event.get("best_ask") is not None:
                                            self._clob_books[asset_id]["best_ask"] = float(event.get("best_ask"))
                                            updated_any = True
                                    except (TypeError, ValueError):
                                        pass
                                    continue

                                # book snapshot event
                                if event_type == "book":
                                    asset_id = event.get("asset_id")
                                    if not asset_id or asset_id not in self._clob_books:
                                        continue
                                    bids = event.get("bids") or []
                                    asks = event.get("asks") or []
                                    try:
                                        if bids:
                                            self._clob_books[asset_id]["best_bid"] = float(bids[0].get("price"))
                                            self._clob_books[asset_id]["bids"] = bids
                                            updated_any = True
                                        if asks:
                                            self._clob_books[asset_id]["best_ask"] = float(asks[0].get("price"))
                                            self._clob_books[asset_id]["asks"] = asks
                                            updated_any = True
                                    except (TypeError, ValueError, AttributeError):
                                        pass
                                    continue

                                # last_trade_price event
                                if event_type == "last_trade_price":
                                    asset_id = event.get("asset_id")
                                    if not asset_id or asset_id not in self._clob_books:
                                        continue
                                    try:
                                        price = event.get("price")
                                        if price is not None:
                                            self._clob_books[asset_id]["last_trade"] = float(price)
                                            updated_any = True
                                    except (TypeError, ValueError):
                                        pass
                                    continue

                                # price_change event contains per-asset updates in `price_changes`
                                if event_type == "price_change":
                                    for pc in event.get("price_changes") or []:
                                        asset_id = pc.get("asset_id")
                                        if not asset_id or asset_id not in self._clob_books:
                                            continue
                                        try:
                                            if pc.get("best_bid") is not None:
                                                new_bid = float(pc.get("best_bid"))
                                                self._clob_books[asset_id]["best_bid"] = new_bid
                                                # Keep the depth array in sync so _book_from_cached reads the fresh price
                                                existing_bids = self._clob_books[asset_id].get("bids") or []
                                                if existing_bids:
                                                    existing_bids[0] = {**existing_bids[0], "price": str(new_bid)}
                                                else:
                                                    self._clob_books[asset_id]["bids"] = [{"price": str(new_bid), "size": "0"}]
                                                updated_any = True
                                            if pc.get("best_ask") is not None:
                                                new_ask = float(pc.get("best_ask"))
                                                self._clob_books[asset_id]["best_ask"] = new_ask
                                                # Keep the depth array in sync so _book_from_cached reads the fresh price
                                                existing_asks = self._clob_books[asset_id].get("asks") or []
                                                if existing_asks:
                                                    existing_asks[0] = {**existing_asks[0], "price": str(new_ask)}
                                                else:
                                                    self._clob_books[asset_id]["asks"] = [{"price": str(new_ask), "size": "0"}]
                                                updated_any = True
                                            if pc.get("price") is not None and pc.get("side") is not None:
                                                # price_change includes price, but treat it as "last_trade" only loosely.
                                                self._clob_books[asset_id]["last_trade"] = float(pc.get("price"))
                                        except (TypeError, ValueError):
                                            pass
                                    continue
                            if updated_any:
                                self._clob_last_update_at = time.time()
            except Exception as e:
                logger.warning("ExternalData CLOB WS error: %s", e)
                with self._lock:
                    self._clob_ws_connected = False
                    self._clob_last_error = str(e)
                await asyncio.sleep(1.0)

    def set_clob_tokens(self, token_ids: list[str]) -> None:
        cleaned = sorted({str(t) for t in token_ids if t})
        with self._lock:
            if cleaned == self._clob_tokens:
                return
            self._clob_tokens = cleaned
            self._clob_books = {
                t: {"best_ask": None, "best_bid": None, "last_trade": None, "bids": [], "asks": []}
                for t in cleaned
            }
            self._clob_sub_version += 1

    def get_clob_book(self, token_id: str) -> Optional[dict]:
        with self._lock:
            b = self._clob_books.get(token_id)
            return dict(b) if b else None

    def get_local_btc_price(self) -> Optional[float]:
        with self._lock:
            return self._last_local_btc

    def clob_ws_is_connected(self) -> bool:
        with self._lock:
            return bool(self._clob_ws_connected)

    def clob_last_update_age_sec(self) -> Optional[float]:
        with self._lock:
            last = self._clob_last_update_at
        if last is None:
            return None
        return float(time.time() - last)

    def clob_last_error_msg(self) -> Optional[str]:
        with self._lock:
            return self._clob_last_error

    def urgent_wake_count_last_60s(self) -> int:
        cutoff = time.time() - 60.0
        with self._lock:
            # deque is ordered; pop left if too old
            while self._urgent_wake_times and self._urgent_wake_times[0] < cutoff:
                self._urgent_wake_times.popleft()
            return len(self._urgent_wake_times)

    def wait_for_urgent_signal(self, timeout_sec: float) -> None:
        self._urgent_signal.wait(timeout=timeout_sec)
        self._urgent_signal.clear()

    def _run_poll_thread(self) -> None:
        next_funding = 0.0
        next_oi = 0.0
        next_depth = 0.0
        next_btc = 0.0
        next_chainlink = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now >= next_btc:
                    # Reuse cached Chainlink-derived price; avoid hitting Polygon RPC twice.
                    with self._lock:
                        px = self._chainlink_price
                    if px is None or px < 100:
                        px = get_btc_price_usd()
                    if px is not None:
                        with self._lock:
                            self._last_local_btc = float(px)
                    next_btc = now + 1.0
                if now >= next_chainlink:
                    self._refresh_chainlink_price(now)
                    next_chainlink = now + self._chainlink_ttl
                if self.enable_funding and now >= next_funding:
                    self._poll_funding()
                    next_funding = now + 20.0
                if self.enable_open_interest and now >= next_oi:
                    self._poll_open_interest()
                    next_oi = now + 15.0
                if self.enable_depth and now >= next_depth:
                    self._poll_depth()
                    next_depth = now + 5.0
            except Exception as e:
                logger.warning("ExternalData poll error: %s", e)
            time.sleep(0.5)

    def _poll_funding(self) -> None:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        r = requests.get(url, params={"symbol": "BTCUSDT"}, timeout=8)
        r.raise_for_status()
        d = r.json()
        v = d.get("lastFundingRate")
        rate = float(v) if v is not None else None
        with self._lock:
            self._funding_rate = rate

    def _poll_open_interest(self) -> None:
        url = "https://fapi.binance.com/fapi/v1/openInterest"
        r = requests.get(url, params={"symbol": "BTCUSDT"}, timeout=8)
        r.raise_for_status()
        d = r.json()
        oi = float(d.get("openInterest"))
        now = time.time()
        with self._lock:
            self._open_interest = oi
            self._oi_hist.append((now, oi))
            cutoff = now - 10 * 60
            while self._oi_hist and self._oi_hist[0][0] < cutoff:
                self._oi_hist.popleft()

    def _poll_depth(self) -> None:
        url = "https://api.binance.com/api/v3/depth"
        r = requests.get(url, params={"symbol": "BTCUSDT", "limit": 100}, timeout=8)
        r.raise_for_status()
        d = r.json()
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        if not bids or not asks:
            return
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_bid <= 0 or best_ask <= 0:
            return
        width = best_bid * 0.002  # ~20 bps
        bid_depth = 0.0
        for p, s in bids:
            px = float(p)
            sz = float(s)
            if px >= best_bid - width:
                bid_depth += px * sz
        ask_depth = 0.0
        for p, s in asks:
            px = float(p)
            sz = float(s)
            if px <= best_ask + width:
                ask_depth += px * sz
        imb = (bid_depth + 1e-9) / (ask_depth + 1e-9)
        with self._lock:
            self._depth_imbalance = float(imb)

    def _refresh_chainlink_price(self, now_ts: Optional[float] = None) -> None:
        """Refresh Chainlink BTC/USD cache (Polymarket resolution source)."""
        now_ts = now_ts if now_ts is not None else time.time()
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [
                    {
                        "to": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
                        "data": "0x50d25bcd",
                    },
                    "latest",
                ],
                "id": 1,
            }
            r = requests.post("https://polygon-rpc.com", json=payload, timeout=3)
            result = r.json().get("result")
            if result:
                val = int(result, 16) / 1e8
                if val > 100:
                    with self._lock:
                        self._chainlink_price = float(val)
                        self._chainlink_last_fetch = now_ts
                    return
        except Exception:
            pass

    def snapshot(self, local_btc_price: Optional[float] = None) -> ExternalSnapshot:
        now = time.time()
        with self._lock:
            ticks = list(self._price_ticks)
            funding = self._funding_rate
            oi = self._open_interest
            oi_hist = list(self._oi_hist)
            imb = self._depth_imbalance
            ws_at = self._last_ws_at
            if local_btc_price is not None:
                self._last_local_btc = float(local_btc_price)
            local_btc = self._last_local_btc
            chainlink = self._chainlink_price
        price = ticks[-1][1] if ticks else None
        base30 = None
        cutoff30 = now - 30.0
        # Search backwards so a slightly out-of-order tick list can't break lookbacks.
        for ts, px in reversed(ticks):
            if ts <= cutoff30:
                base30 = px
                break
        move30 = (price - base30) if (price is not None and base30 is not None) else None
        oi5 = None
        cutoff_5m = now - 5 * 60
        first_oi = None
        for ts, v in reversed(oi_hist):
            if ts <= cutoff_5m:
                first_oi = v
                break
        if oi is not None and first_oi is not None and first_oi > 0:
            oi5 = (oi - first_oi) / first_oi
        gap = None
        if price is not None and chainlink is not None:
            gap = price - chainlink
        elif price is not None and local_btc is not None:
            gap = price - local_btc
        return ExternalSnapshot(
            binance_price=price,
            binance_move_30s=move30,
            oracle_gap_usd=gap,
            funding_rate=funding,
            open_interest=oi,
            open_interest_change_5m=oi5,
            binance_depth_imbalance=imb,
            last_ws_at=ws_at.isoformat() if ws_at else None,
        )
