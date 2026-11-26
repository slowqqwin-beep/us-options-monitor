#!/usr/bin/env python3
"""Fetch dual-pool (US $96k) dashboard data for the Muse dashboard + Muse.

Sources: yfinance (Yahoo) daily bars, 2y history.
  - VIX light (three-colour hard rules, no exceptions):
      RED if VIX/VIX3M > 1.05, or ratio >= 1.00 and today's VIX >=
      yesterday's VIX. YELLOW if 1.00 <= ratio <= 1.05 and today's VIX <
      yesterday's VIX and today's VIX < its 5-day average. GREEN only if
      ratio < 1.00. VIX absolute level is recorded for observation only
      and is never a hard threshold. Any case matching none of the three
      defaults to RED (conservative). The old panic-top-recovery green
      is removed: a fall from a 10-day peak below the 5-day average no
      longer grants green while the curve is still inverted.
  - Right pool pillars: SMH, DXJ, XLE, IAU, PDBC. For each: latest close,
    SMA200, 126-day and 252-day returns, momentum score
    (ret126 + ret252) / 2, eligible = above SMA200 (with the checklist's
    +/-1% buffer applied by the dashboard, which knows holdings) and
    momentum above the half-year T-bill hurdle.
  - Hurdle: ^IRX (13-week T-bill yield, annual %) / 2; falls back to the
    checklist's 1.9% constant and flags it when ^IRX is unavailable.

Fail-stop: any missing price series -> exit non-zero and write nothing.
No silent fallback prices. The GitHub Actions workflow only commits when
this script exits 0. A Claude Agent Chinese brief written into the previous
data file as top-level `summary_cn` is carried over into the regenerated
file, so the twice-daily rewrite never wipes it.
"""
import argparse
import datetime as dt
import json
import math
import sys

import yfinance as yf

PILLARS = ["SMH", "DXJ", "XLE", "IAU", "PDBC"]
SLOT_USD = 15519  # right pool $46,558 / 3, rounded down
HURDLE_FALLBACK_PCT = 1.9


def fetch_closes(symbol, period="2y"):
    """Return list of (date_str, adj_close) sorted ascending, or None."""
    try:
        h = yf.Ticker(symbol).history(period=period, interval="1d")
    except Exception as e:
        print(f"ERROR {symbol}: {e}", file=sys.stderr)
        return None
    if h is None or h.empty:
        print(f"ERROR {symbol}: empty history", file=sys.stderr)
        return None
    col = "Adj Close" if "Adj Close" in h.columns else "Close"
    out = []
    for idx, val in h[col].dropna().items():
        try:
            v = float(val)
        except Exception:
            continue
        if math.isfinite(v) and v > 0:
            out.append((idx.strftime("%Y-%m-%d"), v))
    out.sort()
    return out or None


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ret_n(values, n):
    """Total return over the last n trading days: close / close[n bars ago] - 1."""
    if len(values) <= n:
        return None
    return values[-1] / values[-1 - n] - 1.0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/dual_pool.json")
    p.add_argument("--history", default="data/dual_pool_history.jsonl")
    a = p.parse_args()

    series = {}
    for sym in ["^VIX", "^VIX3M", "^IRX"] + PILLARS:
        s = fetch_closes(sym)
        if s is None:
            print(f"FATAL: no data for {sym}", file=sys.stderr)
            return 1
        series[sym] = s

    # --- VIX light: three-colour hard rules ------------------------------
    vix_closes = [v for _, v in series["^VIX"]]
    vix3m_closes = [v for _, v in series["^VIX3M"]]
    vix = vix_closes[-1]
    vix_prev = vix_closes[-2]
    vix3m = vix3m_closes[-1]
    ratio = vix / vix3m
    vix_ma5 = sma(vix_closes, 5)
    if ratio > 1.05 or (ratio >= 1.00 and vix >= vix_prev):
        light = "red"
    elif ratio < 1.00:
        light = "green"
    elif (
        1.00 <= ratio <= 1.05
        and vix < vix_prev
        and vix_ma5 is not None
        and vix < vix_ma5
    ):
        light = "yellow"
    else:
        # Matches none of the three rules (e.g. ratio 1.00-1.05 with VIX
        # falling but still above its 5-day average): default to red
        # rather than grant an unconfirmed yellow/green.
        light = "red"
    right_defensive = ratio > 1.02

    # --- T-bill hurdle (half-year) ---------------------------------------
    irx = series["^IRX"][-1][1]
    hurdle = irx / 2.0
    hurdle_source = "^IRX/2"
    if not (0.0 < hurdle < 10.0):
        hurdle = HURDLE_FALLBACK_PCT
        hurdle_source = "constant 1.9% (checklist)"

    # --- Right-pool pillars ----------------------------------------------
    as_of = series["^VIX"][-1][0]
    pillars = []
    for t in PILLARS:
        closes = [v for d, v in series[t]]
        dates = [d for d, v in series[t]]
        close = closes[-1]
        s200 = sma(closes, 200)
        r126 = ret_n(closes, 126)
        r252 = ret_n(closes, 252)
        if s200 is None or r126 is None or r252 is None:
            print(f"FATAL: {t} has only {len(closes)} bars, need 253+", file=sys.stderr)
            return 1
        mom = (r126 + r252) / 2.0
        pillars.append(
            {
                "ticker": t,
                "as_of": dates[-1],
                "close": round(close, 2),
                "sma200": round(s200, 2),
                "vs_sma200_pct": round((close / s200 - 1.0) * 100, 2),
                "ret126_pct": round(r126 * 100, 2),
                "ret252_pct": round(r252 * 100, 2),
                "momentum_pct": round(mom * 100, 2),
                "eligible": bool(close >= s200 and mom * 100 > hurdle),
            }
        )

    eligible = sorted(
        [x for x in pillars if x["eligible"]],
        key=lambda x: x["momentum_pct"],
        reverse=True,
    )
    slots = [x["ticker"] for x in eligible[:3]]
    while len(slots) < 3:
        slots.append(None)  # null = parked in T-Bills / BIL haven

    now_utc = dt.datetime.now(dt.timezone.utc)
    payload = {
        "as_of": as_of,
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "yfinance",
        "vix": {
            "vix": round(vix, 2),
            "vix_prev": round(vix_prev, 2),
            "vix3m": round(vix3m, 2),
            "ratio": round(ratio, 4),
            "vix_ma5": round(vix_ma5, 2) if vix_ma5 else None,
            "light": light,
            "right_pool_defensive": bool(right_defensive),
        },
        "hurdle_half_year_pct": round(hurdle, 2),
        "hurdle_source": hurdle_source,
        "slot_usd": SLOT_USD,
        "pillars": pillars,
        "slots": slots,
    }

    # Preserve a Claude Agent Chinese brief (summary_cn) written into the
    # previous file: this script rewrites the file twice daily, and a fresh
    # write must not wipe the brief.
    try:
        with open(a.out, encoding="utf-8") as f:
            prev = json.load(f)
        if isinstance(prev.get("summary_cn"), str) and prev["summary_cn"].strip():
            payload["summary_cn"] = prev["summary_cn"]
    except (OSError, ValueError):
        pass

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    line = {
        "date": as_of,
        "vix": round(vix, 2),
        "vix3m": round(vix3m, 2),
        "ratio": round(ratio, 4),
        "light": light,
        "slots": slots,
    }
    with open(a.history, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(json.dumps(line, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
