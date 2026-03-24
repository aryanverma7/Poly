"""
Smart-money / leaderboard follower service for btc-updown-5m.

Polymarket doesn't expose a public leaderboard REST endpoint, so we approximate
the "top 3 traders" signal by monitoring large-bet activity on the current window:
  - Large trade ($15+ USD in one shot on a 5-min binary) = high-conviction trader
  - We track the directional consensus of these traders per window
  - Strategy fires when 2+ smart-money traders agree, or 1 whale ($50+) acts

Data source: https://data-api.polymarket.com/trades?market=btc-updown-5m
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from copy import copy
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"


@dataclass
class SmartMoneySignal:
    direction: Optional[str] = None   # "Up" or "Down" or None (no consensus)
    confidence: float = 0.0           # vol_winning / vol_total (0-1)
    buy_vol_up: float = 0.0           # USD bought on Up this window
    buy_vol_down: float = 0.0         # USD bought on Down this window
    n_up_traders: int = 0             # distinct traders who bet Up
    n_down_traders: int = 0           # distinct traders who bet Down
    largest_single_bet: float = 0.0   # biggest single trade this window
    window_slug: str = ""
    last_updated: float = 0.0         # time.time() when last refreshed


class LeaderboardService:
    """
    Polls btc-updown-5m trades every N seconds and maintains a SmartMoneySignal
    for the current 5-minute window. Thread-safe.

    Tracks traders placing >= min_usd_per_trade on a single bet as "smart money".
    A whale is anyone betting >= large_usd_threshold in a single transaction.
    """

    def __init__(
        self,
        poll_interval_sec: float = 5.0,
        min_usd_per_trade: float = 15.0,
        large_usd_threshold: float = 50.0,
    ):
        self.poll_interval = max(5.0, float(poll_interval_sec))
        self.min_usd = max(1.0, float(min_usd_per_trade))
        self.large_usd = max(self.min_usd, float(large_usd_threshold))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._signal: SmartMoneySignal = SmartMoneySignal()
        # Seen tx hashes so we don't double-count across polls
        self._seen_tx: deque[str] = deque(maxlen=5000)
        # window_slug -> {wallet_addr -> {"Up": float, "Down": float, "max_single": float}}
        self._window_data: dict[str, dict[str, dict]] = {}
        self._current_window: str = ""

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="leaderboard_svc")
        self._thread.start()
        logger.info("LeaderboardService started (poll=%.0fs min_usd=%.0f large_usd=%.0f)",
                    self.poll_interval, self.min_usd, self.large_usd)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    # ------------------------------------------------------------------ public

    def get_signal(self) -> SmartMoneySignal:
        with self._lock:
            return copy(self._signal)

    def set_current_window(self, slug: str) -> None:
        """Notify service of the active window slug so it can scope analysis."""
        if not slug:
            return
        with self._lock:
            if slug == self._current_window:
                return
            self._current_window = slug
            # Prune stale windows (keep last 3)
            keys = [k for k in self._window_data if k != slug]
            for k in keys[3:]:
                self._window_data.pop(k, None)
            # Reset signal for fresh window
            self._signal = SmartMoneySignal(window_slug=slug, last_updated=time.time())

    # ------------------------------------------------------------------ internals

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now >= next_poll:
                try:
                    self._poll()
                except Exception as exc:
                    logger.debug("LeaderboardService poll error: %s", exc)
                next_poll = time.time() + self.poll_interval
            time.sleep(0.5)

    def _poll(self) -> None:
        resp = requests.get(
            f"{DATA_API_BASE}/trades",
            params={"market": "btc-updown-5m", "limit": 50},
            timeout=10,
        )
        resp.raise_for_status()
        trades = resp.json()
        if not isinstance(trades, list):
            return

        with self._lock:
            window = self._current_window
            if not window:
                return

            # Log first response so we can see actual slug field names
            if trades and logger.isEnabledFor(logging.DEBUG):
                sample = trades[0]
                logger.debug("LeaderboardService sample trade keys: %s slug=%r eventSlug=%r conditionId=%r",
                             list(sample.keys()),
                             sample.get("slug"), sample.get("eventSlug"),
                             sample.get("conditionId"))

            new_trades = 0
            for trade in trades:
                # Match current window — try multiple slug field names
                slug = (trade.get("slug") or trade.get("eventSlug")
                        or trade.get("market") or trade.get("marketSlug") or "")
                # Accept if slug matches exactly OR if our window slug contains the trade slug
                if slug and slug != window and window not in slug and slug not in window:
                    continue
                # Only buys (entering a position = conviction)
                if str(trade.get("side") or "").upper() != "BUY":
                    continue
                # Dedup
                tx = str(trade.get("transactionHash") or "")
                if tx and tx in self._seen_tx:
                    continue
                if tx:
                    self._seen_tx.append(tx)
                # USD size filter
                usd = float(trade.get("usdcSize") or 0.0)
                if usd < self.min_usd:
                    continue
                # Direction
                outcome = str(trade.get("outcome") or "").strip()
                if outcome not in ("Up", "Down"):
                    continue
                addr = str(trade.get("proxyWallet") or trade.get("maker") or "").lower()
                if not addr:
                    continue
                # Accumulate
                wd = self._window_data.setdefault(window, {})
                td = wd.setdefault(addr, {"Up": 0.0, "Down": 0.0, "max_single": 0.0})
                td[outcome] += usd
                if usd > td["max_single"]:
                    td["max_single"] = usd
                new_trades += 1

            if new_trades > 0 or self._signal.window_slug != window:
                self._signal = self._compute_signal(window)
                logger.debug("LeaderboardService: window=%s up=%.1f(n=%d) down=%.1f(n=%d) dir=%s conf=%.2f",
                             window[-10:],
                             self._signal.buy_vol_up, self._signal.n_up_traders,
                             self._signal.buy_vol_down, self._signal.n_down_traders,
                             self._signal.direction, self._signal.confidence)
            else:
                # Heartbeat: always refresh last_updated so strategy knows we're alive
                self._signal.last_updated = time.time()

    def _compute_signal(self, window: str) -> SmartMoneySignal:
        data = self._window_data.get(window, {})
        vol_up = vol_down = 0.0
        traders_up: set = set()
        traders_down: set = set()
        largest = 0.0
        for addr, dirs in data.items():
            up = dirs.get("Up", 0.0)
            dn = dirs.get("Down", 0.0)
            ms = dirs.get("max_single", 0.0)
            if ms > largest:
                largest = ms
            # Assign trader to their dominant direction
            if up > dn:
                vol_up += up
                traders_up.add(addr)
            elif dn > up:
                vol_down += dn
                traders_down.add(addr)
        total = vol_up + vol_down
        direction: Optional[str] = None
        confidence = 0.0
        if total > 1e-9:
            if vol_up > vol_down:
                direction = "Up"
                confidence = vol_up / total
            elif vol_down > vol_up:
                direction = "Down"
                confidence = vol_down / total
        return SmartMoneySignal(
            direction=direction,
            confidence=float(confidence),
            buy_vol_up=float(vol_up),
            buy_vol_down=float(vol_down),
            n_up_traders=len(traders_up),
            n_down_traders=len(traders_down),
            largest_single_bet=float(largest),
            window_slug=window,
            last_updated=time.time(),
        )
