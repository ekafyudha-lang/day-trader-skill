import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import FIX, load

fc = load("trader-firstcandle", "firstcandle")
size = load("trader-budget", "size")

DAY = datetime.date(2026, 11, 12)   # a Thursday, New York on EST (UTC-5)

# 9:30 to 9:41, one candle a minute: (open, high, low, close)
LONG_DAY = [
    (100.5, 101.0, 100.2, 100.6),   # 9:30  first candle, five 1-minute bars: range 100.00-101.00
    (100.6, 100.9, 100.0, 100.3),   # 9:31
    (100.3, 100.8, 100.1, 100.7),   # 9:32
    (100.7, 100.9, 100.4, 100.5),   # 9:33
    (100.5, 100.8, 100.3, 100.6),   # 9:34
    (100.6, 100.9, 100.4, 100.8),   # 9:35  inside
    (100.8, 101.4, 100.7, 101.3),   # 9:36  BREAK: first close above 101.00, low 100.70 = the stop
    (101.3, 102.2, 101.25, 102.1),  # 9:37  low 101.25 > 9:35 high 100.90: fair value gap
    (102.1, 102.6, 101.6, 102.4),   # 9:38
    (102.4, 102.5, 101.5, 101.7),   # 9:39
    (101.7, 101.8, 100.9, 101.1),   # 9:40  wick back into the range (low 100.90)
    (101.1, 101.5, 101.0, 101.4),   # 9:41  closes above again: ENTRY 101.40, risk 0.70, target 104.20
]


def flat(px, n):
    return [(px, px + 0.1, px - 0.1, px)] * n


def chart_json(rows, day=DAY, sym="TEST"):
    start = fc.ts_of(fc.eastern_to_utc(datetime.datetime.combine(day, datetime.time(9, 30))))
    ts = [start + 60 * i for i in range(len(rows))]
    return {"chart": {"result": [{
        "meta": {"symbol": sym, "regularMarketTime": ts[-1] + 60, "dataGranularity": "1m"},
        "timestamp": ts,
        "indicators": {"quote": [{"open": [r[0] for r in rows], "high": [r[1] for r in rows],
                                  "low": [r[2] for r in rows], "close": [r[3] for r in rows],
                                  "volume": [1000] * len(rows)}]}}], "error": None}}


def full_day(head, after):
    """head candles from 9:30, then `after` until 16:00 (390 one-minute candles)."""
    rows = list(head) + list(after)
    rows += flat(rows[-1][3], 390 - len(rows))
    return rows[:390]


def mirror(rows, axis=201.0):
    return [(axis - o, axis - lo, axis - h, axis - c) for o, h, lo, c in rows]


def bars_of(rows, day=DAY):
    chart = fc.parse_chart(json.dumps(chart_json(rows, day)), "TEST")
    return fc.sessions(chart, "1m")[day]


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fc.main(argv)
    return code, buf.getvalue()


class Rule(unittest.TestCase):
    def test_first_candle_is_the_five_minutes_from_930(self):
        res = fc.evaluate_day(bars_of(LONG_DAY[:5]))
        self.assertEqual(res["first_candle"], {"high": 101.0, "low": 100.0, "bars": 5})
        self.assertEqual(fc.evaluate_day(bars_of(LONG_DAY[:4]))["status"], "forming")

    def test_long_entry_on_the_double_break(self):
        res = fc.evaluate_day(bars_of(LONG_DAY))
        self.assertEqual(res["status"], "entry")
        e = res["entry"]
        self.assertEqual((e["side"], e["price"], e["stop"], e["target"]), ("long", 101.4, 100.7, 104.2))
        self.assertEqual(e["risk_per_share"], 0.7)
        self.assertEqual(e["time"], "9:42 AM")              # the 9:41 candle closes at 9:42
        self.assertEqual(e["break_candle"]["time"], "09:36")
        # the break candle itself is the gap's middle: 9:37's low 101.25 is above 9:35's high 100.90
        self.assertEqual(e["fvg"]["candles"], ["09:35", "09:36", "09:37"])
        self.assertEqual((e["fvg"]["gap_low"], e["fvg"]["gap_high"]), (100.9, 101.25))
        self.assertEqual(e["back_in"]["time"], "09:40")

    def test_no_entry_without_coming_back_inside(self):
        res = fc.evaluate_day(bars_of(LONG_DAY[:10] + [(101.7, 102.0, 101.2, 101.9)]))
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(res["sides"]["long"]["stage"], "fvg")

    def test_reentry_close_is_stricter(self):
        # the 9:40 candle only wicks in (close 101.10), so under --reentry close it is not back inside
        res = fc.evaluate_day(bars_of(LONG_DAY), reentry="close")
        self.assertEqual(res["status"], "waiting")

    def test_break_without_fvg_is_dropped(self):
        rows = LONG_DAY[:7] + [(101.3, 101.4, 100.8, 100.9)] + [(100.9, 101.0, 100.5, 100.6)]
        res = fc.evaluate_day(bars_of(rows))
        self.assertEqual(res["sides"]["long"]["stage"], "idle")
        self.assertTrue(any("before any fair value gap" in x for x in res["sides"]["long"]["log"]))

    def test_close_on_the_other_side_cancels(self):
        rows = LONG_DAY[:10] + [(101.7, 101.8, 99.5, 99.8)]    # closes below the low
        res = fc.evaluate_day(bars_of(rows), sides=("long",))
        self.assertEqual(res["sides"]["long"]["stage"], "idle")

    def test_short_is_the_mirror(self):
        res = fc.evaluate_day(bars_of(mirror(LONG_DAY)))
        e = res["entry"]
        self.assertEqual((e["side"], e["price"], e["stop"], e["target"]), ("short", 99.6, 100.3, 96.8))

    def test_long_only_ignores_the_short(self):
        res = fc.evaluate_day(bars_of(mirror(LONG_DAY)), sides=("long",))
        self.assertNotEqual(res["status"], "entry")

    def test_rr_changes_the_target(self):
        self.assertEqual(fc.evaluate_day(bars_of(LONG_DAY), rr=2)["entry"]["target"], 102.8)

    def test_last_entry_time(self):
        res = fc.evaluate_day(bars_of(LONG_DAY), last_entry=datetime.time(9, 40))
        self.assertNotEqual(res["status"], "entry")

    def test_only_closed_bars_count(self):
        bars = bars_of(LONG_DAY)
        at = fc.eastern_to_utc(datetime.datetime.combine(DAY, datetime.time(9, 41, 30)))
        self.assertEqual(fc.evaluate_day(fc.closed_bars(bars, at))["status"], "waiting")
        at = fc.eastern_to_utc(datetime.datetime.combine(DAY, datetime.time(9, 42)))
        self.assertEqual(fc.evaluate_day(fc.closed_bars(bars, at))["status"], "entry")


class Exits(unittest.TestCase):
    def entry_and_bars(self, after):
        bars = bars_of(full_day(LONG_DAY, after))
        return fc.evaluate_day(bars)["entry"], bars

    def test_target(self):
        e, bars = self.entry_and_bars([(101.4, 102.5, 101.2, 102.4), (102.4, 104.3, 102.3, 104.1)])
        x = fc.simulate_exit(bars, e, datetime.time(15, 55))
        self.assertEqual((x["outcome"], x["exit"], x["r"], x["time"]), ("target", 104.2, 4.0, "09:43"))

    def test_stop(self):
        e, bars = self.entry_and_bars([(101.4, 101.5, 100.6, 100.8)])
        x = fc.simulate_exit(bars, e, datetime.time(15, 55))
        self.assertEqual((x["outcome"], x["exit"], x["r"]), ("stop", 100.7, -1.0))

    def test_both_in_one_candle_counts_as_the_stop(self):
        e, bars = self.entry_and_bars([(101.4, 104.5, 100.5, 102.0)])
        self.assertEqual(fc.simulate_exit(bars, e, datetime.time(15, 55))["outcome"], "stop")

    def test_flat_by_the_close(self):
        e, bars = self.entry_and_bars([(101.4, 101.9, 101.3, 101.8)])
        x = fc.simulate_exit(bars, e, datetime.time(15, 55))
        self.assertEqual((x["outcome"], x["exit"], x["time"]), ("flat", 101.8, "15:55"))


class Backtest(unittest.TestCase):
    def test_two_sessions(self):
        win = full_day(LONG_DAY, [(101.4, 104.3, 101.3, 104.1)])
        lose = full_day(mirror(LONG_DAY), [(99.6, 100.4, 99.5, 100.2)])
        day2 = DAY + datetime.timedelta(days=1)
        a, b = chart_json(win, DAY), chart_json(lose, day2)
        res = a["chart"]["result"][0]
        res["timestamp"] += b["chart"]["result"][0]["timestamp"]
        for k in ("open", "high", "low", "close", "volume"):
            res["indicators"]["quote"][0][k] += b["chart"]["result"][0]["indicators"]["quote"][0][k]
        chart = fc.parse_chart(json.dumps(a), "TEST")
        bt = fc.backtest(chart, "1m", 4.0, "wick", datetime.time(15, 30), datetime.time(15, 55))
        s = bt["summary"]
        self.assertEqual((s["trades"], s["targets"], s["stops"], s["total_r"]), (2, 1, 1, 3.0))
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertEqual([r["side"] for r in bt["rows"]], ["long", "short"])

    def test_real_tsla_five_minute_bars_run(self):
        code, out = run(["backtest", "TSLA", "--interval", "5m", "--offline",
                         os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz"), "--config", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("first candle rule backtest on 5m candles, 1:4", out)
        self.assertRegex(out, r"\d+ sessions, \d+ trades")


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "TEST.json")
        with open(self.path, "w") as fh:
            json.dump(chart_json(full_day(LONG_DAY, [])), fh)

    def test_check_writes_a_plan_the_budget_manager_sizes(self):
        out_dir = os.path.join(self.tmp, "trader")
        code, out = run(["check", "TEST", "--offline", self.path, "--now", "2026-11-12 10:00",
                         "--config", "/nonexistent", "--write", out_dir])
        self.assertEqual(code, 0, out)
        self.assertIn("ENTRY LONG at 9:42 AM, $101.40. Stop $100.70", out)
        with open(os.path.join(out_dir, "plan.json")) as fh:
            plan = json.load(fh)
        self.assertEqual((plan["setup"], plan["side"], plan["entry"], plan["stop"], plan["target"]),
                         ("first-candle", "buy", 101.4, 100.7, 104.2))
        s = size.size_trade(plan, {"budget": 5000, "max_loss_per_trade_usd": 50}, today="2026-11-12")
        # 50 / 0.70 = 71 shares by risk; the budget's position cap may trim it, never raise it
        self.assertTrue(0 < int(s["qty"]) <= 71, s["qty"])

    def test_check_before_the_entry_waits(self):
        code, out = run(["check", "TEST", "--offline", self.path, "--now", "2026-11-12 09:39",
                         "--config", "/nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("WAITING", out)

    def test_watch_replays_to_the_entry(self):
        code, out = run(["watch", "TEST", "--offline", self.path, "--now", "2026-11-12 09:35",
                         "--config", "/nonexistent"])
        self.assertEqual(code, 0, out)
        self.assertIn("09:42 TEST ENTRY", out)


if __name__ == "__main__":
    unittest.main()
