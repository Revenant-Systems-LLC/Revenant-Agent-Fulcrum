# Revenant Agent Fulcrum

## The two-minute version

Fulcrum is a program that practices trading a few large cryptocurrencies (Bitcoin, Ethereum, Solana, and up to five others) with pretend money on Alpaca's paper-trading account. It follows a fixed set of rules that are written down in this repo and can be read by anyone. It runs on your machine only when you start it, and it stops when you stop it.

**What it does**
- Once a week it looks at recent prices and decides, by rule, how much of each coin to hold. Coins that have been going up get held; coins that have not get sold or skipped.
- It sizes every position so that a bad week costs about the same in each coin, and it never puts more than a set fraction of the account into one coin or into coins overall.
- Every position has a stop: a price at which it will be sold no matter what. The stop can move up, never down.
- Every 15 minutes it checks prices against those stops and checks the whole account against a safety brake (the kill switch). If the account falls 15% from its best point, the brake trips: no new buys until you personally reset it.
- Before a buy, it asks an AI one question: "is there a concrete reason not to buy this right now?" The AI can say no to a buy. It cannot pick a coin, an amount, a price, a time, or a sale. For the first week it only records its answers and blocks nothing (shadow mode), so you can judge whether its reasons are sane before letting it act.
- It writes everything it does to a ledger file and sends you a short Telegram message when it trades or when something needs your attention.

**What it never does**
- It never touches real money. The code refuses to connect to anything but Alpaca's paper endpoint, and there is no setting to change that.
- It never touches the old Fulcrum's account, files, or credentials.
- It never runs in the background on its own. You start it from the menu, and it stops when you close it.
- The AI never places, sizes, or times a trade, and it never sees the stops or the kill switch.

**How to use it**
Double-click `Start-Fulcrum.ps1` (or right-click, Run with PowerShell). You get a numbered menu. Start with 1 (Check setup), then 2 (Practice cycle). The rest of the steps, in order, are in `docs/HANDOFF.md`.

## Glossary

- **Paper trading**: trading with pretend money on a broker's practice account. Real prices, no real risk.
- **Rebalance**: the weekly decision round where targets are recomputed and trades are made.
- **Conviction**: a score from 0 to 1 for how strongly a coin has been trending up. 0 means "do not hold", 1 means "hold the full target".
- **Stop**: a price below which a position is sold automatically. Fulcrum's stops only ratchet upward.
- **Drawdown**: how far the account has fallen from its highest value so far. A 15% drawdown means the account is 15% below its best day.
- **Kill switch**: the safety brake. Trips at a 15% drawdown. Stops still work; no new buys. Only you can reset it, by creating a file by hand (the program has no code that can create it).
- **Shadow mode**: the AI filter records what it would have vetoed but blocks nothing. **Active mode**: its vetoes actually block buys.
- **Ghost book**: a second, pretend account that tracks what would have happened if the AI's vetoes had been the other way round. It is how we measure whether the filter helps, over months, not by feel.
- **Fee drag**: how much trading fees cost per year as a share of the account. Fulcrum alarms if this passes 3%.
- **Sharpe**: return per unit of risk. Above 1.0 over 90 days is the bar this project set before anyone discusses real money.

## The contract, for anyone reading the code

RULES DECIDE, SIZE, AND EXECUTE. THE LLM MAY ONLY VETO. If any code path lets the LLM choose a size, a price, a symbol, a time, or an exit, it is a bug of the highest severity. The filter receives entry and increase candidates only (`fulcrum/llm_filter.py`), every order passes through one function with no bypass parameter (`fulcrum/risk.py::check_order`), and `AlpacaBroker` asserts the paper URL as a constant (`fulcrum/alpaca.py`).

## Commands

Everything below is what the menu runs for you. From the repo root, with the venv:

| Menu | Command | What it does |
|---|---|---|
| 1 | `python -m fulcrum selftest` | Loads config, opens the ledger, exercises every module against fakes, shows what the AI filter would be sent. Sends nothing anywhere. |
| 2 | `python -m fulcrum run --once --dry-run` | One decision round with pretend fills on real prices. No order leaves the machine. |
| 3 | `python -m fulcrum run` | The loop: weekly rebalance, 15-minute stop and kill-switch checks. Uses the paper account only when `config.toml` says `mode = "paper"`. |
| 4 | `python -m fulcrum status` | Holdings, cash, drawdown, kill switch, filter mode, last actions, in plain sentences. |
| 5 | `python -m fulcrum report` | Performance so far: return, drawdown, fees, filter verdicts, ghost book comparison. |
| 6 | `python -m fulcrum backtest` | The history test described in `docs/BACKTEST.md`. |

## The truth about the shadow week

At a weekly rebalance, seven days produces about one rebalance and a handful of verdicts. That validates plumbing, JSON reliability, latency, cost, and whether the veto reasons are sane. It proves nothing about edge. To make the week more informative, in shadow mode the filter also runs once a day against that day's hypothetical candidate list (logged, never traded). The ghost book is what eventually answers the edge question, and it will take months, not a week.

## Go-live thresholds (recorded, not acted on)

From the research report: at least 90 days of paper trading across a trending and a choppy stretch, net Sharpe at or above 1.0, max drawdown at or below 20%. Going live would be a deliberate code change reviewed by Dave, not a setting; there is no live mode in this codebase.

## Known biases and limits

- The coin list is today's Alpaca listing, so any history test is survivor-biased.
- The sample is short, and crypto trend-following has severe crash risk; the volatility scaling and the kill switch reduce it, they do not remove it.
- Backtest history comes from Coinbase (USD) with Binance bulk files (USDT) as a cross-check; paper trading happens on Alpaca. Three venues, three price series.
- Paper trading may or may not simulate Alpaca's crypto fees; Fulcrum models fees itself (0.25% taker) so results are honest either way.
- The research report's "March 2026 stress window" does not match primary sources; the crash was February 2026. See `docs/DECISIONS.md` D-012.

## Where things are

- `docs/PLAN.md`: the build plan Dave approved.
- `docs/DECISIONS.md`: every judgment call and verified fact, with sources and dates.
- `docs/BACKTEST.md`: the honest history test.
- `docs/HANDOFF.md`: the steps only Dave can do, in order.
- `docs/research/Fulcrum_Report.md`: the research this design rests on.
- `config.example.toml`: every number the strategy uses, with comments. Copy to `config.toml` to run.
- `data/fulcrum.sqlite`: the ledger (created on first run, never committed).
