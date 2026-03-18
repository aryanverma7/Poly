"""Gamma API: find active Bitcoin Up or Down 5m event and token IDs."""
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    outcome: str
    token_id: str
    end_date_iso: Optional[str] = None


@dataclass
class EventInfo:
    event_id: str
    slug: str
    title: str
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    markets: list[MarketInfo]

    def get_token_id(self, outcome: str) -> Optional[str]:
        for m in self.markets:
            if m.outcome == outcome:
                return m.token_id
        return None


def _parse_clob_token_ids(market: dict) -> list[str]:
    raw = market.get("clobTokenIds") or market.get("tokens") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        if isinstance(x, dict):
            out.append(str(x.get("token_id") or x.get("tokenId") or ""))
        else:
            out.append(str(x))
    return [t for t in out if t]


def _parse_event(event: dict) -> Optional[EventInfo]:
    event_id = str(event.get("id") or event.get("slug", ""))
    slug = event.get("slug", "") or ""
    title = event.get("title", "") or ""
    start_date = None
    end_date = None
    for key in ("startDate", "startTime"):
        if event.get(key):
            try:
                start_date = datetime.fromisoformat(str(event[key]).replace("Z", "+00:00"))
                if start_date.tzinfo:
                    start_date = start_date.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
            break
    for key in ("endDate", "endDateIso"):
        if event.get(key):
            try:
                end_date = datetime.fromisoformat(str(event[key]).replace("Z", "+00:00"))
                if end_date.tzinfo:
                    end_date = end_date.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
            break

    markets: list[MarketInfo] = []
    for market in event.get("markets") or []:
        clob_ids = _parse_clob_token_ids(market)
        if len(clob_ids) >= 2:
            outcomes_raw = market.get("outcomes")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except Exception:
                    outcomes = ["Up", "Down"]
            else:
                outcomes = outcomes_raw or ["Up", "Down"]
            for i, token_id in enumerate(clob_ids[:2]):
                outcome = outcomes[i] if i < len(outcomes) else ("Up" if i == 0 else "Down")
                markets.append(
                    MarketInfo(
                        condition_id=market.get("conditionId", ""),
                        question=market.get("question", ""),
                        outcome=outcome,
                        token_id=token_id,
                        end_date_iso=market.get("endDate") or market.get("endDateIso"),
                    )
                )
        elif len(clob_ids) == 1:
            outcomes = market.get("outcomes") or ["Yes"]
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = ["Yes"]
            markets.append(
                MarketInfo(
                    condition_id=market.get("conditionId", ""),
                    question=market.get("question", ""),
                    outcome=outcomes[0] if outcomes else "Yes",
                    token_id=clob_ids[0],
                    end_date_iso=market.get("endDate") or market.get("endDateIso"),
                )
            )
    if not markets:
        return None
    return EventInfo(
        event_id=event_id,
        slug=slug,
        title=title,
        start_date=start_date,
        end_date=end_date,
        markets=markets,
    )


def _fetch_event_by_slug(slug: str, base_url: str) -> Optional[dict]:
    url = f"{base_url.rstrip('/')}/events/slug/{slug}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("fetch_event_by_slug %s: %s", slug, e)
        return None


def discover_btc_5m_event(config: Optional[Config] = None) -> tuple[Optional[EventInfo], str]:
    """
    Resolve the current active Bitcoin 5m Up/Down event.
    Gamma slug_contains does NOT return these markets; use public-search + slug fetch.
    Returns (EventInfo, message) — message explains failure if None.
    """
    cfg = config or Config.from_env()
    base = cfg.gamma_api_base
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1) public-search for active 5m windows
    try:
        r = requests.get(
            f"{base.rstrip('/')}/public-search",
            params={"q": "btc-updown-5m", "events_status": "active", "limit": 30},
            timeout=15,
        )
        r.raise_for_status()
        events = (r.json() or {}).get("events") or []
    except Exception as e:
        logger.warning("public-search failed: %s", e)
        events = []

    candidates: list[tuple[str, Optional[datetime], Optional[datetime]]] = []
    for ev in events:
        slug = (ev.get("slug") or "").strip()
        m = re.match(r"^btc-updown-5m-(\d+)$", slug)
        if not m:
            continue
        end_dt = start_dt = None
        if ev.get("endDate"):
            try:
                end_dt = datetime.fromisoformat(str(ev["endDate"]).replace("Z", "+00:00"))
                if end_dt.tzinfo:
                    end_dt = end_dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
        if ev.get("startDate"):
            try:
                start_dt = datetime.fromisoformat(str(ev["startDate"]).replace("Z", "+00:00"))
                if start_dt.tzinfo:
                    start_dt = start_dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
        if start_dt is None or end_dt is None:
            try:
                ts = int(m.group(1))
                start_dt = start_dt or datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
                end_dt = end_dt or datetime.fromtimestamp(ts + 300, tz=timezone.utc).replace(tzinfo=None)
            except (ValueError, OSError):
                pass
        candidates.append((slug, start_dt, end_dt))

    def try_slug(slug: str) -> Optional[EventInfo]:
        full = _fetch_event_by_slug(slug, base)
        if not full:
            return None
        return _parse_event(full)

    def sort_key(item):
        slug, start_dt, end_dt = item
        # Drop past: window already ended
        if end_dt and now_utc >= end_dt:
            return (2, 0)
        # Current window: start <= now < end — prefer these, latest start first
        if start_dt and end_dt and start_dt <= now_utc < end_dt:
            return (0, -start_dt.timestamp())
        # Future window: start > now — want earliest next
        if start_dt and start_dt > now_utc:
            return (1, start_dt.timestamp())
        return (2, 0)

    candidates.sort(key=sort_key)

    # Prefer current 5m window, then next; skip past
    for slug, start_dt, end_dt in candidates:
        if end_dt and now_utc >= end_dt:
            continue
        info = try_slug(slug)
        if not info or not info.markets:
            continue
        if start_dt and now_utc < start_dt:
            continue
        return info, "ok"

    # Any candidate with valid markets (fallback)
    for slug, _, _ in candidates:
        info = try_slug(slug)
        if info and info.markets:
            return info, "ok"

    # 2) Time-bucket fallback: try slug btc-updown-5m-{unix}; prefer current bucket
    now_ts = int(time.time())
    # Current 5m bucket (aligned to 5 min), then next
    bucket = (now_ts // 300) * 300
    for ts in [bucket, bucket + 300, bucket - 300, bucket + 600, bucket - 600]:
        slug = f"btc-updown-5m-{ts}"
        full = _fetch_event_by_slug(slug, base)
        if full and full.get("active") and not full.get("closed"):
            info = _parse_event(full)
            if info and info.markets:
                # Only return if window not ended
                if info.end_date and now_utc >= info.end_date:
                    continue
                return info, "ok"

    return None, (
        "No active Bitcoin 5m event found. Polymarket may be between windows or the Gamma API changed."
    )


def get_current_btc_5m_event(config: Optional[Config] = None) -> Optional[EventInfo]:
    """Backward-compatible: discover current event."""
    info, _ = discover_btc_5m_event(config)
    return info


def get_next_btc_5m_event(config: Optional[Config] = None) -> Optional[EventInfo]:
    return get_current_btc_5m_event(config)
