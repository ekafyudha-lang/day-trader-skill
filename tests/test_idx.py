import datetime
import io
import json
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load

idx = load("trader-idx", "idx")


def daily(n=400, seed=7, start=120.0):
    """A seeded random walk on the IDX tick grid: synthetic, not real DOOH data."""
    rng = random.Random(seed)
    bars, c = [], start
    day = datetime.datetime(2024, 1, 2)
    for _ in range(n):
        while day.weekday() >= 5:
            day += datetime.timedelta(days=1)
        o = idx.round_down(max(60, c * (1 + rng.gauss(0, 0.01))))
        c = idx.round_down(max(60, o * (1 + rng.gauss(0.001, 0.03))))
        h = idx.round_up(max(o, c) * (1 + abs(rng.gauss(0, 0.01))))
        l = idx.round_down(min(o, c) * (1 - abs(rng.gauss(0, 0.01))))
        v = 5e6 * (1 + abs(rng.gauss(0, 1)) * (4 if rng.random() < 0.05 else 1))
        bars.append({"t": day, "o": o, "h": h, "l": l, "c": c, "v": v})
        day += datetime.timedelta(days=1)
    return bars


def chart_json(bars):
    ts = [int(((b["t"] - idx.WIB) - datetime.datetime(1970, 1, 1)).total_seconds()) for b in bars]
    q = {k: [b[s] for b in bars] for k, s in (("open", "o"), ("high", "h"), ("low", "l"),
                                               ("close", "c"), ("volume", "v"))}
    return json.dumps({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}})


class Rules(unittest.TestCase):
    def test_tick_bands(self):
        for price, tick in ((50, 1), (199, 1), (200, 2), (498, 2), (500, 5), (1995, 5),
                            (2000, 10), (4990, 10), (5000, 25), (9975, 25)):
            self.assertEqual(idx.tick_size(price), tick, price)

    def test_rounding_stays_on_the_grid(self):
        self.assertEqual(idx.round_down(201.5), 200)
        self.assertEqual(idx.round_up(201.5), 202)
        self.assertEqual(idx.round_up(499.5), 500)
        self.assertEqual(idx.round_up(1997), 2000)
        self.assertEqual(idx.tick_down(200), 199)
        self.assertEqual(idx.tick_down(500), 498)
        self.assertEqual(idx.tick_up(498), 500)
        for p in (57.3, 233.3, 777.7, 3333.3, 7777.7):
            self.assertTrue(idx.on_grid(idx.round_down(p)), p)
            self.assertTrue(idx.on_grid(idx.round_up(p)), p)

    def test_ara_arb(self):
        self.assertEqual(idx.limits(118), (159, 101))           # 35 % up, 15 % down
        self.assertEqual(idx.limits(1000), (1250, 850))         # 25 % up
        self.assertEqual(idx.limits(10000), (12000, 8500))      # 20 % up
        self.assertEqual(idx.limits(1000, arb_tiered=True), (1250, 750))
        self.assertEqual(idx.limits(55)[1], 50)                 # never below the minimum price

    def test_size_in_lots_with_fees(self):
        s = idx.size_position(10000000, 1, 122, 114)
        self.assertEqual(s["lots"], 118)
        self.assertLessEqual(s["loss_at_stop"], 100000)
        self.assertEqual(idx.size_position(1000000, 1, 5000, 4000)["lots"], 0)
        with self.assertRaises(idx.IDXError):
            idx.size_position(1000000, 1, 100, 110)
        cash = idx.size_position(1000000, 50, 1000, 990)        # risk allows more than the cash
        self.assertEqual(cash["lots"], cash["by_cash"])

    def test_symbol(self):
        self.assertEqual(idx.symbol("dooh"), "DOOH.JK")
        self.assertEqual(idx.symbol("BBCA.JK"), "BBCA.JK")
        self.assertEqual(idx.symbol("^JKSE"), "^JKSE")


class Backtest(unittest.TestCase):
    def test_every_strategy_runs_and_is_repeatable(self):
        bars = daily()
        for name in idx.STRATEGIES:
            a = idx.backtest(bars, name, min_value=0)
            b = idx.backtest(bars, name, min_value=0)
            self.assertEqual(a["trades"], b["trades"], name)
            for t in a["trades"]:
                self.assertLess(t["stop"], t["entry"], name)
                self.assertGreater(t["target"], t["entry"], name)
                self.assertTrue(idx.on_grid(t["stop"]) and idx.on_grid(t["target"]), t)
                self.assertGreater(t["entry_date"], t["signal"])   # entry is the next day's open
                self.assertGreaterEqual(t["exit_date"], t["entry_date"])
                self.assertLessEqual(t["days"], 10)
            ex = [(t["entry_date"], t["exit_date"]) for t in a["trades"]]
            for (e1, x1), (e2, x2) in zip(ex, ex[1:]):
                self.assertGreater(e2, x1, name)                   # one position at a time

    def test_liquidity_filter_skips_thin_stocks(self):
        bars = daily()
        r = idx.backtest(bars, "pullback", min_value=1e15)
        self.assertEqual(r["trades"], [])
        self.assertGreater(r["skipped"]["likuiditas"], 0)

    def test_fees_make_a_flat_trade_negative(self):
        self.assertLess(idx.net_r(100, 100, 5, 0.15, 0.25), 0)
        self.assertAlmostEqual(idx.net_r(100, 105, 5, 0, 0), 1.0)

    def test_stop_before_target_and_gaps(self):
        base = daily(60)
        d = base[-1]["t"]
        nxt = lambda k: d + datetime.timedelta(days=k)
        bars = [dict(b) for b in base]
        bars[-1].update({"c": 100.0, "h": 101.0, "l": 99.0, "o": 100.0, "v": 1e9})
        # force a signal on the last base bar with a strategy name we patch in
        bars += [{"t": nxt(1), "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6},
                 {"t": nxt(2), "o": 100, "h": 200, "l": 50, "c": 100, "v": 1e6}]
        orig = idx.signal
        try:
            idx.signal = lambda name, b, i, c, v: i == len(base) - 1
            r = idx.backtest(bars, "x", min_value=0)
        finally:
            idx.signal = orig
        self.assertEqual(len(r["trades"]), 1)
        self.assertEqual(r["trades"][0]["why"], "stop")            # both touched: the stop counts

    def test_stats_and_verdict(self):
        win = {"r": 2.0, "days": 3}
        loss = {"r": -1.0, "days": 2}
        s = idx.stats([win] * 12 + [loss] * 24)
        self.assertAlmostEqual(s["win_rate"], 100 * 12 / 36.0)
        self.assertAlmostEqual(s["avg_r"], 0.0)
        self.assertIn("impas", idx.verdict(s))
        self.assertIn("Sampel terlalu sedikit", idx.verdict(idx.stats([win] * 5)))
        self.assertIn("edge", idx.verdict(idx.stats([win] * 20 + [loss] * 20)))
        self.assertIn("Negatif", idx.verdict(idx.stats([win] * 5 + [loss] * 30)))
        self.assertIsNone(idx.stats([win] * 3)["pf"])


class Data(unittest.TestCase):
    def test_chart_json_and_csv_round_trip(self):
        bars = daily(120)
        got = idx.parse_chart(chart_json(bars))
        self.assertEqual(len(got), len(bars))
        self.assertEqual(got[0]["t"], bars[0]["t"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.csv")
            with open(path, "w") as fh:
                fh.write("Date,Open,High,Low,Close,Volume\n")
                for b in bars:
                    fh.write("%s,%s,%s,%s,%s,%d\n" % (b["t"].strftime("%Y-%m-%d"), b["o"], b["h"],
                                                       b["l"], b["c"], b["v"]))
            self.assertEqual([b["c"] for b in idx.parse_csv(path)], [b["c"] for b in bars])
            out = io.StringIO()
            with redirect_stdout(out):
                code = idx.main(["backtest", "TEST", "--csv", path, "--min-value", "0"])
            self.assertEqual(code, 0)
            text = out.getvalue()
            for s in idx.STRATEGIES:
                self.assertIn(s, text)
            self.assertIn("not financial advice", text)


class ORB(unittest.TestCase):
    def test_orb_on_wib_sessions(self):
        day = datetime.datetime(2026, 10, 5, 9, 0)   # a Monday
        bars = []
        # 09:00 to 09:10: the range 100 to 104
        for k, (o, h, l, c) in enumerate(((100, 102, 100, 101), (101, 104, 101, 103), (103, 103, 101, 102))):
            bars.append({"t": day + datetime.timedelta(minutes=5 * k), "o": o, "h": h, "l": l, "c": c, "v": 1})
        bars.append({"t": day + datetime.timedelta(minutes=15), "o": 102, "h": 106, "l": 102, "c": 105, "v": 1})
        bars.append({"t": day + datetime.timedelta(minutes=20), "o": 105, "h": 111, "l": 105, "c": 110, "v": 1})
        r = idx.orb_backtest(bars, 15, 1.0, "low", 0, 0)
        self.assertEqual(r["sessions"], 1)
        t = r["trades"][0]
        self.assertEqual((t["entry_time"], t["entry"], t["stop"], t["target"]), ("09:15", 105, 100, 110))
        self.assertEqual(t["why"], "target")
        self.assertAlmostEqual(t["r"], 1.0)


if __name__ == "__main__":
    unittest.main()
