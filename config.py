"""Load config from env and optional config file. Trading mode defaults to paper."""
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
# Trading mode: "paper" (default) or "live"
TRADING_MODE: str = os.getenv("TRADING_MODE", "paper").lower()
PAPER_STARTING_BALANCE: float = float(os.getenv("PAPER_STARTING_BALANCE", "20"))

# Strategy params (BTC 5m)
BUY_THRESHOLD_CENTS: int = int(os.getenv("BUY_THRESHOLD_CENTS", "25"))
SELL_LIMIT_CENTS: int = int(os.getenv("SELL_LIMIT_CENTS", "40"))
MAX_BTC_MOVE_USD: float = float(os.getenv("MAX_BTC_MOVE_USD", "100"))
TIME_WINDOW_SECONDS: int = 2 * 60  # first 2 minutes of 5-min window
BUY_AMOUNT_USD: float = float(os.getenv("BUY_AMOUNT_USD", "1"))
SMA_WINDOW_TICKS: int = int(os.getenv("SMA_WINDOW_TICKS", "10"))
SMA_DISCOUNT_CENTS: float = float(os.getenv("SMA_DISCOUNT_CENTS", "5.0"))
SMA_MAX_ENTRY_CENTS: int = int(os.getenv("SMA_MAX_ENTRY_CENTS", "35"))

# Advanced strategy knobs
END_WINDOW_MOVE_TRIGGER_USD: float = float(os.getenv("END_WINDOW_MOVE_TRIGGER_USD", "80"))
END_WINDOW_MAX_ENTRY_CENTS: int = int(os.getenv("END_WINDOW_MAX_ENTRY_CENTS", "55"))
ORACLE_LAG_MOVE_TRIGGER_USD: float = float(os.getenv("ORACLE_LAG_MOVE_TRIGGER_USD", "20"))
ORACLE_LAG_GAP_TRIGGER_USD: float = float(os.getenv("ORACLE_LAG_GAP_TRIGGER_USD", "3"))
ORACLE_LAG_STALE_MID_BAND: float = float(os.getenv("ORACLE_LAG_STALE_MID_BAND", "0.08"))
ORACLE_LAG_MAX_ENTRY_CENTS: int = int(os.getenv("ORACLE_LAG_MAX_ENTRY_CENTS", "70"))
ORACLE_LAG_BASE_STAKE_USD: float = float(os.getenv("ORACLE_LAG_BASE_STAKE_USD", "5"))
ORACLE_LAG_MIN_PROFIT_MARGIN: float = float(os.getenv("ORACLE_LAG_MIN_PROFIT_MARGIN", "0.30"))
END_WINDOW_SELL_LIMIT_CENTS: int = int(os.getenv("END_WINDOW_SELL_LIMIT_CENTS", "85"))
HYBRID_MOMENTUM_TRIGGER_USD: float = float(os.getenv("HYBRID_MOMENTUM_TRIGGER_USD", "15"))
HYBRID_ATR_MIN_USD: float = float(os.getenv("HYBRID_ATR_MIN_USD", "40"))
HYBRID_MAX_ENTRY_CENTS: int = int(os.getenv("HYBRID_MAX_ENTRY_CENTS", "25"))
MAX_CONSECUTIVE_LOSSES: int = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
COOLDOWN_WINDOWS_AFTER_LOSSES: int = int(os.getenv("COOLDOWN_WINDOWS_AFTER_LOSSES", "3"))
STAKE_MIN_MULT: float = float(os.getenv("STAKE_MIN_MULT", "0.25"))
STAKE_MAX_MULT: float = float(os.getenv("STAKE_MAX_MULT", "4"))

# Event slug pattern for discovery
EVENT_SLUG_CONTAINS: str = os.getenv("EVENT_SLUG_CONTAINS", "btc-updown-5m")

# API
GAMMA_API_BASE: str = os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com")
CLOB_API_BASE: str = os.getenv("CLOB_API_BASE", "https://clob.polymarket.com")

# External data feature flags (Binance)
ENABLE_EXTERNAL_DATA: bool = os.getenv("ENABLE_EXTERNAL_DATA", "false").lower() == "true"
ENABLE_BINANCE_WS: bool = os.getenv("ENABLE_BINANCE_WS", "true").lower() == "true"
ENABLE_BINANCE_FUNDING: bool = os.getenv("ENABLE_BINANCE_FUNDING", "true").lower() == "true"
ENABLE_BINANCE_OPEN_INTEREST: bool = os.getenv("ENABLE_BINANCE_OPEN_INTEREST", "true").lower() == "true"
ENABLE_BINANCE_DEPTH: bool = os.getenv("ENABLE_BINANCE_DEPTH", "true").lower() == "true"


@dataclass
class Config:
    trading_mode: str
    paper_starting_balance: float
    buy_threshold_cents: int
    sell_limit_cents: int
    max_btc_move_usd: float
    time_window_seconds: int
    buy_amount_usd: float
    sma_window_ticks: int
    sma_discount_cents: float
    sma_max_entry_cents: int
    end_window_move_trigger_usd: float
    end_window_max_entry_cents: int
    oracle_lag_move_trigger_usd: float
    oracle_lag_gap_trigger_usd: float
    oracle_lag_stale_mid_band: float
    oracle_lag_max_entry_cents: int
    oracle_lag_base_stake_usd: float
    oracle_lag_min_profit_margin: float
    end_window_sell_limit_cents: int
    hybrid_momentum_trigger_usd: float
    hybrid_atr_min_usd: float
    hybrid_max_entry_cents: int
    max_consecutive_losses: int
    cooldown_windows_after_losses: int
    stake_min_mult: float
    stake_max_mult: float
    event_slug_contains: str
    gamma_api_base: str
    clob_api_base: str
    enable_external_data: bool
    enable_binance_ws: bool
    enable_binance_funding: bool
    enable_binance_open_interest: bool
    enable_binance_depth: bool

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            trading_mode=TRADING_MODE,
            paper_starting_balance=PAPER_STARTING_BALANCE,
            buy_threshold_cents=BUY_THRESHOLD_CENTS,
            sell_limit_cents=SELL_LIMIT_CENTS,
            max_btc_move_usd=MAX_BTC_MOVE_USD,
            time_window_seconds=TIME_WINDOW_SECONDS,
            buy_amount_usd=BUY_AMOUNT_USD,
            sma_window_ticks=SMA_WINDOW_TICKS,
            sma_discount_cents=SMA_DISCOUNT_CENTS,
            sma_max_entry_cents=SMA_MAX_ENTRY_CENTS,
            end_window_move_trigger_usd=END_WINDOW_MOVE_TRIGGER_USD,
            end_window_max_entry_cents=END_WINDOW_MAX_ENTRY_CENTS,
            oracle_lag_move_trigger_usd=ORACLE_LAG_MOVE_TRIGGER_USD,
            oracle_lag_gap_trigger_usd=ORACLE_LAG_GAP_TRIGGER_USD,
            oracle_lag_stale_mid_band=ORACLE_LAG_STALE_MID_BAND,
            oracle_lag_max_entry_cents=ORACLE_LAG_MAX_ENTRY_CENTS,
            oracle_lag_base_stake_usd=ORACLE_LAG_BASE_STAKE_USD,
            oracle_lag_min_profit_margin=ORACLE_LAG_MIN_PROFIT_MARGIN,
            end_window_sell_limit_cents=END_WINDOW_SELL_LIMIT_CENTS,
            hybrid_momentum_trigger_usd=HYBRID_MOMENTUM_TRIGGER_USD,
            hybrid_atr_min_usd=HYBRID_ATR_MIN_USD,
            hybrid_max_entry_cents=HYBRID_MAX_ENTRY_CENTS,
            max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
            cooldown_windows_after_losses=COOLDOWN_WINDOWS_AFTER_LOSSES,
            stake_min_mult=STAKE_MIN_MULT,
            stake_max_mult=STAKE_MAX_MULT,
            event_slug_contains=EVENT_SLUG_CONTAINS,
            gamma_api_base=GAMMA_API_BASE,
            clob_api_base=CLOB_API_BASE,
            enable_external_data=ENABLE_EXTERNAL_DATA,
            enable_binance_ws=ENABLE_BINANCE_WS,
            enable_binance_funding=ENABLE_BINANCE_FUNDING,
            enable_binance_open_interest=ENABLE_BINANCE_OPEN_INTEREST,
            enable_binance_depth=ENABLE_BINANCE_DEPTH,
        )
