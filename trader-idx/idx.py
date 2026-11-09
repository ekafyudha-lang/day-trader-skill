#!/usr/bin/env python3
"""IDX toolkit for trader-idx: saham Indonesia (Bursa Efek Indonesia). Standard library only.

    python3 idx.py rules 122 --prev 118                 # fraksi harga, ARA / ARB, harga lot
    python3 idx.py size --modal 10000000 --risk-pct 1 --entry 122 --stop 114
    python3 idx.py backtest DOOH                        # 4 strategi harian, 2 tahun data Yahoo DOOH.JK
    python3 idx.py backtest DOOH --strategy breakout --target-r 2 --hold 10
    python3 idx.py backtest DOOH --csv data/DOOH.csv    # data sendiri: Date,Open,High,Low,Close,Volume
    python3 idx.py orb DOOH                             # opening range breakout intraday, 5 menit, 60 hari
    python3 idx.py cross BBRI --fast 5 --slow 20 --from 2026-01-01   # golden cross, keluar di death cross
    python3 idx.py scan DOOH BBCA TLKM GOTO             # semua strategi, semua saham, urut expectancy

The IDX rules it applies (defaults, check them against idx.co.id, they change):
  Fraksi harga (tick): < 200: 1, 200 to < 500: 2, 500 to < 2,000: 5,
      2,000 to < 5,000: 10, >= 5,000: 25 rupiah.
  1 lot = 100 lembar. Harga minimum 50 (papan utama / pengembangan).
  ARA (auto rejection atas) from the previous close: 35 % for 50 to 200,
      25 % for > 200 to 5,000, 20 % for > 5,000. ARB (bawah): 15 % flat by
      default (--arb to change, --arb-tiered to mirror the ARA tiers).
      Papan akselerasi and full call auction stocks follow other rules,
      not modelled here.
  Fees: beli 0.15 %, jual 0.25 % (the sell side includes the 0.1 % final
      sales tax). Override with --fee-buy / --fee-sell, in percent.
  Sessions (WIB, UTC+7): Senin to Kamis 09:00 to 12:00 and 13:30 to 15:50,
      Jumat 09:00 to 11:30 and 14:00 to 15:50. Pre-closing after 15:50.

Backtest (daily bars), one position per strategy at a time:
  A signal is read on the CLOSE of day t. The entry is the OPEN of day t+1,
  never the close of the signal day (no look-ahead). It is skipped when that
  day is locked at ARA (open = high = low, nothing to buy) or has no volume,
  and when the 20-day average traded value (close x volume) is under
  --min-value rupiah (default 500 juta), because a thin book eats the edge.
  Stop: entry - --atr-mult x ATR(14) of the signal day, rounded DOWN to the
  tick, at least one tick below the entry. Target: entry + --target-r x the
  risk, rounded UP to the tick. Each day: the stop is checked before the
  target, so a day that touches both counts as the stop. A gap through either
  fills at the open. A day locked at ARB (high = low at the lower limit)
  cannot be sold: the trade waits for the next day. After --hold days it
  exits at the close.
  R is net of fees: (exit x (1 - fee_sell) - entry x (1 + fee_buy)) / risk.
  A win is a trade with net R > 0.

Strategies:
  breakout  close > highest high of the previous 20 days, volume > 1.5 x avg(20)
  pullback  MA20 > MA50, low touched MA20, closed back above it, green candle
  rsi2      RSI(2) < 10 while close > MA50 (a dip in an uptrend)
  volspike  volume > 3 x avg(20), close up on the day, in the top 25 % of the range
  orb       (intraday, `orb`) first 5-minute close above the 15-minute opening
            range of session 1, stop at the range low (or --stop-rule mid),
            out at the target, the stop, or the last bar before 15:50

Verdict: under 30 trades "Sampel terlalu sedikit". Otherwise average net R
>= +0.10 and profit factor >= 1.2: "Ada edge historis"; <= -0.10: "Negatif";
in between: "Mendekati impas". Win rate alone never decides: a 2R target
breaks even at a 33 % win rate, a 0.5R target needs 67 %.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<KODE>.JK (unofficial,
free, can lag, falls back to query1). --offline FILE reads a saved chart JSON,
--csv FILE your own daily bars. This is history, not a prediction.
"""

import argparse
import csv
import datetime
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (trader-idx; standard-library Python)"
HOSTS = ("query2", "query1")
WIB = datetime.timedelta(hours=7)
EPOCH = datetime.datetime(1970, 1, 1)
LOT = 100
MIN_PRICE = 50
DEFAULTS = {
    "fee_buy": 0.15, "fee_sell": 0.25, "arb": 15.0, "arb_tiered": False,
    "min_value": 500e6, "atr_mult": 1.5, "target_r": 2.0, "hold": 10,
}
STRATEGIES = ("breakout", "pullback", "rsi2", "volspike")


class IDXError(Exception):
    pass


# ---------------------------------------------------------------- IDX rules

def tick_size(price):
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_down(price):
    t = tick_size(price)
    p = math.floor(price / t + 1e-9) * t
    return int(p) if p == int(p) else p


def round_up(price):
    t = tick_size(price)
    p = math.ceil(price / t - 1e-9) * t
    if tick_size(p) != t:  # crossed into a bigger band: round on that band
        p = math.ceil(price / tick_size(p) - 1e-9) * tick_size(p)
    return int(p) if p == int(p) else p


def tick_up(price):
    return price + tick_size(price)


def tick_down(price):
    return price - tick_size(price - 1) if price > MIN_PRICE else price


def ara_pct(prev):
    if prev <= 200:
        return 35.0
    if prev <= 5000:
        return 25.0
    return 20.0


def limits(prev, arb=None, arb_tiered=None):
    """(ARA price, ARB price) from the previous close."""
    arb = DEFAULTS["arb"] if arb is None else arb
    arb_tiered = DEFAULTS["arb_tiered"] if arb_tiered is None else arb_tiered
    up = round_down(prev * (1 + ara_pct(prev) / 100.0))
    down_pct = ara_pct(prev) if arb_tiered else arb
    down = max(MIN_PRICE, round_up(prev * (1 - down_pct / 100.0)))
    return up, down


def on_grid(price):
    return abs(price / tick_size(price) - round(price / tick_size(price))) < 1e-9


def rupiah(x, dec=0):
    s = ("{:,.%df}" % dec).format(x).replace(",", "X").replace(".", ",").replace("X", ".")
    return "Rp " + s


def px(x):
    """A price as IDX shows it: whole rupiah when it is whole."""
    return "%d" % x if float(x) == int(x) else "%.2f" % x


def size_position(modal, risk_pct, entry, stop, fee_buy=None, fee_sell=None):
    fee_buy = DEFAULTS["fee_buy"] if fee_buy is None else fee_buy
    fee_sell = DEFAULTS["fee_sell"] if fee_sell is None else fee_sell
    if stop >= entry:
        raise IDXError("stop harus di bawah harga beli (long only)")
    if modal <= 0 or risk_pct <= 0:
        raise IDXError("modal dan risk-pct harus lebih dari 0")
    buy_cost = entry * (1 + fee_buy / 100.0)
    loss_share = buy_cost - stop * (1 - fee_sell / 100.0)
    budget_risk = modal * risk_pct / 100.0
    by_risk = int(budget_risk // (loss_share * LOT))
    by_cash = int(modal // (buy_cost * LOT))
    lots = max(0, min(by_risk, by_cash))
    return {
        "lots": lots, "shares": lots * LOT, "by_risk": by_risk, "by_cash": by_cash,
        "cost": lots * LOT * buy_cost, "loss_at_stop": lots * LOT * loss_share,
        "risk_budget": budget_risk, "loss_per_share": loss_share,
    }


# ---------------------------------------------------------------- data

def now_wib():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + WIB


def symbol(code):
    code = code.strip().upper()
    return code if "." in code or code.startswith("^") else code + ".JK"


def chart_url(sym, rng, interval, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (
        host, urllib.parse.quote(sym), rng, interval)


def fetch_chart(sym, rng, interval):
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            time.sleep(1.0)
        try:
            req = urllib.request.Request(chart_url(sym, rng, interval, host),
                                         headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e.code
            if e.code == 404:
                raise IDXError("Yahoo tidak punya data %s (HTTP 404): cek kode sahamnya" % sym)
            if e.code == 429 or e.code >= 500:
                continue
            raise IDXError("Yahoo menjawab HTTP %d untuk %s" % (e.code, sym))
        except (urllib.error.URLError, OSError) as e:
            raise IDXError("tidak bisa menghubungi Yahoo: %s" % getattr(e, "reason", e))
    raise IDXError("Yahoo menjawab HTTP %s untuk %s di dua host. Coba lagi nanti." % (last, sym))


def read_text(path):
    if not os.path.exists(path):
        raise IDXError("file %s tidak ada" % path)
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def parse_chart(text, sym=""):
    """Yahoo chart JSON -> list of bars {t (WIB datetime), o, h, l, c, v}."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise IDXError("data chart %s tidak bisa dibaca" % sym)
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            v = q.get("volume", [0] * len(ts))[i] or 0
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        when = EPOCH + datetime.timedelta(seconds=t) + WIB
        bars.append({"t": when, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)})
    return bars


def parse_csv(path):
    """Daily bars from a CSV with a header Date,Open,High,Low,Close,Volume (any case)."""
    text = read_text(path)
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        raise IDXError("CSV %s kosong" % path)
    keys = {k.strip().lower(): k for k in rows[0].keys() if k}
    need = ("date", "open", "high", "low", "close", "volume")
    miss = [k for k in need if k not in keys]
    if miss:
        raise IDXError("CSV %s tidak punya kolom: %s" % (path, ", ".join(miss)))
    bars = []
    for r in rows:
        try:
            d = datetime.datetime.strptime(r[keys["date"]].strip()[:10], "%Y-%m-%d")
            bar = {"t": d}
            for k, s in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v")):
                bar[s] = float(r[keys[k]].replace(",", ""))
        except (ValueError, AttributeError):
            continue
        bars.append(bar)
    bars.sort(key=lambda b: b["t"])
    return bars


def daily_bars(a):
    if a.csv:
        return parse_csv(a.csv), "csv " + a.csv
    sym = symbol(a.code)
    text = read_text(a.offline) if a.offline else fetch_chart(sym, a.range, "1d")
    bars = parse_chart(text, sym)
    today = now_wib().date()
    # a daily bar for today is still forming during the session: leave it out
    bars = [b for b in bars if a.offline or b["t"].date() < today]
    return bars, ("offline " + a.offline) if a.offline else "Yahoo %s %s 1d" % (sym, a.range)


# ---------------------------------------------------------------- indicators

def sma(vals, n, i):
    if i + 1 < n:
        return None
    return sum(vals[i + 1 - n:i + 1]) / float(n)


def atr(bars, n, i):
    if i < n:
        return None
    trs = []
    for k in range(i + 1 - n, i + 1):
        pc = bars[k - 1]["c"]
        b = bars[k]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return sum(trs) / float(n)


def rsi(closes, n, i):
    if i < n:
        return None
    gain = loss = 0.0
    for k in range(i + 1 - n, i + 1):
        d = closes[k] - closes[k - 1]
        if d > 0:
            gain += d
        else:
            loss -= d
    if loss == 0:
        return 100.0
    rs = gain / loss
    return 100.0 - 100.0 / (1.0 + rs)


def signal(name, bars, i, closes, vols):
    b = bars[i]
    if name == "breakout":
        if i < 20:
            return False
        hh = max(x["h"] for x in bars[i - 20:i])
        av = sma(vols, 20, i - 1)
        return b["c"] > hh and av is not None and av > 0 and b["v"] > 1.5 * av
    if name == "pullback":
        m20, m50 = sma(closes, 20, i), sma(closes, 50, i)
        if m20 is None or m50 is None:
            return False
        return m20 > m50 and b["l"] <= m20 < b["c"] and b["c"] > b["o"]
    if name == "rsi2":
        r, m50 = rsi(closes, 2, i), sma(closes, 50, i)
        return r is not None and m50 is not None and r < 10 and b["c"] > m50
    if name == "volspike":
        if i < 21:
            return False
        av = sma(vols, 20, i - 1)
        rng = b["h"] - b["l"]
        return (av is not None and av > 0 and b["v"] > 3 * av and b["c"] > bars[i - 1]["c"]
                and rng > 0 and (b["c"] - b["l"]) / rng >= 0.75)
    raise IDXError("strategi tidak dikenal: %s" % name)


# ---------------------------------------------------------------- backtest

def locked(bar):
    return bar["h"] == bar["l"]


def net_r(entry, exit_, risk, fee_buy, fee_sell):
    return (exit_ * (1 - fee_sell / 100.0) - entry * (1 + fee_buy / 100.0)) / risk


def backtest(bars, strategy, target_r=None, atr_mult=None, hold=None, min_value=None,
             fee_buy=None, fee_sell=None, arb=None, arb_tiered=None):
    target_r = DEFAULTS["target_r"] if target_r is None else target_r
    atr_mult = DEFAULTS["atr_mult"] if atr_mult is None else atr_mult
    hold = DEFAULTS["hold"] if hold is None else hold
    min_value = DEFAULTS["min_value"] if min_value is None else min_value
    fee_buy = DEFAULTS["fee_buy"] if fee_buy is None else fee_buy
    fee_sell = DEFAULTS["fee_sell"] if fee_sell is None else fee_sell
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    values = [b["c"] * b["v"] for b in bars]
    trades, skipped = [], {"likuiditas": 0, "terkunci_ara": 0, "tanpa_volume": 0}
    i, n = 50, len(bars)
    while i < n - 1:
        if not signal(strategy, bars, i, closes, vols):
            i += 1
            continue
        av = sma(values, 20, i)
        if av is None or av < min_value:
            skipped["likuiditas"] += 1
            i += 1
            continue
        e = bars[i + 1]
        up, _ = limits(bars[i]["c"], arb, arb_tiered)
        if e["v"] <= 0:
            skipped["tanpa_volume"] += 1
            i += 1
            continue
        if locked(e) and e["o"] >= up:
            skipped["terkunci_ara"] += 1
            i += 1
            continue
        entry = e["o"]
        a_ = atr(bars, 14, i)
        stop = round_down(entry - atr_mult * a_) if a_ else tick_down(entry)
        if stop >= entry:
            stop = tick_down(entry)
        stop = max(stop, MIN_PRICE)
        risk = entry - stop
        if risk <= 0:
            i += 1
            continue
        target = round_up(entry + target_r * risk)
        exit_, why, k = None, None, i + 1
        while k < n:
            b = bars[k]
            _, down = limits(bars[k - 1]["c"], arb, arb_tiered)
            stuck = locked(b) and b["l"] <= down  # locked at ARB: no buyers
            if not stuck:
                if b["l"] <= stop:
                    exit_, why = (b["o"] if b["o"] < stop else stop), "stop"
                    break
                if b["h"] >= target:
                    exit_, why = (b["o"] if b["o"] > target else target), "target"
                    break
                if k - (i + 1) + 1 >= hold:
                    exit_, why = b["c"], "waktu"
                    break
            k += 1
        if exit_ is None:  # the data ran out with the trade still open
            k = n - 1
            exit_, why = bars[k]["c"], "terbuka"
        trades.append({
            "signal": bars[i]["t"].strftime("%Y-%m-%d"), "entry_date": e["t"].strftime("%Y-%m-%d"),
            "exit_date": bars[k]["t"].strftime("%Y-%m-%d"), "entry": entry, "stop": stop,
            "target": target, "exit": exit_, "why": why, "days": k - (i + 1) + 1,
            "r": round(net_r(entry, exit_, risk, fee_buy, fee_sell), 4),
        })
        i = k + 1  # one position at a time: look for the next signal after the exit
    return {"strategy": strategy, "trades": trades, "skipped": skipped,
            "params": {"target_r": target_r, "atr_mult": atr_mult, "hold": hold,
                       "min_value": min_value, "fee_buy": fee_buy, "fee_sell": fee_sell}}


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "wins": 0, "win_rate": 0.0, "avg_r": 0.0, "pf": 0.0, "sum_r": 0.0,
                "max_losses": 0, "avg_days": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gl = -sum(losses)
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    return {
        "n": n, "wins": len(wins), "win_rate": 100.0 * len(wins) / n,
        "avg_r": sum(rs) / n, "sum_r": sum(rs),
        "pf": (sum(wins) / gl) if gl > 0 else (None if wins else 0.0),  # None: no losing trade
        "max_losses": worst, "avg_days": sum(t["days"] for t in trades) / float(n),
        "avg_win_r": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_r": (sum(losses) / len(losses)) if losses else 0.0,
    }


def verdict(s):
    if s["n"] < 30:
        return "Sampel terlalu sedikit (%d trade, butuh minimal 30): belum bisa disimpulkan." % s["n"]
    if s["avg_r"] >= 0.10 and (s["pf"] is None or s["pf"] >= 1.2):
        return "Ada edge historis: rata-rata %+.2fR per trade setelah fee. Bukan jaminan ke depan." % s["avg_r"]
    if s["avg_r"] <= -0.10:
        return "Negatif: rata-rata %+.2fR per trade setelah fee. Jangan dipakai di saham ini." % s["avg_r"]
    return "Mendekati impas: rata-rata %+.2fR per trade setelah fee." % s["avg_r"]


# ---------------------------------------------------------------- intraday ORB

def session_end(day):
    return datetime.time(15, 50)


def orb_backtest(bars, or_minutes=15, target_r=1.0, stop_rule="low", fee_buy=None, fee_sell=None):
    fee_buy = DEFAULTS["fee_buy"] if fee_buy is None else fee_buy
    fee_sell = DEFAULTS["fee_sell"] if fee_sell is None else fee_sell
    days = {}
    for b in bars:
        days.setdefault(b["t"].date(), []).append(b)
    today = now_wib().date()
    start = datetime.time(9, 0)
    or_end = (datetime.datetime.combine(today, start) + datetime.timedelta(minutes=or_minutes)).time()
    trades, sessions = [], 0
    for day in sorted(days):
        if day >= today:
            continue  # today is not finished
        D = [b for b in days[day] if start <= b["t"].time() < session_end(day)]
        rng = [b for b in D if b["t"].time() < or_end]
        rest = [b for b in D if b["t"].time() >= or_end]
        if len(rng) < max(1, or_minutes // 5) or not rest:
            continue
        sessions += 1
        oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
        entry_i = None
        for j, b in enumerate(rest):
            if b["c"] > oh:
                entry_i = j
                break
        if entry_i is None:
            continue
        entry = rest[entry_i]["c"]
        stop = round_down((oh + ol) / 2.0) if stop_rule == "mid" else round_down(ol)
        if stop >= entry:
            stop = tick_down(entry)
        risk = entry - stop
        target = round_up(entry + target_r * risk)
        exit_, why = rest[-1]["c"], "tutup"
        for b in rest[entry_i + 1:]:
            if b["l"] <= stop:
                exit_, why = (b["o"] if b["o"] < stop else stop), "stop"
                break
            if b["h"] >= target:
                exit_, why = (b["o"] if b["o"] > target else target), "target"
                break
        trades.append({"date": day.strftime("%Y-%m-%d"), "entry_time": rest[entry_i]["t"].strftime("%H:%M"),
                       "entry": entry, "stop": stop, "target": target, "exit": exit_, "why": why,
                       "days": 1, "r": round(net_r(entry, exit_, risk, fee_buy, fee_sell), 4)})
    return {"strategy": "orb", "trades": trades, "sessions": sessions,
            "params": {"or_minutes": or_minutes, "target_r": target_r, "stop_rule": stop_rule,
                       "fee_buy": fee_buy, "fee_sell": fee_sell}}


# ---------------------------------------------------------------- report

def line(name, s):
    pf = "n/a" if s["pf"] is None else "%.2f" % s["pf"]
    return "  %-9s %4d trade  win rate %5.1f %%  rata-rata %+.2fR  PF %5s  rugi beruntun maks %d" % (
        name, s["n"], s["win_rate"], s["avg_r"], pf, s["max_losses"])


def cmd_rules(a):
    p = a.price
    if not on_grid(p):
        print("Harga %s TIDAK di grid fraksi %d. Terdekat: %s / %s." % (
            px(p), tick_size(p), px(round_down(p)), px(round_up(p))))
        p = round_down(p)
    out = {"price": p, "tick": tick_size(p), "on_grid": on_grid(p), "lot": LOT,
           "lot_value": p * LOT, "next_up": tick_up(p), "next_down": tick_down(p)}
    if a.prev:
        up, down = limits(a.prev, a.arb, a.arb_tiered)
        out.update({"prev": a.prev, "ara": up, "arb": down, "ara_pct": ara_pct(a.prev)})
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    print("Harga %s: fraksi %d, 1 lot = %d lembar = %s." % (px(p), out["tick"], LOT, rupiah(out["lot_value"])))
    print("  Satu tick naik: %s, satu tick turun: %s." % (px(out["next_up"]), px(out["next_down"])))
    if a.prev:
        print("  Dari penutupan %s: ARA %s (+%d %%), ARB %s." % (
            px(a.prev), px(out["ara"]), out["ara_pct"], px(out["arb"])))
    return 0


def cmd_size(a):
    entry, stop = a.entry, a.stop
    if not on_grid(entry) or not on_grid(stop):
        print("Catatan: harga dibulatkan ke fraksi: entry %s, stop %s." % (px(round_up(entry)), px(round_down(stop))))
        entry, stop = round_up(entry), round_down(stop)
    s = size_position(a.modal, a.risk_pct, entry, stop, a.fee_buy, a.fee_sell)
    if a.json:
        print(json.dumps(s, indent=2))
        return 0
    print("Modal %s, risiko %.2f %% = %s per trade." % (rupiah(a.modal), a.risk_pct, rupiah(s["risk_budget"])))
    print("  Rugi per lembar sampai stop (termasuk fee): %s." % rupiah(s["loss_per_share"], 2))
    if s["lots"] == 0:
        print("  0 lot: risiko satu lot sudah melebihi batas. Perlebar modal atau perkecil jarak stop.")
        return 0
    print("  Beli %d lot (%d lembar) @ %s = %s." % (s["lots"], s["shares"], px(entry), rupiah(s["cost"])))
    print("  Rugi bila kena stop %s: %s." % (px(stop), rupiah(s["loss_at_stop"])))
    if s["by_cash"] < s["by_risk"]:
        print("  Dibatasi modal, bukan risiko.")
    return 0


def run_daily(a, bars, names):
    out = []
    for name in names:
        r = backtest(bars, name, a.target_r, a.atr_mult, a.hold, a.min_value,
                     a.fee_buy, a.fee_sell, a.arb, a.arb_tiered)
        r["stats"] = stats(r["trades"])
        r["verdict"] = verdict(r["stats"])
        out.append(r)
    return out


def print_daily(code, src, bars, results, a):
    print("%s: %d hari bursa (%s sampai %s), sumber %s." % (
        code.upper(), len(bars), bars[0]["t"].strftime("%Y-%m-%d"), bars[-1]["t"].strftime("%Y-%m-%d"), src))
    p = results[0]["params"]
    print("Stop %.1f x ATR(14), target %.1fR, maksimal %d hari, fee beli %.2f %% jual %.2f %%, "
          "likuiditas min %s/hari." % (p["atr_mult"], p["target_r"], p["hold"], p["fee_buy"], p["fee_sell"],
                                       rupiah(p["min_value"])))
    be = 100.0 / (1 + p["target_r"])
    print("Impas di target %.1fR butuh win rate sekitar %.0f %% (sebelum fee)." % (p["target_r"], be))
    for r in sorted(results, key=lambda r: -r["stats"]["avg_r"]):
        print(line(r["strategy"], r["stats"]))
        sk = r["skipped"]
        if any(sk.values()):
            print("            dilewati: %d likuiditas, %d terkunci ARA, %d tanpa volume" % (
                sk["likuiditas"], sk["terkunci_ara"], sk["tanpa_volume"]))
        print("            " + r["verdict"])
        if a.trades:
            for t in r["trades"]:
                print("            %s beli %s stop %s target %s -> %s %s (%s, %d hari) %+.2fR" % (
                    t["entry_date"], px(t["entry"]), px(t["stop"]), px(t["target"]), t["exit_date"], px(t["exit"]),
                    t["why"], t["days"], t["r"]))


def cmd_backtest(a):
    bars, src = daily_bars(a)
    if len(bars) < 80:
        raise IDXError("hanya %d hari data, butuh minimal 80" % len(bars))
    names = STRATEGIES if a.strategy == "all" else (a.strategy,)
    results = run_daily(a, bars, names)
    if a.json:
        print(json.dumps({"code": a.code.upper(), "source": src, "bars": len(bars), "results": results},
                         indent=2, default=str))
    else:
        print_daily(a.code, src, bars, results, a)
        print("Ini data masa lalu, bukan prediksi. Bukan saran investasi (not financial advice).")
    return 0


def cmd_orb(a):
    sym = symbol(a.code)
    text = read_text(a.offline) if a.offline else fetch_chart(sym, "60d", "5m")
    bars = parse_chart(text, sym)
    r = orb_backtest(bars, a.or_minutes, a.target_r, a.stop_rule, a.fee_buy, a.fee_sell)
    s = stats(r["trades"])
    if a.json:
        r["stats"], r["verdict"] = s, verdict(s)
        print(json.dumps(r, indent=2, default=str))
        return 0
    if r["sessions"] < 20:
        print("Hanya %d sesi 5 menit yang lengkap, butuh minimal 20." % r["sessions"])
    print("%s ORB %d menit sesi 1, stop %s, target %.1fR, keluar paling lambat 15:50 WIB." % (
        a.code.upper(), a.or_minutes, "tengah range" if a.stop_rule == "mid" else "low range", a.target_r))
    print("%d sesi, setup muncul %d kali." % (r["sessions"], s["n"]))
    print(line("orb", s))
    print("  " + verdict(s))
    if a.trades:
        for t in r["trades"]:
            print("  %s %s beli %s stop %s target %s -> %s (%s) %+.2fR" % (
                t["date"], t["entry_time"], px(t["entry"]), px(t["stop"]), px(t["target"]), px(t["exit"]),
                t["why"], t["r"]))
    print("Ini data masa lalu, bukan prediksi. Bukan saran investasi (not financial advice).")
    return 0


def cross_backtest(bars, fast=5, slow=20, start=None, fee_buy=None, fee_sell=None, arb=None, arb_tiered=None):
    """Golden cross: buy at the next open after MA(fast) closes above MA(slow), sell at the
    next open after the death cross (MA(fast) closes back below). Returns in percent, net of fees.
    start (YYYY-MM-DD): only signals on or after it count; earlier bars still warm up the MAs."""
    fee_buy = DEFAULTS["fee_buy"] if fee_buy is None else fee_buy
    fee_sell = DEFAULTS["fee_sell"] if fee_sell is None else fee_sell
    closes = [b["c"] for b in bars]
    trades, i, n = [], slow, len(bars)
    while i < n - 1:
        f0, s0 = sma(closes, fast, i - 1), sma(closes, slow, i - 1)
        f1, s1 = sma(closes, fast, i), sma(closes, slow, i)
        day = bars[i]["t"].strftime("%Y-%m-%d")
        if None in (f0, s0, f1, s1) or not (f0 <= s0 and f1 > s1) or (start and day < start):
            i += 1
            continue
        e = bars[i + 1]
        up, _ = limits(bars[i]["c"], arb, arb_tiered)
        if e["v"] <= 0 or (locked(e) and e["o"] >= up):
            i += 1
            continue
        entry, k, exit_, why, xk = e["o"], i + 1, None, None, None
        while k < n - 1:
            if sma(closes, fast, k) < sma(closes, slow, k):  # death cross on day k's close
                j = k + 1
                while j < n - 1 and locked(bars[j]) and bars[j]["l"] <= limits(bars[j - 1]["c"], arb, arb_tiered)[1]:
                    j += 1  # locked at ARB: cannot sell yet
                exit_, why, xk = bars[j]["o"], "death cross", j
                break
            k += 1
        if exit_ is None:
            xk = n - 1
            exit_, why = bars[xk]["c"], "terbuka"
        ret = 100.0 * (exit_ * (1 - fee_sell / 100.0) - entry * (1 + fee_buy / 100.0)) / (entry * (1 + fee_buy / 100.0))
        trades.append({"signal": day, "entry_date": e["t"].strftime("%Y-%m-%d"), "entry": entry,
                       "exit_date": bars[xk]["t"].strftime("%Y-%m-%d"), "exit": exit_, "why": why,
                       "days": xk - (i + 1) + 1, "ret": round(ret, 3)})
        i = xk  # next signal can come after the exit
    return trades


def cross_stats(trades):
    n = len(trades)
    rets = [t["ret"] for t in trades]
    closed = [t for t in trades if t["why"] != "terbuka"]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    total = 1.0
    for r in rets:
        total *= 1 + r / 100.0
    return {"n": n, "closed": len(closed), "wins": len(wins),
            "win_rate": 100.0 * len(wins) / n if n else 0.0,
            "avg_ret": sum(rets) / n if n else 0.0,
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "compound": 100.0 * (total - 1), "best": max(rets) if rets else 0.0,
            "worst": min(rets) if rets else 0.0}


def cmd_cross(a):
    bars, src = daily_bars(a)
    if len(bars) < a.slow + 2:
        raise IDXError("hanya %d hari data" % len(bars))
    trades = cross_backtest(bars, a.fast, a.slow, a.start, a.fee_buy, a.fee_sell, a.arb, a.arb_tiered)
    s = cross_stats(trades)
    held = [b for b in bars if not a.start or b["t"].strftime("%Y-%m-%d") >= a.start]
    bh = None
    if len(held) > 1:
        bh = 100.0 * (held[-1]["c"] / held[0]["o"] - 1)
    if a.json:
        print(json.dumps({"code": a.code.upper(), "source": src, "fast": a.fast, "slow": a.slow,
                          "start": a.start, "stats": s, "buy_and_hold": bh, "trades": trades}, indent=2))
        return 0
    print("%s golden cross MA%d / MA%d, sinyal sejak %s, data sampai %s (%s)." % (
        a.code.upper(), a.fast, a.slow, a.start or bars[a.slow]["t"].strftime("%Y-%m-%d"),
        bars[-1]["t"].strftime("%Y-%m-%d"), src))
    print("Beli di open setelah MA%d tutup di atas MA%d, jual di open setelah death cross. "
          "Fee beli %.2f %%, jual %.2f %%." % (a.fast, a.slow, a.fee_buy, a.fee_sell))
    for t in trades:
        print("  %s beli %s -> %s %s %s (%d hari) %+.2f %%" % (
            t["entry_date"], px(t["entry"]), t["exit_date"], px(t["exit"]), t["why"], t["days"], t["ret"]))
    print("%d trade (%d sudah ditutup), menang %d: win rate %.1f %%." % (s["n"], s["closed"], s["wins"], s["win_rate"]))
    if s["n"]:
        print("Rata-rata %+.2f %% per trade (menang %+.2f %%, kalah %+.2f %%), terbaik %+.2f %%, terburuk %+.2f %%." % (
            s["avg_ret"], s["avg_win"], s["avg_loss"], s["best"], s["worst"]))
        print("Total bila diulang terus (compounding): %+.2f %%." % s["compound"])
    if bh is not None:
        print("Pembanding, beli dan tahan di periode yang sama: %+.2f %% (sebelum fee)." % bh)
    if s["n"] < 30:
        print("Sampel kecil (%d trade): win rate ini belum bisa dijadikan pegangan." % s["n"])
    print("Ini data masa lalu, bukan prediksi. Bukan saran investasi (not financial advice).")
    return 0


def cmd_scan(a):
    if a.csv or a.offline:
        raise IDXError("scan membaca Yahoo untuk tiap kode; untuk satu file pakai `backtest --csv`")
    rows, errors = [], []
    for code in a.codes:
        a.code = code
        try:
            bars, _ = daily_bars(a)
            if len(bars) < 80:
                raise IDXError("hanya %d hari data" % len(bars))
        except IDXError as e:
            errors.append("%s: %s" % (code.upper(), e))
            continue
        for r in run_daily(a, bars, STRATEGIES):
            rows.append((code.upper(), r["strategy"], r["stats"]))
        time.sleep(0.5)  # polite to Yahoo
    rows.sort(key=lambda x: (x[2]["n"] >= 30, x[2]["avg_r"]), reverse=True)
    if a.json:
        print(json.dumps([{"code": c, "strategy": n, "stats": s} for c, n, s in rows] +
                         [{"error": e} for e in errors], indent=2))
        return 0
    print("Urut dari rata-rata R setelah fee (yang sampelnya >= 30 trade lebih dulu):")
    for c, n, s in rows:
        print("%-6s" % c + line(n, s)[1:] + ("" if s["n"] >= 30 else "  (sampel kecil)"))
    for e in errors:
        print("Gagal: " + e)
    print("Ini data masa lalu, bukan prediksi. Bukan saran investasi (not financial advice).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="trader-idx: aturan BEI, ukuran lot dan backtest saham Indonesia.")
    sub = ap.add_subparsers(dest="cmd")

    def fees(p):
        p.add_argument("--fee-buy", type=float, default=DEFAULTS["fee_buy"], help="fee beli, persen")
        p.add_argument("--fee-sell", type=float, default=DEFAULTS["fee_sell"], help="fee jual, persen")
        p.add_argument("--json", action="store_true")

    def limit_args(p):
        p.add_argument("--arb", type=float, default=DEFAULTS["arb"], help="batas ARB, persen")
        p.add_argument("--arb-tiered", action="store_true", help="ARB ikut tingkatan ARA")

    def data(p):
        p.add_argument("--csv", help="CSV harian sendiri: Date,Open,High,Low,Close,Volume")
        p.add_argument("--offline", help="file chart Yahoo tersimpan (.json / .json.gz)")
        p.add_argument("--range", default="2y", help="rentang data Yahoo (default 2y)")
        p.add_argument("--target-r", type=float, default=DEFAULTS["target_r"])
        p.add_argument("--atr-mult", type=float, default=DEFAULTS["atr_mult"])
        p.add_argument("--hold", type=int, default=DEFAULTS["hold"], help="maks hari pegang")
        p.add_argument("--min-value", type=float, default=DEFAULTS["min_value"],
                       help="nilai transaksi rata-rata 20 hari minimum, rupiah")
        p.add_argument("--trades", action="store_true", help="tampilkan setiap trade")
        fees(p)
        limit_args(p)

    p = sub.add_parser("rules", help="fraksi harga, ARA/ARB, nilai lot")
    p.add_argument("price", type=float)
    p.add_argument("--prev", type=float, help="harga penutupan kemarin")
    p.add_argument("--json", action="store_true")
    limit_args(p)

    p = sub.add_parser("size", help="jumlah lot dari modal dan risiko")
    p.add_argument("--modal", type=float, required=True)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float, required=True)
    fees(p)

    p = sub.add_parser("backtest", help="backtest strategi harian")
    p.add_argument("code")
    p.add_argument("--strategy", default="all", choices=("all",) + STRATEGIES)
    data(p)

    p = sub.add_parser("orb", help="backtest opening range breakout intraday 5 menit")
    p.add_argument("code")
    p.add_argument("--offline")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--target-r", type=float, default=1.0)
    p.add_argument("--stop-rule", default="low", choices=("low", "mid"))
    p.add_argument("--trades", action="store_true")
    fees(p)

    p = sub.add_parser("cross", help="golden cross MA cepat / MA lambat, keluar di death cross")
    p.add_argument("code")
    p.add_argument("--fast", type=int, default=5)
    p.add_argument("--slow", type=int, default=20)
    p.add_argument("--from", dest="start", help="hanya sinyal sejak tanggal ini, YYYY-MM-DD")
    data(p)

    p = sub.add_parser("scan", help="semua strategi untuk beberapa saham")
    p.add_argument("codes", nargs="+")
    data(p)

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"rules": cmd_rules, "size": cmd_size, "backtest": cmd_backtest,
                "orb": cmd_orb, "cross": cmd_cross, "scan": cmd_scan}[a.cmd](a)
    except IDXError as e:
        print("trader-idx: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
