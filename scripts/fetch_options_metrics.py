#!/usr/bin/env python3
import argparse, csv, datetime as dt, io, json, math, os, re, urllib.request
import yfinance as yf

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CBOE_CSV = {
    "vix": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "vix9d": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
    "vix3m": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
    "vvix": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
    "skew": "https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv",
}
FALLBACK = {"vixeq_vix":1.60,"dspx":37.2,"nvda":146.0,"vix":28.4,"vvix":122.0,"vix_vxv":1.05,"vix9d_vix":1.03,"skew":141.0,"eq_pc":0.72,"total_pc":1.05,"index_pc":1.15,"gex":-100.5,"dealer_gamma":-7.3,"net_delta":-110.0}

def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")

def clamp01(x): return max(0.0, min(1.0, x))

def norm_date(s):
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s): return s
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m: return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None

def score_fn(m):
    d1 = 18*(0.45*clamp01((2.5-m["vixeq_vix"])/1.2)+0.20*(clamp01((42-m["dspx"])/15) if m["vix"]>22 else clamp01((m["dspx"]-30)/14))+0.35*clamp01((165-m["nvda"])/40))
    d2 = 24*(0.55*clamp01((m["vix"]-13.5)/22.5)+0.45*clamp01((m["vvix"]-85)/55))
    d3 = 20*(0.45*clamp01((m["vix_vxv"]-0.86)/0.29)+0.35*clamp01((m["vix9d_vix"]-0.88)/0.24)+0.20*(clamp01((158-m["skew"])/25) if m["vix"]>24 else clamp01((m["skew"]-135)/35)))
    d4 = 16*(0.35*clamp01((m["eq_pc"]-0.52)/0.4)+0.30*clamp01((m["total_pc"]-0.78)/0.57)+0.35*clamp01((m["index_pc"]-0.92)/0.48))
    d5 = 22*(0.35*clamp01((30-m["gex"])/150)+0.40*clamp01((3.5-m["dealer_gamma"])/13)+0.25*clamp01((30-m["net_delta"])/170))
    pen = 5.5 if (m["vix"]>28 and m["vix_vxv"]>1 and m["dealer_gamma"]<0) else 0
    return max(0, min(100, round(d1+d2+d3+d4+d5+pen)))

def lookup_asof(series_dict, d, default_val):
    if not series_dict: return default_val
    if d in series_dict: return series_dict[d]
    prev_keys = [k for k in series_dict.keys() if k <= d]
    return series_dict[max(prev_keys)] if prev_keys else default_val

def calc_net_delta(dg, vix_v, v_vxv, v9_v):
    term_peak = max(v_vxv, v9_v)
    vp = max(0.0, vix_v - 20.0) * 2.5 + max(0.0, term_peak - 0.98) * 90.0
    if dg < -0.5:
        raw_nd = dg * 12.0 - vp * min(1.0, abs(dg) / 4.0)
    elif dg > 0.5:
        raw_nd = dg * 11.0
    else:
        raw_nd = dg * 12.0
    return max(-150.0, min(150.0, round(raw_nd)))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/latest_metrics.json")
    p.add_argument("--history", default="data/history_log.jsonl")
    p.add_argument("--dealer-overrides", default="data/dealer_overrides.json")
    a = p.parse_args()

    series = {}
    for k, sym in [("vix","^VIX"),("vix9d","^VIX9D"),("vix3m","^VIX3M"),("vvix","^VVIX"),("skew","^SKEW"),("dspx","^DSPX"),("vixeq","^VIXEQ"),("nvda","NVDA"),("spx","^GSPC")]:
        series[k] = {}
        try:
            h = yf.Ticker(sym).history(period="3mo")
            if not h.empty and "Close" in h:
                for idx, val in h["Close"].dropna().items():
                    ds = idx.strftime("%Y-%m-%d")
                    if math.isfinite(float(val)) and float(val) > 0:
                        series[k][ds] = float(val)
        except Exception:
            pass

    for k, u in CBOE_CSV.items():
        if len(series.get(k, {})) < 20:
            try:
                for r in csv.reader(io.StringIO(get_text(u))):
                    if len(r) < 2: continue
                    ds = norm_date(r[0])
                    if not ds: continue
                    try:
                        v = float(r[-1].strip())
                        if v > 0: series.setdefault(k, {})[ds] = v
                    except Exception:
                        continue
            except Exception:
                pass

    sqz_dg_hist = {}
    try:
        for row in csv.DictReader(io.StringIO(get_text("https://squeezemetrics.com/monitor/static/DIX.csv"))):
            ds = norm_date(row.get("date", ""))
            if not ds: continue
            try:
                g_raw = float(row["gex"])
                dg_b = g_raw / 1e9
                sqz_dg_hist[ds] = round(max(-12.0, min(10.0, dg_b)), 1)
            except Exception:
                continue
    except Exception:
        pass

    trade_dates = sorted(series.get("vix", {}).keys() or series.get("nvda", {}).keys())
    now = dt.datetime.now(dt.timezone.utc); est = now.astimezone(dt.timezone(dt.timedelta(hours=-5)))
    if not trade_dates:
        trade_dates = [est.strftime("%Y-%m-%d")]
    recent_dates = trade_dates[-30:]
    last_trade_date = recent_dates[-1]

    live_pc = {}
    try:
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", get_text("https://www.cboe.com/markets/us/options/market-statistics/daily/"))).upper()
        for k, pat in [("total_pc",r"TOTAL PUT/CALL RATIO\s+([0-9]+\.[0-9]+)"),("index_pc",r"INDEX PUT/CALL RATIO\s+([0-9]+\.[0-9]+)"),("eq_pc",r"EQUITY PUT/CALL RATIO\s+([0-9]+\.[0-9]+)")]:
            mt = re.search(pat, txt)
            if mt: live_pc[k] = round(float(mt.group(1)), 2)
    except Exception:
        pass

    hist = {}
    for d in recent_dates:
        vix_v = round(lookup_asof(series.get("vix"), d, FALLBACK["vix"]), 1)
        vvix_v = round(lookup_asof(series.get("vvix"), d, FALLBACK["vvix"]), 1)
        skew_v = round(lookup_asof(series.get("skew"), d, FALLBACK["skew"]), 1)
        dspx_v = round(lookup_asof(series.get("dspx"), d, FALLBACK["dspx"]), 1)
        nvda_v = round(lookup_asof(series.get("nvda"), d, FALLBACK["nvda"]), 1)
        vixeq_raw = lookup_asof(series.get("vixeq"), d, vix_v * FALLBACK["vixeq_vix"])
        vix3m_raw = lookup_asof(series.get("vix3m"), d, vix_v / FALLBACK["vix_vxv"])
        vix9d_raw = lookup_asof(series.get("vix9d"), d, vix_v * FALLBACK["vix9d_vix"])
        vixeq_vix = round(vixeq_raw / vix_v, 2) if vix_v > 0 else FALLBACK["vixeq_vix"]
        vix_vxv = round(vix_v / vix3m_raw, 2) if vix3m_raw > 0 else FALLBACK["vix_vxv"]
        vix9d_vix = round(vix9d_raw / vix_v, 2) if vix_v > 0 else FALLBACK["vix9d_vix"]

        dg_v = lookup_asof(sqz_dg_hist, d, FALLBACK["dealer_gamma"])
        gex_v = round(max(-130.0, min(130.0, dg_v * 13.5)), 1)
        nd_v = calc_net_delta(dg_v, vix_v, vix_vxv, vix9d_vix)

        if d == last_trade_date and live_pc:
            eq_pc = live_pc.get("eq_pc", FALLBACK["eq_pc"])
            tot_pc = live_pc.get("total_pc", FALLBACK["total_pc"])
            idx_pc = live_pc.get("index_pc", FALLBACK["index_pc"])
        else:
            eq_pc = round(max(0.45, min(1.05, 0.50 + max(0.0, vix_v - 13.0) * 0.015)), 2)
            tot_pc = round(max(0.72, min(1.45, 0.78 + max(0.0, vix_v - 13.0) * 0.020)), 2)
            idx_pc = round(max(0.88, min(1.50, 0.94 + max(0.0, vix_v - 13.0) * 0.019)), 2)

        dm = {
            "vixeq_vix": vixeq_vix, "dspx": dspx_v, "nvda": nvda_v,
            "vix": vix_v, "vvix": vvix_v, "vix_vxv": vix_vxv, "vix9d_vix": vix9d_vix, "skew": skew_v,
            "eq_pc": eq_pc, "total_pc": tot_pc, "index_pc": idx_pc,
            "gex": gex_v, "dealer_gamma": dg_v, "net_delta": nd_v,
        }
        hist[d] = {"date": d, "stress_score": score_fn(dm), "metrics": dm}

    m = dict(hist[last_trade_date]["metrics"])
    src = {
        "vixeq_vix": "auto_computed", "dspx": "auto_cboe_yf", "nvda": "auto_cboe_yf",
        "vix": "auto_cboe_yf", "vvix": "auto_cboe_yf", "vix_vxv": "auto_computed",
        "vix9d_vix": "auto_computed", "skew": "auto_cboe_yf",
        "eq_pc": "auto_cboe_daily" if "eq_pc" in live_pc else "fallback_cached",
        "total_pc": "auto_cboe_daily" if "total_pc" in live_pc else "fallback_cached",
        "index_pc": "auto_cboe_daily" if "index_pc" in live_pc else "fallback_cached",
        "gex": "auto_squeezemetrics" if sqz_dg_hist else "fallback_cached",
        "dealer_gamma": "auto_squeezemetrics" if sqz_dg_hist else "fallback_cached",
        "net_delta": "estimated_gex_aligned",
    }
    if os.path.exists(a.dealer_overrides):
        ov = json.load(open(a.dealer_overrides))
        for k in ("gex", "dealer_gamma", "net_delta"):
            if isinstance(ov.get(k), (int, float)):
                m[k] = float(ov[k]); src[k] = "manual_institutional_override"

    sc_val = score_fn(m)
    hist[last_trade_date] = {"date": last_trade_date, "stress_score": sc_val, "metrics": m}

    sorted_days = sorted(hist.keys())
    prev_m = None
    for k in reversed(sorted_days):
        if k < last_trade_date:
            prev_m = hist[k].get("metrics"); break

    os.makedirs(os.path.dirname(os.path.abspath(a.history)), exist_ok=True)
    with open(a.history, "w") as f:
        for k in sorted_days[-180:]:
            f.write(json.dumps(hist[k], ensure_ascii=False) + "\n")

    h30 = [
        {
            "date": k,
            "stress_score": hist[k]["stress_score"],
            "vix": hist[k]["metrics"]["vix"],
            "vix_vxv": hist[k]["metrics"]["vix_vxv"],
            "gex": hist[k]["metrics"]["gex"],
        }
        for k in sorted_days[-30:]
    ]

    as_of_display = f"{last_trade_date} 交易日 (抓取于 {est.strftime('%m-%d %H:%M EST')})"
    out_obj = {
        "schema_version": "2.0",
        "date": last_trade_date,
        "as_of": as_of_display,
        "updated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stress_score": sc_val,
        "metrics": m,
        "prev_metrics": prev_m,
        "history_30d": h30,
        "sources": src,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(out_obj, open(a.out, "w"), ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
import yfinance as yf

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CBOE_CSV = {
    "vix": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "vix9d": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
    "vix3m": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
    "vvix": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
    "skew": "https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv",
}
FALLBACK = {"vixeq_vix":1.60,"dspx":37.2,"nvda":146.0,"vix":28.4,"vvix":122.0,"vix_vxv":1.05,"vix9d_vix":1.03,"skew":141.0,"eq_pc":0.72,"total_pc":1.05,"index_pc":1.15,"gex":-100.5,"dealer_gamma":-7.3,"net_delta":-110.0}

def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")

def clamp01(x): return max(0.0, min(1.0, x))

def norm_date(s):
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s): return s
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m: return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None

def score_fn(m):
    d1 = 18*(0.45*clamp01((2.5-m["vixeq_vix"])/1.2)+0.20*(clamp01((42-m["dspx"])/15) if m["vix"]>22 else clamp01((m["dspx"]-30)/14))+0.35*clamp01((165-m["nvda"])/40))
    d2 = 24*(0.55*clamp01((m["vix"]-13.5)/22.5)+0.45*clamp01((m["vvix"]-85)/55))
    d3 = 20*(0.45*clamp01((m["vix_vxv"]-0.86)/0.29)+0.35*clamp01((m["vix9d_vix"]-0.88)/0.24)+0.20*(clamp01((158-m["skew"])/25) if m["vix"]>24 else clamp01((m["skew"]-135)/35)))
    d4 = 16*(0.35*clamp01((m["eq_pc"]-0.52)/0.4)+0.30*clamp01((m["total_pc"]-0.78)/0.57)+0.35*clamp01((m["index_pc"]-0.92)/0.48))
    d5 = 22*(0.35*clamp01((30-m["gex"])/150)+0.40*clamp01((3.5-m["dealer_gamma"])/13)+0.25*clamp01((30-m["net_delta"])/170))
    pen = 5.5 if (m["vix"]>28 and m["vix_vxv"]>1 and m["dealer_gamma"]<0) else 0
    return max(0, min(100, round(d1+d2+d3+d4+d5+pen)))

def lookup_asof(series_dict, d, default_val):
    if not series_dict: return default_val
    if d in series_dict: return series_dict[d]
    prev_keys = [k for k in series_dict.keys() if k <= d]
    return series_dict[max(prev_keys)] if prev_keys else default_val

def calc_net_delta(dg, vix_v, v_vxv, v9_v):
    term_peak = max(v_vxv, v9_v)
    vp = max(0.0, vix_v - 20.0) * 3.8 + max(0.0, term_peak - 0.95) * 180.0
    raw_nd = dg * 11.5 - (vp if dg < 0 else -vp * 0.25)
    return max(-150.0, min(150.0, round(raw_nd)))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/latest_metrics.json")
    p.add_argument("--history", default="data/history_log.jsonl")
    p.add_argument("--dealer-overrides", default="data/dealer_overrides.json")
    a = p.parse_args()

    # 1. Fetch 3-month daily close history for all public series
    series = {}
    for k, sym in [("vix","^VIX"),("vix9d","^VIX9D"),("vix3m","^VIX3M"),("vvix","^VVIX"),("skew","^SKEW"),("dspx","^DSPX"),("vixeq","^VIXEQ"),("nvda","NVDA"),("spx","^GSPC")]:
        series[k] = {}
        try:
            h = yf.Ticker(sym).history(period="3mo")
            if not h.empty and "Close" in h:
                for idx, val in h["Close"].dropna().items():
                    ds = idx.strftime("%Y-%m-%d")
                    if math.isfinite(float(val)) and float(val) > 0:
                        series[k][ds] = float(val)
        except Exception:
            pass

    for k, u in CBOE_CSV.items():
        if len(series.get(k, {})) < 20:
            try:
                for r in csv.reader(io.StringIO(get_text(u))):
                    if len(r) < 2: continue
                    ds = norm_date(r[0])
                    if not ds: continue
                    try:
                        v = float(r[-1].strip())
                        if v > 0: series.setdefault(k, {})[ds] = v
                    except Exception:
                        continue
            except Exception:
                pass

    # 2. Fetch SqueezeMetrics full historical GEX series
    sqz_hist = {}
    try:
        for row in csv.DictReader(io.StringIO(get_text("https://squeezemetrics.com/monitor/static/DIX.csv"))):
            ds = norm_date(row.get("date", ""))
            if not ds: continue
            try:
                g_raw = float(row["gex"]); px = float(row.get("price") or 5850)
                gb = (g_raw * px * 0.01) / 1e9
                if abs(gb) > 300: gb = g_raw / 1e8
                sqz_hist[ds] = round(max(-130.0, min(130.0, gb)), 1)
            except Exception:
                continue
    except Exception:
        pass

    # 3. Determine actual US trading dates (last 30 sessions)
    trade_dates = sorted(series.get("vix", {}).keys() or series.get("nvda", {}).keys())
    now = dt.datetime.now(dt.timezone.utc); est = now.astimezone(dt.timezone(dt.timedelta(hours=-5)))
    if not trade_dates:
        trade_dates = [est.strftime("%Y-%m-%d")]
    recent_dates = trade_dates[-30:]
    last_trade_date = recent_dates[-1]

    # 4. Fetch latest Cboe Daily Put/Call Ratios
    live_pc = {}
    try:
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", get_text("https://www.cboe.com/markets/us/options/market-statistics/daily/"))).upper()
        for k, pat in [("total_pc",r"TOTAL PUT/CALL RATIO\s+([0-9]+\.[0-9]+)"),("index_pc",r"INDEX PUT/CALL RATIO\s+([0-9]+\.[0-9]+)"),("eq_pc",r"EQUITY PUT/CALL RATIO\s+([0-9]+\.[0-9]+)")]:
            mt = re.search(pat, txt)
            if mt: live_pc[k] = round(float(mt.group(1)), 2)
    except Exception:
        pass

    # 5. Load existing history (drop stale non-trading-day entries newer than last_trade_date)
    hist = {}
    if os.path.exists(a.history):
        for line in open(a.history):
            if line.strip():
                try:
                    o = json.loads(line); d_k = str(o.get("date", ""))[:10]
                    if d_k and d_k <= last_trade_date and "metrics" in o:
                        hist[d_k] = o
                except Exception:
                    continue

    # 6. Backfill any missing days in the last 30 trading sessions
    for d in recent_dates:
        if d in hist and d != last_trade_date:
            continue
        vix_v = round(lookup_asof(series.get("vix"), d, FALLBACK["vix"]), 1)
        vvix_v = round(lookup_asof(series.get("vvix"), d, FALLBACK["vvix"]), 1)
        skew_v = round(lookup_asof(series.get("skew"), d, FALLBACK["skew"]), 1)
        dspx_v = round(lookup_asof(series.get("dspx"), d, FALLBACK["dspx"]), 1)
        nvda_v = round(lookup_asof(series.get("nvda"), d, FALLBACK["nvda"]), 1)
        vixeq_raw = lookup_asof(series.get("vixeq"), d, vix_v * FALLBACK["vixeq_vix"])
        vix3m_raw = lookup_asof(series.get("vix3m"), d, vix_v / FALLBACK["vix_vxv"])
        vix9d_raw = lookup_asof(series.get("vix9d"), d, vix_v * FALLBACK["vix9d_vix"])
        vixeq_vix = round(vixeq_raw / vix_v, 2) if vix_v > 0 else FALLBACK["vixeq_vix"]
        vix_vxv = round(vix_v / vix3m_raw, 2) if vix3m_raw > 0 else FALLBACK["vix_vxv"]
        vix9d_vix = round(vix9d_raw / vix_v, 2) if vix_v > 0 else FALLBACK["vix9d_vix"]

        gex_v = lookup_asof(sqz_hist, d, FALLBACK["gex"])
        dg_v = max(-12.0, min(10.0, round(gex_v / 13.5, 1)))
        nd_v = calc_net_delta(dg_v, vix_v, vix_vxv, vix9d_vix)

        # Put/Call: use live Cboe for latest day; preserve or smoothly proxy for historical backfill
        if d == last_trade_date and live_pc:
            eq_pc = live_pc.get("eq_pc", FALLBACK["eq_pc"])
            tot_pc = live_pc.get("total_pc", FALLBACK["total_pc"])
            idx_pc = live_pc.get("index_pc", FALLBACK["index_pc"])
        else:
            eq_pc = round(max(0.45, min(1.05, 0.50 + max(0.0, vix_v - 13.0) * 0.015)), 2)
            tot_pc = round(max(0.72, min(1.45, 0.78 + max(0.0, vix_v - 13.0) * 0.020)), 2)
            idx_pc = round(max(0.88, min(1.50, 0.94 + max(0.0, vix_v - 13.0) * 0.019)), 2)

        dm = {
            "vixeq_vix": vixeq_vix, "dspx": dspx_v, "nvda": nvda_v,
            "vix": vix_v, "vvix": vvix_v, "vix_vxv": vix_vxv, "vix9d_vix": vix9d_vix, "skew": skew_v,
            "eq_pc": eq_pc, "total_pc": tot_pc, "index_pc": idx_pc,
            "gex": gex_v, "dealer_gamma": dg_v, "net_delta": nd_v,
        }
        hist[d] = {"date": d, "stress_score": score_fn(dm), "metrics": dm}

    # 7. Finalize latest day with source tags & optional dealer_overrides.json
    m = dict(hist[last_trade_date]["metrics"])
    src = {
        "vixeq_vix": "auto_computed", "dspx": "auto_cboe_yf", "nvda": "auto_cboe_yf",
        "vix": "auto_cboe_yf", "vvix": "auto_cboe_yf", "vix_vxv": "auto_computed",
        "vix9d_vix": "auto_computed", "skew": "auto_cboe_yf",
        "eq_pc": "auto_cboe_daily" if "eq_pc" in live_pc else "fallback_cached",
        "total_pc": "auto_cboe_daily" if "total_pc" in live_pc else "fallback_cached",
        "index_pc": "auto_cboe_daily" if "index_pc" in live_pc else "fallback_cached",
        "gex": "auto_squeezemetrics" if sqz_hist else "fallback_cached",
        "dealer_gamma": "estimated_gex_aligned",
        "net_delta": "estimated_gex_aligned",
    }
    if os.path.exists(a.dealer_overrides):
        ov = json.load(open(a.dealer_overrides))
        for k in ("gex", "dealer_gamma", "net_delta"):
            if isinstance(ov.get(k), (int, float)):
                m[k] = float(ov[k]); src[k] = "manual_institutional_override"

    sc_val = score_fn(m)
    hist[last_trade_date] = {"date": last_trade_date, "stress_score": sc_val, "metrics": m}

    sorted_days = sorted(hist.keys())
    prev_m = None
    for k in reversed(sorted_days):
        if k < last_trade_date:
            prev_m = hist[k].get("metrics"); break

    os.makedirs(os.path.dirname(os.path.abspath(a.history)), exist_ok=True)
    with open(a.history, "w") as f:
        for k in sorted_days[-180:]:
            f.write(json.dumps(hist[k], ensure_ascii=False) + "\n")

    h30 = [
        {
            "date": k,
            "stress_score": hist[k]["stress_score"],
            "vix": hist[k]["metrics"]["vix"],
            "vix_vxv": hist[k]["metrics"]["vix_vxv"],
            "gex": hist[k]["metrics"]["gex"],
        }
        for k in sorted_days[-30:]
    ]

    as_of_display = f"{last_trade_date} 交易日 (抓取于 {est.strftime('%m-%d %H:%M EST')})"
    out_obj = {
        "schema_version": "2.0",
        "date": last_trade_date,
        "as_of": as_of_display,
        "updated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stress_score": sc_val,
        "metrics": m,
        "prev_metrics": prev_m,
        "history_30d": h30,
        "sources": src,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(out_obj, open(a.out, "w"), ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
import yfinance as yf

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CBOE_CSV = {
    "vix": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "vix9d": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
    "vix3m": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
    "vvix": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
    "skew": "https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv",
}
FALLBACK = {"vixeq_vix":1.60,"dspx":37.2,"nvda":146.0,"vix":28.4,"vvix":122.0,"vix_vxv":1.05,"vix9d_vix":1.03,"skew":141.0,"eq_pc":0.72,"total_pc":1.05,"index_pc":1.15,"gex":-100.5,"dealer_gamma":-7.3,"net_delta":-110.0}

def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")

def clamp01(x): return max(0.0, min(1.0, x))

def score_fn(m):
    d1 = 18*(0.45*clamp01((2.5-m["vixeq_vix"])/1.2)+0.20*(clamp01((42-m["dspx"])/15) if m["vix"]>22 else clamp01((m["dspx"]-30)/14))+0.35*clamp01((165-m["nvda"])/40))
    d2 = 24*(0.55*clamp01((m["vix"]-13.5)/22.5)+0.45*clamp01((m["vvix"]-85)/55))
    d3 = 20*(0.45*clamp01((m["vix_vxv"]-0.86)/0.29)+0.35*clamp01((m["vix9d_vix"]-0.88)/0.24)+0.20*(clamp01((158-m["skew"])/25) if m["vix"]>24 else clamp01((m["skew"]-135)/35)))
    d4 = 16*(0.35*clamp01((m["eq_pc"]-0.52)/0.4)+0.30*clamp01((m["total_pc"]-0.78)/0.57)+0.35*clamp01((m["index_pc"]-0.92)/0.48))
    d5 = 22*(0.35*clamp01((30-m["gex"])/150)+0.40*clamp01((3.5-m["dealer_gamma"])/13)+0.25*clamp01((30-m["net_delta"])/170))
    pen = 5.5 if (m["vix"]>28 and m["vix_vxv"]>1 and m["dealer_gamma"]<0) else 0
    return max(0, min(100, round(d1+d2+d3+d4+d5+pen)))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="data/latest_metrics.json")
    p.add_argument("--history", default="data/history_log.jsonl")
    p.add_argument("--dealer-overrides", default="data/dealer_overrides.json")
    a = p.parse_args()
    prev = json.load(open(a.out)) if os.path.exists(a.out) else {}
    m = {**FALLBACK, **(prev.get("metrics") or {})}
    src = {k: "fallback_cached" for k in FALLBACK}
    raw = {}
    for k, sym in [("vix","^VIX"),("vix9d","^VIX9D"),("vix3m","^VIX3M"),("vvix","^VVIX"),("skew","^SKEW"),("dspx","^DSPX"),("vixeq","^VIXEQ"),("nvda","NVDA"),("spx","^GSPC")]:
        try:
            h = yf.Ticker(sym).history(period="5d")
            if not h.empty: raw[k] = float(h["Close"].dropna().iloc[-1])
        except Exception: pass
    for k, u in CBOE_CSV.items():
        if k not in raw:
            try:
                rows = [r for r in csv.reader(io.StringIO(get_text(u))) if len(r)>=2]
                for r in reversed(rows):
                    try:
                        v = float(r[-1].strip())
                        if v > 0: raw[k] = v; break
                    except Exception: continue
            except Exception: pass
    for k in ("vix","vvix","skew","dspx","nvda"):
        if k in raw: m[k] = round(raw[k], 1); src[k] = "auto_cboe_yf"
    if "vixeq" in raw and m["vix"]>0: m["vixeq_vix"] = round(raw["vixeq"]/m["vix"], 2); src["vixeq_vix"] = "auto_computed"
    if "vix3m" in raw and raw["vix3m"]>0: m["vix_vxv"] = round(m["vix"]/raw["vix3m"], 2); src["vix_vxv"] = "auto_computed"
    if "vix9d" in raw and m["vix"]>0: m["vix9d_vix"] = round(raw["vix9d"]/m["vix"], 2); src["vix9d_vix"] = "auto_computed"
    try:
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", get_text("https://www.cboe.com/markets/us/options/market-statistics/daily/"))).upper()
        for k, pat in [("total_pc",r"TOTAL PUT/CALL RATIO\s+([0-9]+\.[0-9]+)"),("index_pc",r"INDEX PUT/CALL RATIO\s+([0-9]+\.[0-9]+)"),("eq_pc",r"EQUITY PUT/CALL RATIO\s+([0-9]+\.[0-9]+)")]:
            mt = re.search(pat, txt)
            if mt: m[k] = round(float(mt.group(1)), 2); src[k] = "auto_cboe_daily"
    except Exception: pass

    chain_dg = None
    try:
        tk = yf.Ticker("^SPX"); exps = list(tk.options or [])[:4]; sc = 1.0; spot = raw.get("spx", 5850.0)
        if not exps: tk = yf.Ticker("SPY"); exps = list(tk.options or [])[:4]; sc = 10.0
        net_g = 0.0; today = dt.date.today()
        for e in exps:
            t = max(1, (dt.date.fromisoformat(e)-today).days)/365.0; ch = tk.option_chain(e)
            for is_c, df in ((True, ch.calls), (False, ch.puts)):
                if df is None or df.empty: continue
                for _, row in df.iterrows():
                    K = float(row.get("strike",0))*sc; oi = float(row.get("openInterest",0) or 0); iv = float(row.get("impliedVolatility",0) or 0)
                    if oi<=0 or K<=0 or iv<=0.01: continue
                    d1 = (math.log(spot/K)+(0.029+0.5*iv*iv)*t)/(iv*math.sqrt(t))
                    g = math.exp(-0.013*t)*(math.exp(-0.5*d1*d1)/math.sqrt(2*math.pi))/(spot*iv*math.sqrt(t))
                    dg = oi*100*g*(spot**2)*0.01
                    net_g += dg if is_c else -dg
        if net_g != 0:
            chain_dg = max(-12.0, min(10.0, round(net_g/1e9, 1)))
    except Exception: pass

    sqz_gex = None
    try:
        rows = list(csv.DictReader(io.StringIO(get_text("https://squeezemetrics.com/monitor/static/DIX.csv"))))
        if rows:
            last = rows[-1]; g_raw = float(last["gex"]); px = float(last.get("price") or 5850)
            gb = (g_raw*px*0.01)/1e9
            if abs(gb) > 300: gb = g_raw/1e8
            sqz_gex = round(max(-130.0, min(130.0, gb)), 1)
    except Exception: pass

    if sqz_gex is not None:
        m["gex"] = sqz_gex; src["gex"] = "auto_squeezemetrics"
        implied_dg = max(-12.0, min(10.0, round(sqz_gex / 13.5, 1)))
        if chain_dg is not None and (chain_dg * sqz_gex > 0):
            m["dealer_gamma"] = max(-12.0, min(10.0, round(0.65 * implied_dg + 0.35 * chain_dg, 1)))
        else:
            m["dealer_gamma"] = implied_dg
        src["dealer_gamma"] = "estimated_gex_aligned"
    elif chain_dg is not None:
        m["dealer_gamma"] = chain_dg
        m["gex"] = max(-130.0, min(130.0, round(chain_dg * 13.5, 1)))
        src["dealer_gamma"] = "estimated_bsm_chain"; src["gex"] = "estimated_bsm_chain"

    term_peak = max(m["vix_vxv"], m["vix9d_vix"])
    vp = max(0.0, m["vix"] - 20.0) * 3.8 + max(0.0, term_peak - 0.95) * 180.0
    raw_nd = m["dealer_gamma"] * 11.5 - (vp if m["dealer_gamma"] < 0 else -vp * 0.25)
    m["net_delta"] = max(-150.0, min(150.0, round(raw_nd)))
    src["net_delta"] = "estimated_gex_aligned"

    if os.path.exists(a.dealer_overrides):
        ov = json.load(open(a.dealer_overrides))
        for k in ("gex","dealer_gamma","net_delta"):
            if isinstance(ov.get(k), (int, float)): m[k] = float(ov[k]); src[k] = "manual_institutional_override"

    now = dt.datetime.now(dt.timezone.utc); est = now.astimezone(dt.timezone(dt.timedelta(hours=-5)))
    d_str = est.strftime("%Y-%m-%d"); sc_val = score_fn(m)
    hist = {}
    if os.path.exists(a.history):
        for line in open(a.history):
            if line.strip():
                o = json.loads(line); hist[str(o.get("date",""))[:10]] = o
    prev_m = None
    for k in sorted(hist.keys(), reverse=True):
        if k < d_str: prev_m = hist[k].get("metrics"); break
    hist[d_str] = {"date": d_str, "stress_score": sc_val, "metrics": m}
    os.makedirs(os.path.dirname(os.path.abspath(a.history)), exist_ok=True)
    with open(a.history, "w") as f:
        for k in sorted(hist.keys())[-180:]: f.write(json.dumps(hist[k], ensure_ascii=False)+"\n")
    h30 = [{"date": k, "stress_score": hist[k]["stress_score"], "vix": hist[k]["metrics"]["vix"], "vix_vxv": hist[k]["metrics"]["vix_vxv"], "gex": hist[k]["metrics"]["gex"]} for k in sorted(hist.keys())[-30:]]
    out_obj = {"schema_version":"2.0","date":d_str,"as_of":est.strftime("%Y-%m-%d %H:%M EST"),"updated_at_utc":now.strftime("%Y-%m-%dT%H:%M:%SZ"),"stress_score":sc_val,"metrics":m,"prev_metrics":prev_m,"history_30d":h30,"sources":src}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(out_obj, open(a.out, "w"), ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
