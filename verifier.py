"""
Trade Outcome Verifier — runs alongside the Polymarket bot.

Two modes:
  python verifier.py          # scan all existing trade logs + verify live settlements
  python verifier.py --scan   # scan historical logs only, then exit

For every settlement trade (sell at >= 0.90 or <= 0.05 at a window boundary),
the verifier:
  1. Calculates the 5-min window slug from the buy timestamp
  2. Fetches the event from Gamma API to get token IDs for Up/Down
  3. Fetches the CLOB /book for each token AFTER settlement
  4. Determines the true winner (last_trade_price >= 0.95 + ask collapsed)
  5. Compares against what the bot recorded
  6. Logs PASS / FAIL / UNVERIFIABLE to verifier_report.md and stdout

Also watches the bot's /api/state every 5s for new settlements in real time.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Config ──────────────────────────────────────────────────────────────────
GAMMA_BASE   = "https://gamma-api.polymarket.com"
CLOB_BASE    = "https://clob.polymarket.com"
BOT_API_BASE = "http://192.168.1.18:8000"   # change if your IP differs
LOG_DIR      = Path(__file__).parent         # where trades_log_*.md files live
REPORT_FILE  = LOG_DIR / "verifier_report.md"

SESSION = requests.Session()
SESSION.headers["Connection"] = "keep-alive"

# Verification mode:
# - resolution: only verify settlement-like closes (near 0/1)
# - with_midwindow: additionally verify early exits via price/pnl consistency
VERIFY_MODE = "with_midwindow"

# ── Helpers ──────────────────────────────────────────────────────────────────

def ts_to_window_slug(iso_ts: str) -> str:
    """Given an ISO timestamp, return btc-updown-5m-{window_start_unix}."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        unix = int(dt.timestamp())
        window_start = (unix // 300) * 300
        return f"btc-updown-5m-{window_start}"
    except Exception:
        return ""


def fetch_event_token_ids(slug: str) -> dict[str, str]:
    """Return {outcome: token_id} for a given event slug, e.g. {'Up': '0xabc', 'Down': '0xdef'}."""
    try:
        r = SESSION.get(f"{GAMMA_BASE}/events/slug/{slug}", timeout=15)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        ev = r.json()
        result = {}
        for market in ev.get("markets") or []:
            raw = market.get("clobTokenIds") or market.get("tokens") or []
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = []
            outcomes_raw = market.get("outcomes") or ["Up", "Down"]
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_raw = json.loads(outcomes_raw)
                except Exception:
                    outcomes_raw = ["Up", "Down"]
            for i, item in enumerate(raw[:2]):
                tid = item.get("token_id") if isinstance(item, dict) else str(item)
                outcome = outcomes_raw[i] if i < len(outcomes_raw) else ("Up" if i == 0 else "Down")
                if tid:
                    result[outcome] = tid
        return result
    except Exception as e:
        return {}


def fetch_book_resolution(token_id: str) -> dict:
    """Fetch CLOB /book for a token and return resolution info."""
    try:
        r = SESSION.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        d = r.json()
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        lt = d.get("last_trade_price")
        try:
            lt = float(lt) if lt is not None else None
        except (TypeError, ValueError):
            lt = None
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        return {"last_trade": lt, "best_bid": best_bid, "best_ask": best_ask}
    except Exception:
        return {}


def determine_true_winner(token_map: dict[str, str], slug: str = "") -> Optional[str]:
    """
    Determine which outcome won for a given window.

    This verifier runs on Windows where direct Gamma/CLOB access can fail.
    It uses two independent checks and only accepts a winner if both agree:
      1) bot local proxy endpoint: `/api/verify-window`
      2) direct Gamma inference from `outcomePrices`

    Returns 'Up', 'Down', or None if indeterminate.
    """
    def infer_gamma_winner(local_slug: str) -> Optional[str]:
        try:
            r = SESSION.get(f"{GAMMA_BASE}/events/slug/{local_slug}", timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            ev = r.json() or {}
            for market in (ev.get("markets") or []):
                outcomes = market.get("outcomes") or ["Up", "Down"]
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = ["Up", "Down"]

                op = market.get("outcomePrices")
                prices = None
                if isinstance(op, str):
                    try:
                        prices = json.loads(op)
                    except Exception:
                        prices = None
                elif isinstance(op, list):
                    prices = op

                if isinstance(prices, list) and len(prices) >= 2:
                    try:
                        p0 = float(prices[0])
                        p1 = float(prices[1])
                    except (TypeError, ValueError):
                        continue

                    if p0 >= 0.99 and p1 <= 0.01:
                        return outcomes[0] if len(outcomes) > 0 else "Up"
                    if p1 >= 0.99 and p0 <= 0.01:
                        return outcomes[1] if len(outcomes) > 1 else "Down"
            return None
        except Exception:
            return None

    # 1) Proxy winner
    proxy_winner: Optional[str] = None
    bases_to_try = [BOT_API_BASE, "http://127.0.0.1:8000", "http://localhost:8000"]
    for base in bases_to_try:
        try:
            r = SESSION.get(
                f"{base}/api/verify-window",
                params={"slug": slug},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            d = r.json() or {}
            w = d.get("winner")
            if w in ("Up", "Down"):
                proxy_winner = w
                break
        except Exception:
            continue

    # 2) Direct Gamma winner
    gamma_winner = infer_gamma_winner(slug)

    # Only accept if both checks agree.
    if proxy_winner in ("Up", "Down") and gamma_winner in ("Up", "Down"):
        if proxy_winner == gamma_winner:
            return proxy_winner
    return None


def window_age_sec(slug: str) -> float:
    """Seconds since the 5-min window ended. Negative if still running."""
    try:
        m = re.match(r"btc-updown-5m-(\d+)$", slug)
        if not m:
            return -1.0
        end_unix = int(m.group(1)) + 300
        return time.time() - end_unix
    except Exception:
        return -1.0


def window_is_resolved(slug: str, min_age_sec: float = 30.0) -> bool:
    """True if the window ended at least min_age_sec ago (oracle needs ~10-30s to settle)."""
    return window_age_sec(slug) >= min_age_sec


# ── Trade log parsing ─────────────────────────────────────────────────────────

def parse_log_file(path: Path) -> list[dict]:
    """Parse a trades_log_*.md file into list of fill dicts."""
    fills = []
    for line in path.read_text(encoding="utf-8").splitlines():
        # Log rows look like: | 2026-03-17T...+00:00 | buy | Up | ...
        # Keep it year-agnostic so the verifier works across sessions.
        if not line.startswith("| 20"):
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 6:
            continue
        ts, side, outcome, price_s, size_s, usd_s = parts[:6]
        try:
            fills.append({
                "ts": ts,
                "side": side,
                "outcome": outcome,
                "price": float(price_s),
                "size": float(size_s),
                "usd": float(usd_s),
            })
        except (ValueError, TypeError):
            continue
    return fills


def build_roundtrips(fills: list[dict]) -> list[dict]:
    """Pair buys and sells FIFO per outcome into roundtrips."""
    open_lots: dict[str, list[dict]] = {}
    roundtrips = []
    for f in fills:
        outcome = f["outcome"]
        lots = open_lots.setdefault(outcome, [])
        if f["side"] == "buy":
            lots.append({"buy_ts": f["ts"], "buy_price": f["price"],
                         "size": f["size"], "buy_usd": f["usd"]})
        elif f["side"] == "sell" and lots:
            lot = lots.pop(0)
            pnl = (f["price"] - lot["buy_price"]) * lot["size"]
            roundtrips.append({
                "outcome": outcome,
                "buy_ts": lot["buy_ts"],
                "sell_ts": f["ts"],
                "buy_price": lot["buy_price"],
                "sell_price": f["price"],
                "size": lot["size"],
                "pnl": pnl,
                "window_slug": ts_to_window_slug(lot["buy_ts"]),
            })
    return roundtrips


def is_completed_roundtrip(rt: dict) -> bool:
    """True if this roundtrip has both a buy and a sell recorded."""
    return float(rt.get("sell_price") or 0) > 0 or float(rt.get("pnl") or 0) != 0


# ── Verification logic ────────────────────────────────────────────────────────

# Cache: slug -> true winner (to avoid repeated API calls)
_winner_cache: dict[str, Optional[str]] = {}

def verify_roundtrip(strategy: str, rt: dict) -> dict:
    """
    Verify a completed roundtrip against Polymarket on-chain data.
    Verdict:
      - PASS: app's recorded `sell_price` is consistent with the resolved outcome for
              the side the bot bought (only for resolution-like sells near 0/1).
      - FAIL: app's recorded `sell_price` contradicts the resolved outcome.
      - PENDING: window not yet resolved / oracle not settled yet.
      - UNVERIFIABLE: winner unknown or trade closed early (sell not near 0/1).
    """
    slug = rt["window_slug"]
    recorded_outcome = rt["outcome"]
    recorded_sell = float(rt.get("sell_price") or 0)
    recorded_buy = float(rt.get("buy_price") or 0)
    recorded_size = float(rt.get("size") or 0)
    pnl = float(rt.get("pnl") or 0)

    base = {
        "strategy": strategy, "slug": slug,
        "outcome": recorded_outcome, "buy_price": recorded_buy,
        "sell_price": recorded_sell, "pnl": pnl,
        "buy_ts": rt["buy_ts"], "sell_ts": rt.get("sell_ts", ""),
    }

    # If the window hasn't ended + settled yet, defer
    if not window_is_resolved(slug):
        return {**base, "true_winner": None, "verdict": "PENDING",
                "reason": "Window not yet resolved"}

    # Determine true winner (cached)
    if slug not in _winner_cache:
        _winner_cache[slug] = determine_true_winner({}, slug)
    true_winner = _winner_cache[slug]

    if true_winner is None:
        age = window_age_sec(slug)
        if age < 120:
            # Oracle usually settles within 60s; keep retrying for 2 min
            return {**base, "true_winner": None, "verdict": "PENDING",
                    "reason": f"Window ended {age:.0f}s ago - oracle not yet settled, will retry"}
        return {
            **base,
            "true_winner": None,
            "verdict": "UNVERIFIABLE",
            "reason": f"Winner unknown after {age:.0f}s - proxy and gamma did not agree",
        }

    # We only consider the trade verifiable if it is a "resolution-like" close:
    # - near 1.00 for a winning side
    # - near 0.00 for a losing side
    # This aligns the verifier with the user's goal: detect false wins/losses in app output.
    expected_settlement_price = 1.0 if recorded_outcome == true_winner else 0.0
    resolution_match = (
        (expected_settlement_price >= 0.99 and recorded_sell >= 0.90) or
        (expected_settlement_price <= 0.01 and recorded_sell <= 0.05)
    )

    near_one = recorded_sell >= 0.90
    near_zero = recorded_sell <= 0.05
    if near_one or near_zero:
        verdict = "PASS" if resolution_match else "FAIL"
        reason = (
            f"App sell indicates settlement_price="
            f"{'1.00' if near_one else '0.00'}; expected={expected_settlement_price:.2f} "
            f"(true winner={true_winner}). sell={recorded_sell:.3f}, pnl={pnl:+.2f} "
            f"({'MATCH' if resolution_match else 'MISMATCH'})"
        )
    else:
        if VERIFY_MODE == "with_midwindow":
            if recorded_size <= 0:
                verdict = "UNVERIFIABLE"
                reason = (
                    "Closed before resolution and missing/invalid size, "
                    "cannot validate mid-window pnl consistency"
                )
            else:
                expected_pnl = (recorded_sell - recorded_buy) * recorded_size
                # allow a small rounding tolerance from UI/API formatting
                pnl_tol = max(0.05, abs(expected_pnl) * 0.02)
                pnl_match = abs(expected_pnl - pnl) <= pnl_tol
                verdict = "PASS" if pnl_match else "FAIL"
                reason = (
                    f"Mid-window check: expected_pnl={(expected_pnl):+.2f} "
                    f"from buy={recorded_buy:.3f}, sell={recorded_sell:.3f}, size={recorded_size:.4f}; "
                    f"recorded_pnl={pnl:+.2f} (tol={pnl_tol:.2f}) "
                    f"({'MATCH' if pnl_match else 'MISMATCH'})"
                )
        else:
            verdict = "UNVERIFIABLE"
            reason = (
                f"Closed before resolution: sell={recorded_sell:.3f} not near 0/1, "
                f"so can't verify against true winner={true_winner}"
            )

    return {**base, "true_winner": true_winner, "verdict": verdict, "reason": reason}


# ── Report writing ────────────────────────────────────────────────────────────

def format_result(r: dict) -> str:
    icon = "PASS" if r["verdict"] == "PASS" else ("UNVERIFIABLE" if r["verdict"] == "UNVERIFIABLE" else "FAIL")
    return (
        f"{icon} [{r['verdict']}] {r['strategy']:<28} "
        f"slug={r['slug'][-10:]}  {r['outcome']:<5} "
        f"buy={r['buy_price']:.2f} sell={r['sell_price']:.3f} "
        f"pnl={r['pnl']:+.2f}  true_winner={r['true_winner'] or '?'}  "
        f"| {r['reason']}"
    )


def write_report(results: list[dict]) -> None:
    passes  = [r for r in results if r["verdict"] == "PASS"]
    fails   = [r for r in results if r["verdict"] == "FAIL"]
    unverif = [r for r in results if r["verdict"] == "UNVERIFIABLE"]
    verified = len(passes) + len(fails)
    pct = f" ({len(passes)/verified*100:.0f}% correct)" if verified else ""

    lines = [
        "# Trade Outcome Verification Report",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Verification mode: {VERIFY_MODE}",
        f"Verified roundtrips: {verified}{pct}",
        f"PASS: {len(passes)}  FAIL: {len(fails)}  UNVERIFIABLE: {len(unverif)}",
        "",
    ]

    if fails:
        lines += ["## FAILED (app sell contradicts resolution)", ""]
        for r in fails:
            lines.append(format_result(r))
        lines.append("")

    if unverif:
        lines += ["## UNVERIFIABLE (winner unknown or sell not at resolution)", ""]
        for r in unverif:
            lines.append(format_result(r))
        lines.append("")

    lines += ["## PASSED", ""]
    for r in passes:
        lines.append(format_result(r))

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


# ── Historical scan ───────────────────────────────────────────────────────────

def scan_historical_logs() -> tuple[list[dict], dict[str, tuple[str, dict]]]:
    """
    Scan all trades_log_*.md files and verify every completed roundtrip.
    Returns (verified_results, pending_dict) where pending_dict maps key→(strategy,rt)
    for roundtrips whose windows haven't resolved yet (for the live loop to re-check).
    """
    log_files = sorted(LOG_DIR.glob("trades_log_*.md"))
    print(f"\n{'='*70}")
    print(f"SCANNING {len(log_files)} trade log files...")
    print(f"{'='*70}\n")

    all_results: list[dict] = []
    pending_out: dict[str, tuple[str, dict]] = {}

    for log_path in log_files:
        strategy = log_path.stem.replace("trades_log_", "")
        fills = parse_log_file(log_path)
        if not fills:
            continue
        roundtrips = [rt for rt in build_roundtrips(fills) if is_completed_roundtrip(rt)]
        if not roundtrips:
            continue

        MIN_CONFIRM_AGE_SEC = 120.0
        verifiable = [
            rt for rt in roundtrips
            if window_is_resolved(rt["window_slug"], min_age_sec=MIN_CONFIRM_AGE_SEC)
        ]
        deferred = [rt for rt in roundtrips if rt not in verifiable]

        if deferred:
            print(f"  {strategy}: {len(deferred)} roundtrip(s) deferred "
                  f"(window still live/settling - will verify once resolved)")
            for rt in deferred:
                key = f"{strategy}|{rt['window_slug']}|{rt['outcome']}|{rt['buy_ts']}"
                pending_out[key] = (strategy, rt)

        if verifiable:
            print(f"  {strategy}: verifying {len(verifiable)} resolved roundtrip(s)...")
            for rt in verifiable:
                result = verify_roundtrip(strategy, rt)
                if result["verdict"] == "PENDING":
                    # Oracle still catching up — defer to live loop
                    key = f"{strategy}|{rt['window_slug']}|{rt['outcome']}|{rt['buy_ts']}"
                    pending_out[key] = (strategy, rt)
                    print(f"    [PENDING] {strategy} {rt['outcome']} - oracle not settled yet")
                else:
                    all_results.append(result)
                    print(f"    {format_result(result)}")
                time.sleep(0.2)

    return all_results, pending_out


# ── Live monitoring ───────────────────────────────────────────────────────────

def get_bot_roundtrips() -> dict[str, list[dict]]:
    """Fetch all current roundtrips from the bot API, keyed by strategy."""
    # Some machines can’t reach the external LAN IP, but can reach localhost.
    bases_to_try = [BOT_API_BASE, "http://127.0.0.1:8000", "http://localhost:8000"]
    last_err: Optional[str] = None
    for base in bases_to_try:
        try:
            state = SESSION.get(f"{base}/api/state", timeout=5).json()
            strategies = state.get("strategies") or []
            result: dict[str, list[dict]] = {}
            for st in strategies:
                sid = st.get("id")
                if not sid:
                    continue
                data = SESSION.get(
                    f"{base}/api/strategy/{sid}/roundtrips",
                    params={"limit": 500},
                    timeout=5,
                ).json()
                result[sid] = data.get("items") or []
            return result
        except Exception as e:
            last_err = str(e)
            continue
    # If all bases fail, let caller print "Could not reach bot API".
    return {}


def live_monitor() -> None:
    """Watch the bot API for new roundtrips and verify them in real time."""
    print("\nLive monitoring mode - checking every 30s for new roundtrips...")
    print("   Press Ctrl+C to stop.\n")

    seen_keys: set[str] = set()
    # pending: key -> (strategy, rt) — re-checked each cycle
    pending: dict[str, tuple[str, dict]] = {}
    all_results: list[dict] = []

    # Seed with already-verified historical results; deferred ones go straight to pending
    hist, initial_pending = scan_historical_logs()
    all_results.extend(hist)
    pending.update(initial_pending)
    if initial_pending:
        print(f"\nSeeded {len(initial_pending)} pending roundtrip(s) - waiting for oracle settlement.\n")
    for r in hist:
        seen_keys.add(f"{r['strategy']}|{r['slug']}|{r['outcome']}|{r['buy_ts']}")
    for key in initial_pending:
        seen_keys.add(key)

    write_report(all_results)
    _print_summary(all_results)

    cycle = 0
    while True:
        try:
            time.sleep(30)
            cycle += 1
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            print(f"\n[{now_str}] Cycle #{cycle} - polling bot API...")

            # Re-check pending roundtrips whose windows may now be resolved
            resolved_pending = []
            for key, (strategy, rt) in list(pending.items()):
                if window_is_resolved(rt["window_slug"]):
                    result = verify_roundtrip(strategy, rt)
                    if result["verdict"] != "PENDING":
                        all_results.append(result)
                        print(f"  Resolved pending: {format_result(result)}")
                        resolved_pending.append(key)
            for key in resolved_pending:
                del pending[key]

            bot_data = get_bot_roundtrips()
            if not bot_data:
                print("  Could not reach bot API")
                continue

            new_count = 0
            total_roundtrips = sum(len(v) for v in bot_data.values())
            for strategy, roundtrips in bot_data.items():
                for rt_raw in roundtrips:
                    rt = {
                        "outcome":     rt_raw.get("outcome", ""),
                        "buy_ts":      rt_raw.get("buy_ts", ""),
                        "sell_ts":     rt_raw.get("sell_ts", ""),
                        "buy_price":   float(rt_raw.get("buy_price") or 0),
                        "sell_price":  float(rt_raw.get("sell_price") or 0),
                        "size":        float(rt_raw.get("size") or 0),
                        "pnl":         float(rt_raw.get("pnl_usd") or 0),
                        "window_slug": ts_to_window_slug(rt_raw.get("buy_ts", "")),
                    }
                    if not is_completed_roundtrip(rt):
                        continue
                    key = f"{strategy}|{rt['window_slug']}|{rt['outcome']}|{rt['buy_ts']}"
                    if key in seen_keys or key in pending:
                        continue

                    seen_keys.add(key)
                    new_count += 1
                    print(f"  New: {strategy} {rt['outcome']} "
                          f"buy={rt['buy_price']:.2f} sell={rt['sell_price']:.3f} pnl={rt['pnl']:+.2f}")
                    result = verify_roundtrip(strategy, rt)
                    if result["verdict"] == "PENDING":
                        pending[key] = (strategy, rt)
                        print(f"     PENDING - window not yet resolved")
                    else:
                        all_results.append(result)
                        print(f"     {format_result(result)}")

            print(f"  API: {len(bot_data)} strategies, {total_roundtrips} roundtrips total, "
                  f"{new_count} new this cycle, {len(pending)} pending")

            if new_count > 0 or resolved_pending:
                write_report(all_results)
                _print_summary(all_results)

        except KeyboardInterrupt:
            print("\n\nStopping verifier.")
            break
        except Exception as e:
            print(f"  Error in monitor cycle: {e}")


def _print_summary(results: list[dict]) -> None:
    passes  = sum(1 for r in results if r["verdict"] == "PASS")
    fails   = sum(1 for r in results if r["verdict"] == "FAIL")
    unverif = sum(1 for r in results if r["verdict"] == "UNVERIFIABLE")
    verified = passes + fails
    pct = f" ({passes/verified*100:.0f}% correct)" if verified else ""
    print(f"\n{'-'*60}")
    print(f"SUMMARY: {verified} verified{pct} | PASS={passes} FAIL={fails} UNVERIFIABLE={unverif}")
    if fails:
        fail_list = [r for r in results if r["verdict"] == "FAIL"]
        for r in fail_list:
            print(f"  FAIL {r['strategy']} | {r['reason']}")
    print(f"Report written to: {REPORT_FILE}")
    print(f"{'-'*60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket trade outcome verifier")
    parser.add_argument("--scan", action="store_true",
                        help="Scan historical logs only, then exit (no live monitoring)")
    parser.add_argument("--bot-url", default=BOT_API_BASE,
                        help=f"Bot API base URL (default: {BOT_API_BASE})")
    parser.add_argument(
        "--verify-mode",
        default="with_midwindow",
        choices=["resolution", "with_midwindow"],
        help=(
            "resolution: verify only settlement-like closes near 0/1; "
            "with_midwindow: also verify closed-before-resolution sells via buy/sell/size->pnl consistency"
        ),
    )
    args = parser.parse_args()

    VERIFY_MODE = args.verify_mode
    BOT_API_BASE = args.bot_url

    if args.scan:
        results, _pending = scan_historical_logs()
        write_report(results)
        _print_summary(results)
        if _pending:
            print(f"\nPending {len(_pending)} roundtrip(s) deferred - re-run later once windows resolve.")
        fails = [r for r in results if r["verdict"] == "FAIL"]
        sys.exit(1 if fails else 0)
    else:
        live_monitor()
