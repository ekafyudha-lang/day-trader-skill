---
name: trader-firstcandle
description: >-
  First Candle bot. Trades the "first candle rule": one setup a day from the
  first 5-minute candle after the 9:30 New York open. Marks that candle's high
  and low, then on 1-minute candles waits for a break with a fair value gap
  (FVG), a pull back inside the range, and a second close outside on the
  FVG's side (the double break). Entry on that close, stop at the candle that
  first broke the range, target 1:4. Checks today live, watches minute by
  minute, backtests every day Yahoo still has, and writes trader/plan.json
  for /trader-budget. It never places an order. Use when the user says
  "first candle rule", "first 5 minute candle", "candle pertama", "strategi
  satu candle", "bot first candle", "FVG double break", "backtest first
  candle", or asks for a bot for this strategy.
argument-hint: "check|watch|backtest TICKER... [--rr 4] [--reentry wick|close] [--side both|long|short]"
---

# trader-firstcandle

One candle a day. The 9:30 to 9:35 candle sets the whole trade: the levels to
break, the stop and, through the stop, the target. The bot reads free Yahoo
1-minute candles, walks them one at a time, and says where the setup stands:
forming, waiting (and at which step), entry, or expired. It never buys or
sells anything. Day trading loses most people money, and a clean rule does
not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/firstcandle.py" check SPY                    # today, as of now
python3 "${CLAUDE_SKILL_DIR}/firstcandle.py" check SPY --write            # + trader/entry.json, trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/firstcandle.py" watch SPY QQQ --until 11:30  # every minute until an entry
python3 "${CLAUDE_SKILL_DIR}/firstcandle.py" backtest SPY QQQ TSLA        # ~30 days of 1-minute candles
python3 "${CLAUDE_SKILL_DIR}/firstcandle.py" backtest TSLA --interval 5m  # ~60 days, rougher
```

Other flags: `--rr 4` (the target in R), `--reentry wick|close`,
`--side both|long|short`, `--last-entry 15:30`, `--flat-by 15:55`
(backtest), `--json`, `--offline FILE` (a saved Yahoo chart JSON, `.json` or
`.json.gz`, or a folder of `<TICKER>.json`), `--now "2026-11-12 10:20"` (New
York time) to replay a day. `watch` with `--offline` or `--now` replays the
day a minute at a time without waiting.

## The rule

1. **The first candle.** High and low of 9:30 to 9:35 New York time: the
   first 5-minute candle (built from the five 1-minute candles inside it).
2. **The break.** On 1-minute candles, the first candle that CLOSES outside
   the range. Above the high is a long, below the low a short. A wick outside
   is not a break.
3. **The fair value gap.** Before price comes back, the breakout has to leave
   a 3-candle gap: candle 3's low above candle 1's high (long), or candle 3's
   high below candle 1's low (short). The middle candle is the strong one: it
   closes outside the range, in the break's direction. If a candle closes
   back inside before any gap forms, the break is dropped and the next close
   outside starts over.
4. **Back inside.** After the gap, price has to come back into the range. By
   default a wick back to the level is enough (`--reentry wick`); `--reentry
   close` needs a close back inside.
5. **The double break.** A LATER candle closes outside the range again, on
   the gap's side. Its close is the entry; its close time is the entry time
   (the 9:41 candle closes at 9:42 AM).
6. **Stop** at the far end of the break candle from step 2: its low for a
   long, its high for a short. **Target** at 4 times the risk (1:4,
   `--rr`). If the break candle's low is not below the entry, the setup is
   skipped: there is no risk to measure.

- A close on the other side of the range cancels the setup on this side.
- One trade a day: the first side to finish the rule.
- No new entry after `no_entries_after` (config, 15:30). Open trades are
  flat by `flat_by` (config, 15:55).
- Only closed candles count. On live data the newest candle also waits for
  Yahoo to catch up (a minute at most).

## What it reads and writes

| file | what |
| --- | --- |
| Yahoo chart API | `range=1d&interval=1m` for today; a week at a time for the 1-minute backtest (Yahoo keeps about 30 days), `range=60d&interval=5m` for `--interval 5m` |
| `trader/config.json` | `no_entries_after` (15:30) and `flat_by` (15:55). Optional |
| `trader/entry.json` | with `--write` on a long entry: symbol, side, time, price, range, stop, target |
| `trader/plan.json` | with `--write` on a long entry, in `/trader-exit`'s shape (setup `first-candle`, stop rule `break-candle`, target 4R), so `/trader-timemachine`, `/trader-budget`, `/trader-risk` and `/trader-broker` read it |

## Steps

1. Get the tickers. Liquid names with a real open work best (SPY, QQQ, big
   caps). Check `/trader-news` first.
2. Before 9:35 there is nothing to do: the first candle is forming. After
   it, run `firstcandle.py check TICKER` and tell the user the range and the
   step the setup is on, in one or two lines.
3. To wait for the entry, run `firstcandle.py watch TICKER --write` in the
   background (it prints one line each time the stage changes and exits 0 on
   the entry), or `check TICKER --write` on a `/loop` every minute.
4. On an entry, say the side, time, price, stop, target and risk a share,
   and that nothing was placed. If the entry is more than a few minutes old,
   say that: a late entry is a different trade.
5. A long goes on: `/trader-budget` sizes it to the user's max loss,
   `/trader-risk` checks it, `/trader-broker` shows the ticket and waits for
   the user's yes. The config's `no_entries_before` is 09:45 by default; this
   rule can fire from 9:37, so tell the user to lower it to 09:35 in
   `trader/config.json` if they want those trades to pass the risk check.
6. A short is information only: this pack sizes and sends longs only
   (`allow_short` is false). Say so.
7. Before anyone trades it with money, run `backtest` on their tickers and
   show the table and the summary. Say plainly how many trades hit the
   target, how many the stop, and the total in R.

## Backtest

`backtest` runs the same rule on every finished session Yahoo still has,
then walks the candles after each entry: the stop, the target, or flat at
`--flat-by`. A candle that touches both the stop and the target counts as
the stop (inside one candle the order is unknown). A candle that opens past
the stop fills at its open. No slippage, no commissions. Results are in R
(multiples of the risk), so a 1:4 target is +4R and a stop is -1R.

`--interval 5m` runs it on 5-minute candles over about 60 days: the first
candle is then one candle, and the gap and the double break are 5-minute
patterns. It is a rougher version of the rule; say so when you show it.

Example, on the repo's real TSLA 5-minute fixture (2026-07-27 to
2026-10-16, `--interval 5m`): 59 sessions, 36 trades, 4 targets, 22 stops,
10 flat at the close, win rate 27.8 %, total -5.64R. With a 1:4 target the
rule needs better than one win in five to break even; on these bars it did
not get there. That is the point of testing before you risk a dollar.

## Rules

- Candle by candle, on free Yahoo candles. They are unofficial and can lag.
  Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money. Past results do not repeat on demand.
- Never call an entry before the double-break candle has closed.
- Never move the stop or the target after the fact.

## What it never does

It never places an order, never sizes a position (that is `/trader-budget`),
never trades a short through the pack, and never runs `/trader-broker` from a
background watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
