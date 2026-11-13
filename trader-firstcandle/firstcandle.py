#!/usr/bin/env python3
"""First Candle bot for trader-firstcandle: the "first candle rule", one setup
a day from the first 5-minute candle of the New York session. Standard
library only. It never places an order.

    python3 firstcandle.py check SPY                       # today's setup, as of now
    python3 firstcandle.py check SPY --write               # also trader/entry.json + trader/plan.json
    python3 firstcandle.py check SPY --now "2026-11-12 10:20" --offline SPY.json
    python3 firstcandle.py watch SPY QQQ --until 11:30     # minute by minute until a setup fires
    python3 firstcandle.py backtest SPY QQQ TSLA           # every day Yahoo still has 1-minute bars
    python3 firstcandle.py backtest TSLA --interval 5m     # 60 days, on 5-minute bars (rougher)

The rule, step by step:
  1. The first candle. The high and low of 9:30 to 9:35 New York time (the
     first 5-minute candle, or the five 1-minute candles inside it).
  2. The break. On 1-minute candles, the first candle that CLOSES outside the
     range (above the high for a long, below the low for a short) is the
     break candle. A wick outside is not a break.
  3. The fair value gap (FVG). Before price comes back, the breakout must leave
     a 3-candle gap in the break's direction: candle 3's low above candle 1's
     high (long), or candle 3's high below candle 1's low (short), with the
     middle candle closing outside the range. A break that closes back inside
     before any FVG forms is dropped, and the next close outside starts over.
  4. Back inside. After the FVG, price has to come back into the range:
     a wick back to the level by default (--reentry wick), or a close back
     inside with --reentry close.
  5. The double break. A LATER candle closes outside the range again, on the
     FVG's side. That close is the entry.
  6. Stop: the far end of the break candle (its low for a long, its high for
     a short). Target: --rr times the risk, 4 by default (1:4).
  A close on the other side of the range cancels the setup on this side. One
  trade a day: the first side to complete the rule. No new entry after
  --last-entry (config no_entries_after, 15:30); open trades are flat by
  --flat-by (config flat_by, 15:55).

Backtest exits: stop and target are checked on every 1-minute candle after the
entry. A candle that touches both counts as the STOP (the honest assumption:
inside a candle the order is unknown). A candle that opens past the stop fills
at its open. Otherwise the trade is closed at the last close before flat-by.

Data: Yahoo's free chart JSON (unofficial, can lag).
  https://query2.finance.yahoo.com/v8/finance/chart/SPY?range=1d&interval=1m
  Yahoo keeps 1-minute bars for about 30 days (fetched a week at a time) and
  5-minute bars for about 60. --offline PATH reads a saved chart JSON
  (.json or .json.gz), or PATH/<TICKER>.json(.gz) when PATH is a folder.

Exit codes: check 0 when the entry fired today, 1 otherwise. watch 0 on an
entry, 1 otherwise. backtest 0.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
HOSTS = ("query2", "query1")
PAUSE = 0.5
BACKOFF = 5.0
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_after": "15:30", "flat_by": "15:55"}
EPOCH = datetime.datetime(1970, 1, 1)
OPEN = datetime.time(9, 30)
FIRST_END = datetime.time(9, 35)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)
NOTE = ("A rules check, not financial advice. Nothing was placed. Size it with /trader-budget, check it with "
        "/trader-risk, and only /trader-broker sends it, after your yes.")

NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}


class FCError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """-4 in US daylight time, -5 otherwise (second Sunday of March to first Sunday of November)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def parse_now(text):
    """--now: "2026-11-12 10:20" (New York), "10:20" (today, New York), or ISO with Z/offset."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" ET"):
        t = t[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise FCError('--now wants a New York time like "2026-11-12 10:20"')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise FCError("%s wants a New York time like 15:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    return "%d:%02d %s" % (t.hour % 12 or 12, t.minute, "AM" if t.hour < 12 else "PM")


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def close_of(d):
    return EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE


def r2(x):
    return None if x is None else round(float(x) + 0.0, 2)


# ---------------------------------------------------------------- data

def chart_path(sym, interval, rng=None, p1=None, p2=None):
    s = urllib.parse.quote(sym.upper().replace(".", "-"))
    if rng:
        return "/v8/finance/chart/%s?range=%s&interval=%s" % (s, rng, interval)
    return "/v8/finance/chart/%s?period1=%d&period2=%d&interval=%s" % (s, p1, p2, interval)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429/5xx; one retry after a 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code = e.code
                body = None
                try:
                    if code == 404:
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404 and body:
                    return body
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise FCError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = "network error: %s" % (getattr(e, "reason", None) or e,)
                break
    raise FCError("Yahoo did not answer (%s): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, bars [{t,o,h,l,c,v}], data_time}. Null closes are dropped."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise FCError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise FCError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "bars": bars,
            "data_time": meta.get("regularMarketTime"), "granularity": meta.get("dataGranularity")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym.upper() + ".json", sym.upper() + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise FCError("no %s.json in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise FCError("no such file: %s" % path)
    return path


def merge_charts(charts):
    by_t = {}
    for ch in charts:
        for b in ch["bars"]:
            by_t[b["t"]] = b
    out = dict(charts[-1])
    out["bars"] = [by_t[t] for t in sorted(by_t)]
    out["data_time"] = max([c.get("data_time") or 0 for c in charts]) or None
    return out


def get_chart(sym, interval="1m", offline=None, history=False, now_utc=None):
    """Today's bars (history=False) or as many days as Yahoo keeps (history=True)."""
    if offline:
        return parse_chart(read_text(offline_file(offline, sym)), sym)
    if not history:
        return parse_chart(yahoo_get(chart_path(sym, interval, rng="1d")), sym)
    if interval == "5m":
        return parse_chart(yahoo_get(chart_path(sym, "5m", rng="60d")), sym)
    # 1-minute: Yahoo serves at most ~7 days per request and ~30 days back
    end = ts_of(now_utc or utc_now())
    charts = []
    for k in range(4):
        p2 = end - k * 7 * 86400
        p1 = p2 - 7 * 86400
        try:
            charts.append(parse_chart(yahoo_get(chart_path(sym, "1m", p1=p1, p2=p2)), sym))
        except FCError:
            if not charts:
                raise
            break
        _sleep(PAUSE)
    return merge_charts(charts[::-1])


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError:
            raise FCError("%s is not valid JSON" % path)
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    return cfg


# ---------------------------------------------------------------- sessions

def sessions(chart, interval="1m"):
    """{date: [bar, ...]} of regular-session bars on the interval grid, with et and end (New York)."""
    step = INTERVALS[interval]
    days = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick is not a bar
        et = to_eastern(utc_of(b["t"]))
        d = et.date()
        if not trading_day(d):
            continue
        if not (OPEN <= et.time() < close_of(d)):
            continue
        days.setdefault(d, []).append(dict(b, et=et, end=et + datetime.timedelta(seconds=step)))
    for d in days:
        days[d].sort(key=lambda b: b["t"])
    return days


def closed_bars(bars, now_utc, data_time=None):
    """Bars whose minute is over at now_utc. On live data the newest one also waits until Yahoo's
    last trade reaches its close (60 seconds at most), so a bar is never read half-filled."""
    out = [b for b in bars if eastern_to_utc(b["end"]) <= now_utc]
    if out and data_time and out[-1] is bars[-1]:
        end_ts = ts_of(eastern_to_utc(out[-1]["end"]))
        if data_time < end_ts and ts_of(now_utc) < end_ts + 60:
            out = out[:-1]
    return out


# ---------------------------------------------------------------- the rule

def first_candle(bars):
    """The 9:30-9:35 candle from the bars inside it, or None if they are not all there."""
    first = [b for b in bars if b["et"].time() < FIRST_END]
    if not first:
        return None
    if first[-1]["end"].time() < FIRST_END:
        return None                         # still forming
    return {"high": max(b["h"] for b in first), "low": min(b["l"] for b in first),
            "open": first[0]["o"], "close": first[-1]["c"], "bars": len(first)}


def _out(b, side, hi, lo):
    return b["c"] > hi if side == "long" else b["c"] < lo


def _back_in(b, side, hi, lo, reentry):
    if side == "long":
        return (b["l"] if reentry == "wick" else b["c"]) <= hi
    return (b["h"] if reentry == "wick" else b["c"]) >= lo


def _fvg(a, b, c, side, hi, lo):
    if side == "long":
        return c["l"] > a["h"] and b["c"] > hi and b["c"] > b["o"]
    return c["h"] < a["l"] and b["c"] < lo and b["c"] < b["o"]


def _bar(b):
    return {"time": hm(b["et"]), "o": r2(b["o"]), "h": r2(b["h"]), "l": r2(b["l"]), "c": r2(b["c"])}


def evaluate_day(bars, rr=4.0, reentry="wick", last_entry=None, sides=("long", "short")):
    """Pure function: one session's CLOSED bars (oldest first) -> the day's setup.

    Returns {status, first_candle, side, entry, stop, target, ..., steps, sides}.
    status: forming (first candle not done), waiting, entry, expired, no-data."""
    if reentry not in ("wick", "close"):
        raise FCError("--reentry is wick or close")
    if rr <= 0:
        raise FCError("--rr must be above 0")
    res = {"status": "waiting", "first_candle": None, "entry": None, "sides": {}}
    if not bars:
        res["status"] = "no-data"
        return res
    fc = first_candle(bars)
    if fc is None:
        res["status"] = "forming"
        return res
    res["first_candle"] = {"high": r2(fc["high"]), "low": r2(fc["low"]), "bars": fc["bars"]}
    hi, lo = fc["high"], fc["low"]
    st = {s: {"stage": "idle", "break": None, "k": None, "fvg": None, "back_in": None, "log": []} for s in sides}
    start = next(i for i, b in enumerate(bars) if b["et"].time() >= FIRST_END) if any(
        b["et"].time() >= FIRST_END for b in bars) else len(bars)
    for i in range(start, len(bars)):
        b = bars[i]
        for side in sides:
            s = st[side]
            other_out = _out(b, "short" if side == "long" else "long", hi, lo)
            if other_out and s["stage"] != "idle":
                s["log"].append("%s closed on the other side of the range: %s setup cancelled" % (hm(b["et"]), side))
                s.update(stage="idle", fvg=None, back_in=None, k=None)
                s["break"] = None
                continue
            if s["stage"] == "idle":
                if _out(b, side, hi, lo):
                    s["stage"], s["break"], s["k"] = "broken", b, i
                    s["log"].append("%s break: closed %s the range at %.2f" % (
                        hm(b["et"]), "above" if side == "long" else "below", b["c"]))
                continue
            if s["stage"] == "broken":
                if not _out(b, side, hi, lo):
                    s["log"].append("%s closed back inside before any fair value gap: break dropped" % hm(b["et"]))
                    s.update(stage="idle", fvg=None)
                    s["break"] = None
                    continue
                k = s["k"]
                if i - 2 >= 0 and i - 1 >= k and _fvg(bars[i - 2], bars[i - 1], b, side, hi, lo):
                    a = bars[i - 2]
                    gap = (a["h"], b["l"]) if side == "long" else (b["h"], a["l"])
                    s["stage"] = "fvg"
                    s["fvg"] = {"candles": [hm(a["et"]), hm(bars[i - 1]["et"]), hm(b["et"])],
                                "gap_low": r2(gap[0]), "gap_high": r2(gap[1])}
                    s["log"].append("%s fair value gap %.2f-%.2f (candles %s, %s, %s)" % (
                        hm(b["et"]), gap[0], gap[1], hm(a["et"]), hm(bars[i - 1]["et"]), hm(b["et"])))
                continue
            if s["stage"] == "fvg":
                if _back_in(b, side, hi, lo, reentry):
                    s["stage"], s["back_in"] = "back-in", b
                    s["log"].append("%s back inside the range (%s %.2f)" % (
                        hm(b["et"]), "low" if side == "long" and reentry == "wick" else
                        "high" if reentry == "wick" else "close",
                        b["l"] if side == "long" and reentry == "wick" else b["h"] if reentry == "wick" else b["c"]))
                continue
            if s["stage"] == "back-in" and _out(b, side, hi, lo):
                if last_entry and b["end"].time() > last_entry:
                    s["log"].append("%s double break, but after the last entry time %s" % (hm(b["et"]), hm(last_entry)))
                    s["stage"] = "late"
                    continue
                brk = s["break"]
                entry = b["c"]
                stop = brk["l"] if side == "long" else brk["h"]
                risk = (entry - stop) if side == "long" else (stop - entry)
                if risk <= 0:
                    s["log"].append("%s double break at %.2f, but the break candle's %s %.2f is not behind it: skipped"
                                    % (hm(b["et"]), entry, "low" if side == "long" else "high", stop))
                    s.update(stage="idle", fvg=None, back_in=None)
                    s["break"] = None
                    continue
                target = entry + rr * risk if side == "long" else entry - rr * risk
                s["log"].append("%s double break: closed %s the range again at %.2f -> ENTRY" % (
                    hm(b["et"]), "above" if side == "long" else "below", entry))
                res["status"] = "entry"
                res["entry"] = {
                    "side": side, "price": r2(entry), "raw": entry, "bar": _bar(b), "time": ampm(b["end"]),
                    "close_time": hm(b["end"]), "index": i, "stop": r2(stop), "stop_raw": stop,
                    "target": r2(target), "target_raw": target, "risk_per_share": r2(risk), "rr": rr,
                    "break_candle": _bar(brk), "fvg": s["fvg"], "back_in": _bar(s["back_in"]),
                }
                break
        if res["status"] == "entry":
            break
    for side in sides:
        s = st[side]
        res["sides"][side] = {"stage": s["stage"], "break": _bar(s["break"]) if s["break"] else None,
                              "fvg": s["fvg"], "log": s["log"]}
    last = bars[-1]
    res["last"] = {"time": hm(last["et"]), "close": r2(last["c"])}
    if res["status"] != "entry" and last_entry and last["end"].time() >= last_entry:
        res["status"] = "expired"
    return res


def simulate_exit(bars, entry, flat_by=None):
    """Walk the bars after the entry: stop, target or flat. Both in one bar = stop."""
    side, e, stop, target, risk = entry["side"], entry["raw"], entry["stop_raw"], entry["target_raw"], \
        entry["risk_per_share"]
    risk = abs(e - stop)
    long = side == "long"
    last = None
    for b in bars[entry["index"] + 1:]:
        if flat_by and b["end"].time() > flat_by:
            break
        last = b
        hit_stop = b["l"] <= stop if long else b["h"] >= stop
        hit_target = b["h"] >= target if long else b["l"] <= target
        if hit_stop:
            gap = (b["o"] < stop) if long else (b["o"] > stop)
            px = b["o"] if gap else stop
            why = "stop" + (" (and target in the same candle: counted as the stop)" if hit_target else "")
            return _result(e, px, risk, long, "stop", hm(b["et"]), why)
        if hit_target:
            gap = (b["o"] > target) if long else (b["o"] < target)
            return _result(e, b["o"] if gap else target, risk, long, "target", hm(b["et"]), "target")
    if last is None:
        return _result(e, e, risk, long, "flat", entry["close_time"], "no bars after the entry")
    return _result(e, last["c"], risk, long, "flat", hm(last["end"]), "flat at the close of the %s candle" % hm(last["et"]))


def _result(e, px, risk, long, outcome, at, why):
    pnl = (px - e) if long else (e - px)
    return {"outcome": outcome, "exit": r2(px), "time": at, "pnl_per_share": r2(pnl),
            "r": round(pnl / risk, 2) if risk else 0.0, "why": why}


# ---------------------------------------------------------------- backtest

def backtest(chart, interval="1m", rr=4.0, reentry="wick", last_entry=None, flat_by=None,
             sides=("long", "short"), skip_today=None):
    days = sessions(chart, interval)
    rows = []
    for d in sorted(days):
        if skip_today and d == skip_today:
            continue
        bars = days[d]
        if not bars or bars[-1]["end"].time() < min(close_of(d), flat_by or CLOSE):
            continue                               # an unfinished session
        res = evaluate_day(bars, rr, reentry, last_entry, sides)
        row = {"date": d.isoformat(), "status": res["status"], "first_candle": res["first_candle"]}
        if res["status"] == "entry":
            e = res["entry"]
            ex = simulate_exit(bars, e, flat_by=min(flat_by or close_of(d), close_of(d)))
            row.update(side=e["side"], entry=e["price"], entry_time=e["close_time"], stop=e["stop"],
                       target=e["target"], risk=e["risk_per_share"], exit=ex)
        rows.append(row)
    trades = [r for r in rows if "exit" in r]
    wins = [r for r in trades if r["exit"]["r"] > 0]
    total_r = round(sum(r["exit"]["r"] for r in trades), 2)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in trades:
        eq += r["exit"]["r"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    summary = {
        "days": len(rows), "trades": len(trades),
        "targets": sum(1 for r in trades if r["exit"]["outcome"] == "target"),
        "stops": sum(1 for r in trades if r["exit"]["outcome"] == "stop"),
        "flat": sum(1 for r in trades if r["exit"]["outcome"] == "flat"),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 1) if trades else None,
        "total_r": total_r, "avg_r": round(total_r / len(trades), 2) if trades else None,
        "max_drawdown_r": round(dd, 2),
    }
    return {"symbol": chart["symbol"], "interval": interval, "rr": rr, "reentry": reentry,
            "rows": rows, "summary": summary}


# ---------------------------------------------------------------- output docs

def plan_doc(sym, day, res, flat_by, now_utc, interval="1m"):
    """trader/plan.json in trader-exit's shape, so /trader-budget, /trader-risk and /trader-timemachine read it."""
    e = res["entry"]
    fc = res["first_candle"]
    return {"created_by": "trader-firstcandle", "symbol": sym, "side": "buy" if e["side"] == "long" else "sell",
            "date": day.isoformat(), "setup": "first-candle", "entry": e["price"], "entry_time": e["time"],
            "entry_bar": e["index"], "interval": interval,
            "opening_range": {"high": fc["high"], "low": fc["low"], "minutes": 5},
            "stop": e["stop"], "stop_rule": "break-candle",
            "stop_label": "the break candle's %s (%s)" % ("low" if e["side"] == "long" else "high",
                                                          e["break_candle"]["time"]),
            "target": e["target"], "target_r": e["rr"], "target_label": "%gR (1:%g)" % (e["rr"], e["rr"]),
            "risk_per_share": e["risk_per_share"], "reward_risk": e["rr"], "flat_by": flat_by,
            "stop_detail": "stop at the far end of the candle that first broke the 9:30 first candle's range",
            "fvg": e["fvg"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}


def entry_doc(sym, day, res, now_utc, interval="1m"):
    e = res["entry"]
    return {"created_by": "trader-firstcandle", "symbol": sym, "setup": "first-candle",
            "side": "buy" if e["side"] == "long" else "sell", "date": day.isoformat(), "status": "triggered",
            "interval": interval, "time": e["time"], "close_time": e["close_time"], "price": e["price"],
            "level": res["first_candle"]["high"] if e["side"] == "long" else res["first_candle"]["low"],
            "opening_range": {"high": res["first_candle"]["high"], "low": res["first_candle"]["low"], "minutes": 5},
            "stop": e["stop"], "target": e["target"], "written_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- words

def describe(sym, day, res, now_local, rr):
    out = ["%s  %s  first candle rule on 1-minute candles (as of %s ET)" % (
        sym, day.strftime("%a %Y-%m-%d"), now_local.strftime("%H:%M"))]
    st = res["status"]
    if st == "no-data":
        out.append("NO DATA  Yahoo sent no bars for this session yet.")
        return "\n".join(out)
    if st == "forming":
        out.append("FORMING  the first candle (9:30-9:35) is not finished yet.")
        return "\n".join(out)
    fc = res["first_candle"]
    out.append("First candle 9:30-9:35: high $%.2f, low $%.2f." % (fc["high"], fc["low"]))
    for side in ("long", "short"):
        s = res["sides"].get(side)
        if not s:
            continue
        for line in s["log"]:
            out.append("  [%s] %s" % (side, line))
    if st == "entry":
        e = res["entry"]
        out.append("ENTRY %s at %s, $%.2f. Stop $%.2f (break candle %s), target $%.2f (1:%g), risk $%.2f a share." % (
            "LONG" if e["side"] == "long" else "SHORT", e["time"], e["price"], e["stop"],
            e["break_candle"]["time"], e["target"], rr, e["risk_per_share"]))
        if e["side"] == "short":
            out.append("This pack sizes and sends longs only (allow_short is false): a short is information here.")
        out.append("Nothing was placed. Next: /trader-budget, /trader-risk, then /trader-broker after your yes.")
    elif st == "expired":
        out.append("EXPIRED  no complete setup by the last entry time. No trade today.")
    else:
        stages = ", ".join("%s: %s" % (k, v["stage"]) for k, v in sorted(res["sides"].items()))
        out.append("WAITING  last close $%.2f (%s candle). Stage: %s." % (
            res["last"]["close"], res["last"]["time"], stages))
    return "\n".join(out)


def describe_backtest(bt):
    s = bt["summary"]
    lines = ["%s  first candle rule backtest on %s candles, 1:%g, re-entry by %s" % (
        bt["symbol"], bt["interval"], bt["rr"], bt["reentry"])]
    lines.append("%-10s %-5s %-6s %9s %9s %9s %-7s %9s %6s" % (
        "date", "side", "time", "entry", "stop", "target", "result", "exit", "R"))
    for r in bt["rows"]:
        if "exit" not in r:
            lines.append("%-10s %s" % (r["date"], "no trade (%s)" % r["status"]))
            continue
        x = r["exit"]
        lines.append("%-10s %-5s %-6s %9.2f %9.2f %9.2f %-7s %9.2f %+6.2f" % (
            r["date"], r["side"], r["entry_time"], r["entry"], r["stop"], r["target"], x["outcome"],
            x["exit"], x["r"]))
    lines.append("")
    lines.append("%d sessions, %d trades: %d targets, %d stops, %d flat at the close." % (
        s["days"], s["trades"], s["targets"], s["stops"], s["flat"]))
    if s["trades"]:
        lines.append("Win rate %.1f%%, total %+.2fR, average %+.2fR a trade, worst drawdown %.2fR." % (
            s["win_rate_pct"], s["total_r"], s["avg_r"], s["max_drawdown_r"]))
    lines.append("Past bars, free Yahoo data, no slippage or commissions. Not a promise; not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def _sides(a):
    return {"both": ("long", "short"), "long": ("long",), "short": ("short",)}[a.side]


def run_check(sym, a, cfg, now_utc, chart=None, fixed_clock=False):
    chart = chart or get_chart(sym, "1m", a.offline)
    if a.offline and not a.now and not fixed_clock and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_eastern(now_utc)
    day = local.date()
    days = sessions(chart, "1m")
    bars = closed_bars(days.get(day, []), now_utc, None if a.offline else chart.get("data_time"))
    last_entry = parse_hhmm(a.last_entry or cfg["no_entries_after"], "--last-entry")
    res = evaluate_day(bars, a.rr, a.reentry, last_entry, _sides(a))
    if not trading_day(day):
        res["status"] = "no-data"
    return chart["symbol"], day, local, res


def cmd_check(a):
    cfg = read_config(a.config)
    now = parse_now(a.now)
    code = 1
    for sym in a.symbols:
        name, day, local, res = run_check(sym.upper(), a, cfg, now)
        if a.json:
            print(json.dumps({"symbol": name, "date": day.isoformat(), "result": _public(res)}, indent=2))
        else:
            print(describe(name, day, res, local, a.rr))
        if res["status"] == "entry":
            code = 0
            if a.write:
                _write(name, day, res, cfg, now, a.write)
    return code


def _public(res):
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if not k.endswith("_raw") and k != "raw"}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x
    return clean(res)


def _write(name, day, res, cfg, now, folder):
    if res["entry"]["side"] != "long":
        print("Not written: this pack plans longs only (a short needs allow_short and a broker that allows it).")
        return
    write_json(os.path.join(folder, "entry.json"), entry_doc(name, day, res, now))
    write_json(os.path.join(folder, "plan.json"), plan_doc(name, day, res, cfg["flat_by"], now))
    print("Wrote %s and %s." % (os.path.join(folder, "entry.json"), os.path.join(folder, "plan.json")))


def cmd_watch(a):
    """Re-check every symbol just after each minute closes until one enters, all expire, or --until.
    With --offline or --now it replays on a simulated clock, a minute at a time, without sleeping."""
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg["no_entries_after"], "--until")
    pending = [s.upper() for s in a.symbols]
    sim = bool(a.offline or a.now)
    charts = {}
    if sim:
        for sym in pending:
            charts[sym] = get_chart(sym, "1m", a.offline)
        if a.now:
            now = parse_now(a.now)
        else:
            last = max(c["data_time"] or c["bars"][-1]["t"] for c in charts.values())
            day = to_eastern(utc_of(last)).date()
            now = eastern_to_utc(datetime.datetime.combine(day, FIRST_END))
    a.now = None  # run_check reads the clock passed in
    seen = {}
    while pending:
        if not sim:
            now = utc_now()
        local = to_eastern(now)
        for sym in list(pending):
            try:
                name, day, _, res = run_check(sym, a, cfg, now, charts.get(sym), sim)
            except FCError as e:
                print("%s %s: %s" % (local.strftime("%H:%M:%S"), sym, e))
                continue
            key = (res["status"], tuple(sorted((k, v["stage"]) for k, v in res["sides"].items())))
            if seen.get(sym) != key:
                seen[sym] = key
                print("%s %s %s %s" % (local.strftime("%H:%M"), name, res["status"].upper(),
                                        ", ".join("%s=%s" % k for k in key[1])))
            if res["status"] == "entry":
                print(describe(name, day, res, local, a.rr))
                if a.write:
                    _write(name, day, res, cfg, now, a.write)
                return 0
            if res["status"] in ("expired", "no-data") and (sim or res["status"] == "expired"):
                pending.remove(sym)
        if local.time() >= until or not pending:
            print("No first-candle entry by %s." % hm(until))
            return 1
        if sim:
            now += datetime.timedelta(minutes=1)
        else:
            _sleep(60 - local.second + 3)
    return 1


def cmd_backtest(a):
    cfg = read_config(a.config)
    last_entry = parse_hhmm(a.last_entry or cfg["no_entries_after"], "--last-entry")
    flat_by = parse_hhmm(a.flat_by or cfg["flat_by"], "--flat-by")
    now = parse_now(a.now) if a.now else utc_now()
    out, totals = [], []
    for i, sym in enumerate(a.symbols):
        if i and not a.offline:
            _sleep(PAUSE)
        chart = get_chart(sym.upper(), a.interval, a.offline, history=True, now_utc=now)
        bt = backtest(chart, a.interval, a.rr, a.reentry, last_entry, flat_by, _sides(a),
                      skip_today=None if a.offline else to_eastern(now).date())
        out.append(bt)
        if not a.json:
            print(describe_backtest(bt))
            print("")
        totals.extend(r["exit"]["r"] for r in bt["rows"] if "exit" in r)
    if a.json:
        print(json.dumps(out, indent=2))
    elif len(out) > 1 and totals:
        print("All symbols: %d trades, total %+.2fR, average %+.2fR." % (
            len(totals), sum(totals), sum(totals) / len(totals)))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="First Candle bot: the first-candle rule (FVG + double break, 1:4).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("symbols", nargs="+", metavar="TICKER")
        p.add_argument("--rr", type=float, default=4.0, help="reward to risk for the target (default 4)")
        p.add_argument("--reentry", choices=("wick", "close"), default="wick",
                       help="how price must come back into the range after the FVG (default wick)")
        p.add_argument("--side", choices=("both", "long", "short"), default="both")
        p.add_argument("--last-entry", help="no new entry after this New York time (default config, 15:30)")
        p.add_argument("--offline", metavar="PATH", help="a saved Yahoo chart JSON, or a folder of them")
        p.add_argument("--now", help='replay as of a New York time, e.g. "2026-11-12 10:20"')
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="today's setup, as of now")
    common(p)
    p.add_argument("--write", nargs="?", const="trader", metavar="FOLDER",
                   help="on a long entry, write FOLDER/entry.json and FOLDER/plan.json (default trader/)")
    p = sub.add_parser("watch", help="check every minute until an entry")
    common(p)
    p.add_argument("--until", help="stop at this New York time (default config no_entries_after)")
    p.add_argument("--write", nargs="?", const="trader", metavar="FOLDER")
    p = sub.add_parser("backtest", help="replay every day Yahoo still has")
    common(p)
    p.add_argument("--interval", choices=("1m", "5m"), default="1m",
                   help="1m (about 30 days) or 5m (about 60 days, rougher)")
    p.add_argument("--flat-by", help="close open trades by this New York time (default config, 15:55)")
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        if a.cmd == "check":
            return cmd_check(a)
        if a.cmd == "watch":
            return cmd_watch(a)
        return cmd_backtest(a)
    except FCError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
