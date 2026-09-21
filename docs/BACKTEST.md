# The history test (backtest), run 2026-09-21

Every number here came out of `python -m fulcrum backtest` and `python -m fulcrum backtest --folds` run on this machine on 2026-09-21 against real Coinbase Exchange daily prices, with the settings in `config.example.toml` exactly as shipped. Nothing was tuned. The exact commands to reproduce are at the bottom.

## The verdict, in plain words

The rules are a cautious strategy. From 2023 to now they turned $100,000 into about $146,800 (up 46.8%) with a worst fall of 12.6% from a peak, while just holding Bitcoin turned the same money into $518,800 (up 418.8%) with a worst fall of 53.1%. Measured as return per unit of risk (Sharpe), the rules scored 1.11 and Bitcoin scored 1.18. That is the number the project pre-registered as the test, and on it the rules came in slightly below simply holding Bitcoin. The other half of the bar, a Sharpe of at least about 0.5, was met comfortably.

So the honest one-line answer is: **the engine works, the strategy as configured did not beat holding Bitcoin on a risk-adjusted basis over 2023 to 2026, and it was not tuned to make that look better.** Dave decides what to do with that.

What the rules did well: they lost very little in bad stretches. In the February to March 2026 crash they gave up 3.4% while Bitcoin gave up 11.3%. In the flat, choppy autumn of 2023 they lost 1.5% while Bitcoin lost 8.5%. Over the longer 2020 to 2026 window, which includes the 2022 bear market, the rules' Sharpe of 1.38 beat Bitcoin's 0.92, with a worst fall of 14.5% against Bitcoin's 76.7%.

What they did badly: they barely participated in the rallies. In the April to October 2025 climb the rules made 8.0% while Bitcoin made 57.7%. The reason is visible in the numbers: the account was, on average, only about 13% invested. The 20% portfolio volatility target, split equally across four coins, gives each coin a budget of 5% volatility, and crypto runs at 50% to 60%, so even at full conviction a coin gets a weight near 9%. The per-asset cap of 15% and the gross cap of 60% never bound; the volatility target did, every week. That is a measurement, not a recommendation: the defaults are the prompt author's starting values (DECISIONS D-001) and changing them is Dave's call, to be made before any more paper trading, not after seeing which value would have looked best.

Fees were not the problem: 0.96% of the account per year, well under the 3% alarm. Trades were few: 322 fills in three and three quarter years, about one and a half a week.

## The numbers

Universe after the liquidity screen (median daily dollar volume above $50 million over the last 30 days of the run): BTC/USD, ETH/USD, SOL/USD, XRP/USD. Rejected by the screen with their measured medians: LINK ($19.3M), DOGE ($17.7M), UNI ($17.4M), ADA ($14.9M), LTC ($7.0M), AVAX ($5.7M), DOT ($4.2M).

All runs: $100,000 start, 25 bps taker fee plus 10 bps slippage on every fill, fills at the next day's open, stops at min(open, stop) when the day's low touches the stop, 2% no-trade band, weekly rebalance on Monday (UTC), same settings in every period.

| Run | Net Sharpe | Max drawdown | CAGR | Total return | Avg exposure | Turnover / yr | Fee drag / yr | Avg win / avg loss | Win rate | Worst week | Fills | Round trips |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Whole period 2023-01-01 to 2026-09-21: rules** | **1.11** | **12.55%** | 10.86% | 46.78% | 13.00% | 3.85 | 0.96% | 2.23 | 48.7% | -4.38% | 322 | 154 |
| Same period: BTC buy and hold | 1.18 | 53.09% | 55.61% | 418.81% | 100% | 0 | 0 | | | -25.72% | | |
| Same period: equal-weight buy and hold | 0.96 | 68.68% | 52.66% | 383.16% | 100% | 0 | 0 | | | -31.73% | | |
| Same period: cash | n/a | 0% | 0% | 0% | 0% | 0 | 0 | | | 0% | | |
| **Stress 2026-02-01 to 2026-03-31: rules** | -5.16 | 3.45% | -19.57% | -3.40% | 3.78% | 3.82 | 0.95% | n/a | 0% | -2.24% | 13 | 6 |
| Stress: BTC buy and hold | -0.79 | 20.23% | -53.01% | -11.31% | 100% | | | | | -10.91% | | |
| Stress: equal-weight buy and hold | -0.74 | 23.41% | -58.65% | -13.09% | 100% | | | | | -12.49% | | |
| **Trending 2025-04-07 to 2025-10-06: rules** | 1.55 | 4.86% | 16.78% | 8.04% | 15.54% | 4.21 | 1.05% | 2.44 | 64.3% | -2.77% | 54 | 28 |
| Trending: BTC buy and hold | 2.78 | 12.27% | 149.43% | 57.74% | 100% | | | | | -7.38% | | |
| Trending: equal-weight buy and hold | 2.58 | 22.37% | 315.50% | 103.44% | 100% | | | | | -15.94% | | |
| **Choppy 2023-08-01 to 2023-10-15: rules** | -3.72 | 1.91% | -7.22% | -1.53% | 2.97% | 1.80 | 0.45% | n/a | 0% | -0.94% | 8 | 2 |
| Choppy: BTC buy and hold | -1.17 | 15.55% | -35.02% | -8.48% | 100% | | | | | -11.43% | | |
| Choppy: equal-weight buy and hold | -1.34 | 20.33% | -44.58% | -11.42% | 100% | | | | | -12.35% | | |
| **Long window 2020-01-01 to 2026-09-21: rules** | 1.38 | 14.51% | 14.49% | 148.46% | 11.36% | 3.08 | 0.77% | 2.27 | 53.7% | -4.38% | 464 | 231 |
| Long window: BTC buy and hold | 0.92 | 76.69% | 44.71% | 1100.84% | 100% | | | | | -46.54% | | |
| Long window: equal-weight buy and hold | 0.84 | 82.93% | 41.47% | 931.45% | 100% | | | | | -50.64% | | |

How to read the columns: Sharpe is daily returns, annualized with the square root of 365, cash rate 0, and is not reported below 10 daily returns. CAGR annualizes with 365 over elapsed calendar days, so short folds show extreme annualized figures (a 3.4% loss over 58 days reads as -19.6% a year). Avg exposure is the mean share of the account invested. Turnover is dollars traded per dollar of average equity per year. Fee drag is fees per year as a share of average equity. Avg win / avg loss is the FIFO round-trip magnitude ratio; a 48.7% win rate with a 2.23 ratio is a profitable book. Stress and choppy folds closed too few round trips for a ratio (6 and 2), so those cells are n/a and the 0% win rates there are two and six losing trades, not a pattern.

## What the numbers do not tell you

- **Sharpe alone hides the drawdown difference.** The rules' worst fall was a quarter of Bitcoin's (12.6% against 53.1%). The pre-registered test was Sharpe and the rules lost it narrowly; a reader who weighs drawdown more heavily will read the same table differently. Both readings are honest; only one was pre-registered.
- **The long window is not a clean test.** Coinbase listed SOL in June 2021 and did not trade XRP between January 2021 and July 2023, so for the first part of that window the universe was two coins, then three. It is shown because it is the only window that includes a full bear market (2022), and in it the rules did better than holding Bitcoin on both risk and return per risk. Treat it as supporting evidence, not the headline.
- **Missing days.** XRP/USD had absent days on Coinbase inside the 2023 window (the delisting gap and its edges). Absent days were left absent, never invented, and the loader reports them; the signal for XRP simply saw fewer bars.
- **The universe is survivor-biased.** These are today's Alpaca listings. Coins that were listed in 2023 and later failed are not in the test, so every result here, including the benchmarks, flatters itself.
- **The sample is short** (three and three quarter years for the headline) and covers one full cycle at most.
- **Crypto trend-following has severe crash risk.** A calm stretch will not show it. The 2022 bear is in the long window only.
- **Three venues, three price series.** History is Coinbase USD (no fallback was needed; every symbol came from Coinbase). Paper trading will happen on Alpaca, whose prices, fees and bar boundaries differ.
- **The stress window label.** The research report calls it the "March 2026 stress window"; primary sources put the crash in February 2026 (DECISIONS D-012), which is why the fold runs 1 February to 31 March.
- **Ghost book warnings in the ledger.** With the AI filter off the ghost book mirrors the real book, and a few dozen of its mirrored orders were refused by its own pretend broker (recorded as `ghost_order_failed` alerts, 60 in the whole period). This does not touch the strategy's numbers, which come from the real book's ledger, but it means the ghost book's counterfactual drifts from the real book over long runs and needs a look before the shadow week is judged on it. It is listed as a follow-up.

## Methodology, exactly

- Data: Coinbase Exchange public daily candles, `GET /products/{BTC-USD}/candles?granularity=86400`, key-free, 300 candles per request, fetched once and cached under `data/bars/` as one CSV per symbol (about 1,300 rows each for the 2023 window; the long window fetched back to 2020 for the coins that existed). Binance Vision was configured as the fallback and was not needed. Bar timestamps observed at 00:00 UTC, one per calendar day.
- Warm-up: 55 extra days of history before each window so the 50-day average and the 30-day volatility are defined on the first rebalance day.
- No lookahead: the engine sees bars through the close of day t; every fill uses day t+1's open; a test perturbs every bar after day t and asserts nothing before t changes.
- Stops: checked against each day's low; filled at min(open, stop) on a gap; a stop is never tested against the low of its own entry bar.
- Costs: 25 bps taker fee (Alpaca tier 1, D-009) plus 10 bps slippage against the trader on every fill, for the rules and for the benchmarks alike. Benchmarks buy on the first day's open and hold.
- Risk layer identical to live: 5% per-order clamp, 15% per asset, 60% gross, 10 trades per 24 hours counted by the simulated clock, 15% kill switch (it never tripped in any run).
- AI filter: off. This test measures the rules only.

## Reproduce

From the repo root with the venv:

```
.\venv\Scripts\python.exe -m fulcrum backtest
.\venv\Scripts\python.exe -m fulcrum backtest --folds
```

Each run writes fresh ledgers under `data\` (never overwriting a previous one) and reads prices from the cache when it has them. The tables above were extracted from those ledgers with `fulcrum.report.metrics`.
