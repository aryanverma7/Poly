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
        self._poll_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._price_ticks: deque[tuple[float, float]] = deque(maxlen=12000)
        self._last_ws_at: Optional[datetime] = None
        self._funding_rate: Optional[float] = None
        self._open_interest: Optional[float] = None
        self._oi_hist: deque[tuple[float, float]] = deque(maxlen=2000)
        self._depth_imbalance: Optional[float] = None
        self._last_local_btc: Optional[float] = None

    def start(self) -> None:
        if self.enable_ws:
            self._ws_thread = threading.Thread(target=self._run_ws_thread, daemon=True)
            self._ws_thread.start()
        if self.enable_funding or self.enable_open_interest or self.enable_depth:
            self._poll_thread = threading.Thread(target=self._run_poll_thread, daemon=True)
            self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws_thread:
            self._ws_thread.join(timeout=3)
        if self._poll_thread:
            self._poll_thread.join(timeout=3)

    def _run_ws_thread(self) -> None:
        asyncio.run(self._ws_loop())

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
            except Exception as e:
                logger.warning("ExternalData WS error: %s", e)
                await asyncio.sleep(2.0)

    def _run_poll_thread(self) -> None:
        next_funding = 0.0
        next_oi = 0.0
        next_depth = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
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
        if price is not None and local_btc is not None:
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
