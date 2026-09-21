# Redesigning Fulcrum Finder: A Full Research Report on Agent-Aware Trading Venues, Copy-Trading Evidence, and a Buildable Strategy

## TL;DR
- **Do not build Fulcrum around blindly copying whale wallets.** The best available evidence — a 90-day, 100,236-outcome study across Binance, Bybit and MEXC (YieldFund, Nov 2025) — shows only 48.48% of copiers finished profitable, and while 97.04% of lead traders were personally profitable, only 43.61% produced positive PnL for the people copying them. Copying is close to a coin flip and is degraded further by latency, slippage, MEV and survivorship bias.
- **Build Fulcrum as a rules-based systematic bot with an LLM as a filter, not an executor**, on Alpaca crypto paper trading first. The strategies with real out-of-sample evidence for a small US retail account are time-series/trend momentum (Liu & Tsyvinski) and, if you later add a hedged venue, funding-rate/cash-and-carry carry — not the 5%/5% grid, which structurally bleeds in trends.
- **US geography is the binding constraint.** Hyperliquid, Bybit, Binance.com and MEXC all bar US persons in their terms; their vaults and copy products are off-limits to you. Your realistic agent-aware, US-legal venues are Alpaca, Coinbase (Advanced + Coinbase for Agents), Kraken and Kalshi. Design for those.

## Key Findings

1. **Copy-trading is roughly a coin flip and structurally worse for the copier than the leader.** YieldFund's November 2025 study: 48,592 winning vs 51,644 losing outcomes (48.48% profitable) across 100,236 cases; only 43.61% of leaders made their followers money despite 97.04% being personally green. Win rate misleads: leaders could show >57% winning trades and still lose money for followers because average loss exceeded average win.

2. **The LLM-trading evidence says the same thing: models mostly lose, and the winners trade least.** In Nof1's Alpha Arena Season 1 (six LLMs, $10k each, real capital on Hyperliquid, Oct 18–Nov 3 2025), only 2 of 6 finished positive (Qwen3 Max +22.31%, DeepSeek +4.89%); GPT-5 finished last at roughly −62%. Nof1's own diagnosis: "PnL was dominated by trading costs in early runs as agents over-traded and took quick, tiny gains that fees erased." Gemini placed 238 trades and lost heavily; the profitable models traded ~40 times.

3. **Your 5%/5% grid loses for a known, structural reason.** A symmetric buy-the-dip/sell-the-rip grid is implicitly short volatility and short trend: it accumulates losing inventory as price trends away from the anchor and has no stop or exit logic, so a sustained trend produces unbounded mark-to-market losses. This is the documented failure mode of grid/martingale-style copy strategies.

4. **The strategy with the deepest academic support that runs on Alpaca paper crypto is time-series momentum / trend.** Liu & Tsyvinski (Review of Financial Studies 2021, Vol. 34, Issue 6, pp. 2689–2727) establish a strong 1–4 week time-series momentum effect in crypto; the follow-up Liu, Tsyvinski & Wu (2022) three-factor model (market, size, momentum) explains the cross-section, and — using 1,827 coins over 2014–July 2020 — a long/short momentum strategy produced roughly 3% excess weekly returns (as quantified by Grobys et al., Financial Markets & Portfolio Management, Springer 2025). A crypto "trend factor" (Cambridge JFQA, 2024) is robust across 55,296 implementations. Caveat: momentum crashes are severe, so volatility scaling is essential.

5. **Funding-rate / cash-and-carry carry is the highest Sharpe market-neutral trade, but needs a perp venue you can't legally reach from the US, and yields have thinned.** 1Token's institutional research puts the typical funding-fee gross return at "a range of 10-30% annualized" with a Sharpe/Calmar of 5–10; but an honest open-source backtest (zwmjj/funding-rate-arb) shows most PnL came before 2022, and against a USD cash rate of ~4–5% through 2023–2025 the 2025 regime at 4.3% annualized was "approximately zero excess return."

6. **US-legal, agent-aware venues exist and are enough to build on.** Coinbase launched "Coinbase for Agents" (MCP + CLI) on June 11, 2026, registered as an RIA with the SEC and a CTA with the NFA. Alpaca's official MCP server (65 tools) supports crypto and defaults to paper trading. These, plus Kraken and the CFTC-regulated Kalshi, are your realistic surface.

## Details

### Section 1 — Inventory of Agent-Accessible Trading Venues (September 2026)

**Legend:** "Agent access" = official MCP/CLI/skill or robust API; "Positions public" = other traders' live positions visible (needed to copy); "US" = whether US persons may use it per ToS.

| Venue | Type | Official API | MCP/CLI/agent | Paper/testnet | Positions public? | Native copy/vault + fee | Trading fees | US availability |
|---|---|---|---|---|---|---|---|---|
| **Alpaca** | US broker (crypto/stocks/options) | Yes | Official MCP server (65 tools), FastMCP/OpenAPI | Yes — $100k paper, real-time data | No | No | Crypto: per-trade maker/taker (volume-tiered CFEE/FEE) | **Yes (US-based)** |
| **Coinbase** | US exchange | Yes (Advanced Trade + CDP) | "Coinbase for Agents" MCP + CLI (June 11 2026); Base MCP | Sandbox; no true paper on Advanced | No | No native copy | Advanced (US): 0.50% maker / 0.90% taker entry tier (Sept 16 2026 restructure), scaling down with volume | **Yes** |
| **Kraken** | US exchange | Yes | Community CLI/skills, API | No native paper | No | No native copy | Pro maker/taker tiered from ~0.25%/0.40% | **Yes** (some products limited) |
| **Robinhood** | US broker | Limited | Agent features introduced ~June 2026 (days before Coinbase) | No | No | No | Commission-free crypto (spread) | **Yes** |
| **IBKR** | US broker | Yes | Community MCP | Paper trading yes | No | No | Low tiered | **Yes** |
| **Binance.com** | Offshore CEX | Yes | Community | Testnet | Only via copy product | Copy trading; futures 0.02%/0.04% base | Low | **No — US persons excluded** |
| **Bybit** | Offshore CEX | Yes | Community | Testnet | Via copy product | Copy trading (profit share); classic weekly settle | Low | **No — §11.3 excludes US, all 50 states** |
| **MEXC** | Offshore CEX | Yes | Community | Testnet | Via copy product | Copy trading | Low | **No (US excluded)** |
| **OKX / Bitget / KuCoin / Crypto.com / Gemini** | CEX | Yes | Mostly community | Some testnet | Via copy products | Bitget copy: leader takes 8–20% of follower profit (loss not shared); KuCoin, OKX similar | Low; Bitget spot ~0.1% | Bitget/OKX/KuCoin **restrict US**; Gemini & Crypto.com **US-available** but no agent-native copy |
| **Hyperliquid** | Perp DEX | Yes (info API, public leaderboard) | Community MCP servers | No | **Yes — fully public positions** | User vaults + HLP; leader takes ~10% of profit; HLP no perf fee | ~0.02%/0.05% | **No — US persons are Restricted Persons; frontend geoblocks US IPs** |
| **Aster** | Perp DEX | Yes | Community | — | Partly (hidden orders available) | — | Low | Restricts US; no KYC on-chain |
| **dYdX** | Perp DEX (Cosmos) | Yes | Community | Yes (testnet) | Order book public, not per-trader ID | No native copy | Low | Opened SOL spot to US Dec 2025; perps historically restricted |
| **GMX / Lighter / Drift / Jupiter / Vertex / Aevo** | Perp DEX | Yes | Community | Some | Pool-based; GMX GLP, Jupiter JLP vaults | Pool vaults (GLP/JLP) | Low | Mostly restrict US persons for perps |
| **GMGN / Photon / BullX / Trojan / Axiom / Banana Gun / Maestro** | Solana on-chain terminals | Partial | GMGN copytrade; API partial | No | Wallets public on-chain | Copytrade built-in; ~0.5–1% platform fee/trade | 0.5–1% + gas + priority + slippage (round-trip 3.2–6.6% real) | Not geofenced (self-custody) but **high scam/rug risk; US legal gray area** |
| **Polymarket** | Prediction market | Yes (CLOB on Polygon) | Community; data API | — | Wallet positions public | Copy tools exist | Low | **Historically restricted for US; ICE invested $2B; verify** |
| **Kalshi** | Prediction market | Yes (REST + FIX) | Community | Demo | Aggregate only | No | Contract-based | **Yes — CFTC-regulated US exchange** |

**Notes on the inventory:**
- **Bybit ToS is explicit:** §11.3 lists the United States (all 50 states and territories) among Excluded Jurisdictions and prohibits residency misrepresentation and VPN circumvention. Bybit holds no CFTC/FinCEN/state MTL licenses to serve US persons.
- **Hyperliquid ToS** designates US persons as Restricted Persons; the app.hyperliquid.xyz frontend geoblocks US IPs; the terms forbid VPN workarounds. The underlying L1 does not IP-block, but using it as a US person violates the interface terms and the leveraged-perps offering is unregistered in the US. As of an Aug 19 2026 White House meeting, President Trump said CFTC Chair Mike Selig was working to bring Hyperliquid into the US "in a fully compliant and legal fashion," but no filing, registration or date has been announced.
- **Coinbase for Agents** ships as both an MCP (single-login for ChatGPT/Claude web) and a CLI+skill (for Claude Code/Codex). It expanded to stocks Aug 31 2026. It is the only agent-native venue that is also a US-registered RIA/CTA.

### Section 2 — Copy-Trading and Whale-Following: The Evidence

**Industry / exchange data (VERIFIED):**
- **YieldFund 90-day multi-exchange study (Nov 2025):** 100,236 outcomes; 48.48% of copiers profitable; 43.61% of leaders produced positive follower PnL despite 97.04% personally profitable; aggregate follower PnL +709,045 USDT (i.e., positive in aggregate but driven by a few leaders). Platform mattered enormously: Binance followers 66.5% win rate vs Bybit 43.65%.
- Arkham's research desk independently cites the YieldFund finding (97% leaders profitable, ~43% of followers profitable) and attributes the gap to execution latency, entry/exit price deviation, and MEV/sandwich attacks.

**Academic literature (VERIFIED):**
- **Pan, Altshuler & Pentland (MIT, 2012):** "guided copying" traders fared 6–10% better than manual traders and 4% better than random copiers — the pro-copy datapoint, but from an early, curated eToro setting.
- **Pelster & Hofmann (2018):** across ~150 million eToro trades (about three-quarters of which are copy trades), losses are typically higher for copied trades on negative returns, and copied traders suffer more from the disposition effect.
- **Apesteguia, Oechssler & Weidenholzer (Management Science, 2020):** copy trading increases strategy volatility and market overreaction; copied trades have a higher probability of positive returns but a lower ROI on successful trades, and larger losses on negative ones.
- **Kogan et al. (2024):** copy-trading users follow momentum in crypto and display stronger co-movement than in stocks — i.e., copying amplifies crowding.
- Weak-outperformance evidence: a Tallinn thesis on eToro copy-portfolios found 19 of 28 alpha estimates were not statistically different from zero — the statistical case for copy-portfolio outperformance is weak.

**On-chain whale copying (VERIFIED failure mechanisms):**
- **Latency & front-running:** memecoin degen wallets make 10 trades/minute; by the time your copy executes, the leader has already sold into your buy. Galaxy Digital data (via MEXC) put the median Solana memecoin hold time at ~100 seconds.
- **MEV/sandwiching:** the academic measurement study "Quantifying the Threat of Sandwiching MEV on Jito" (Weintraub et al.) found the median sandwiched Solana transaction lost roughly $5.6, with some over $100; shattered.io's 2026 MEV analysis (citing EigenPhi/Cointelegraph data) reports most sandwiches shave 0.3%–0.8% off a swap, with the current average take about $3 per attack. The percentage bite is worst on high-slippage memecoin trades.
- **Survivorship bias:** leaderboards are survivor lists by construction — they show who's still standing, not who tried and got liquidated.
- **Concentration/insider dynamics:** the YZY celebrity token launch put 51,800+ wallets in loss (~$74M) within 24 hours; the first 13 wallets made ~$24.5M before the crash. Even a wallet linked to Andrew Tate lost ~$699k shorting it (36.25% win rate on 80 trades).

**Bottom line for Section 2:** copying works in curated academic settings and in aggregate only because a handful of leaders carry the average; for an individual copier it is roughly a coin flip before the specific decay of latency, slippage and MEV, which are worst exactly on the high-return on-chain venues.

### Section 3 — Hyperliquid Vaults and "Share the PnL" Mechanisms

**HLP (Hyperliquidity Provider vault) — VERIFIED data:**
- Live TVL around $186.5M (Hyperliquid app snapshot) after peaking above $500M; TVL has fallen roughly two-thirds from peak, and had a trailing 30-day TVL drop of 25.7% in one Aug 2026 reading.
- Returns: DefiLlama-based estimates put HLP at ~15–30% APR across most quarterly windows; a Medium analysis cites ~1.75%/month average; a recent app reading showed 4% past-month APR (annualized). It has distributed roughly $137M cumulatively and reported a 5.68% front-end max drawdown, with real drawdowns during fast directional moves (notably March 2024's BTC rally).
- Mechanics: HLP is a market-making/liquidation-backstop vault — depositors are effectively short-vol, taking the other side of the venue's traders. USDC-only, no performance fee, 4-day lockup.

**User vaults — VERIFIED:**
- 350+ vaults with $100M+ TVL beyond HLP; one analytics site lists 2,000+ vaults. Leaders take ~10% of profits; 24-hour lockup. Examples reviewed Sept 3 2026: "KCR" (BTC trend-following targeting 3x/yr) had negative cumulative PnL and cross-margin exposure; "SIM Funds" held ~95% of TVL in a single AVAX position. This illustrates that headline APR hides concentration, leverage and short track records.

**Mechanical difference vs external copy bots:** a vault depositor holds a pro-rata share of one on-chain account, so there is zero mirroring latency and zero per-follower slippage — you get the leader's exact fills. That removes the single biggest copy-bot failure mode. The trade-offs are lock-ups, operator/rug risk, and that you take the leader's full drawdown.

**US legality:** Hyperliquid's ToS makes US persons Restricted Persons and the frontend geoblocks US IPs. **A US person cannot legally/technically use HLP or user vaults through the official interface.** This is the decisive fact for Fulcrum.

**Comparators:** Bitget/Bybit/Binance copy products charge the leader 8–20% of follower profit (loss not shared) but exclude US persons (except where separately licensed). On-chain funds like dHEDGE/Enzyme and GMX GLP / Jupiter JLP are pool-based vaults; GLP/JLP expose you to a token basket (price risk), unlike HLP's USDC-only design. None of the offshore CEX copy products is US-legal.

### Section 4 — Systematic Strategies With Real Out-of-Sample Evidence

| Strategy | Best evidence | Reported performance | Post-fee realism (small US retail) | Runs on Alpaca paper crypto? |
|---|---|---|---|---|
| **Time-series (trend) momentum** | Liu & Tsyvinski 2021 (RFS); Liu/Tsyvinski/Wu 2022; Moskowitz-Ooi-Pedersen lineage | ~3% excess weekly return in a long/short momentum test (1,827 coins, 2014–2020, Grobys et al. 2025); a "Papers With Backtest" implementation shows ~18% annual, Sharpe 0.63, 31% max drawdown (2024–2026) | Realistic but drawdowns severe; needs vol-scaling; low turnover keeps fees manageable | **Yes** |
| **Cross-sectional momentum** | Liu, Tsyvinski & Wu 2022; crypto trend factor (JFQA 2024) | Trend factor robust across 55,296 implementations; positive momentum payoff, diminishes with market cap | Needs a universe of coins; feasible on Alpaca's larger coin set | **Yes (spot only)** |
| **Mean-reversion / grid** | Widely documented failure mode | Works in ranges, bleeds in trends; martingale blows up on sustained moves | Poor without a regime filter and hard stops — this is Fulcrum's current problem | Yes, but not recommended as-is |
| **Funding-rate / cash-and-carry** | 1Token; Springer Digital Finance 2026; arXiv 2510.14435 | 1Token: gross 10–30%/yr, Sharpe/Calmar 5–10; crypto-carry mean ~8% with 0.8% vol (arXiv 2510.14435); honest open-source backtest 9–11% gross, ~zero excess vs cash rate in 2025 | Thinned post-2022; netting USD cash rate takes recent years near zero; needs perp venue | **No — needs a perp venue (US-restricted)** |
| **Market making** | HLP and CEX MM data | Short-vol, small steady gains + fat tails | Requires low latency and rebates; hard for retail | Partially (limit orders) |
| **Volatility-scaled sizing** | Cryptocurrency momentum crash literature (Springer 2025) | Reduces momentum crash severity | Strongly recommended as an overlay on any of the above | **Yes (as an overlay)** |

**Why the 5%/5% grid fails (explicit):** it is symmetric and unconditional, so in a trend it keeps buying a falling asset (or selling a rising one) with no invalidation level; it is implicitly short-vol/short-trend and has unbounded downside without a stop. The fix is not to tune the 5% band — it is to add (a) a trend/regime filter that disables mean-reversion when trend is strong, (b) hard stops/invalidation levels, and (c) volatility-scaled position sizing.

**On the funding-arb Sharpe numbers:** treat the eye-popping figures skeptically. A ScienceDirect study ("Exploring risk and return profiles of funding rate arbitrage on CEX and DEX") found DEX venues Drift and ApolloX yielded Sharpe 23.55 and 6.50 versus HODL's 2.89 — but the CEX versions were *negative* (−7.34 Binance, −7.93 BitMEX), and the authors caution the elevated ratios stem from a relatively short observation period. A separate arXiv paper reported a model-implied Sharpe of ~38.82 under stated synthetic-DEX cost assumptions, explicitly "not a directly realized live-trading return."

### Section 5 — LLM-Agent Trading Evidence

**Nof1 Alpha Arena (VERIFIED):**
- **Season 1** (Oct 18–Nov 3 2025, $10k each, Hyperliquid perps): Qwen3 Max +22.31% ($12,231), DeepSeek +4.89% ($10,489), Claude Sonnet 4.5 ~−42% ($5,799), Gemini 2.5 Pro ~−45.6% ($5,445), Grok 4 ~−58% ($4,208), GPT-5 last ($4,126). Four of six lost heavily.
- **Season 1.5** ran US equities and ended Dec 3 2025. As of checks through Aug 2026, nof1.ai still showed Season 1.5 as the latest completed run; no Season 2 roster/leaderboard had been posted. Nof1 raised $15M in May 2026 (co-led by SUI Group and Karatage) to build a consumer product.

**What Nof1 said failed (VERIFIED, primary + Bloomberg):**
- Overtrading and fee drag: Nof1's blog (TechPost1, quoted by Protos) states "PnL was dominated by trading costs in early runs as agents over-traded and took quick, tiny gains that fees erased." Trade counts diverged wildly (Gemini 238 vs Claude 38 vs Qwen 43 vs DeepSeek 41), and the biggest turnover cohort had the biggest fee drag (Qwen paid ~$1,654 fees, Gemini ~$1,331 — 13–17% of the $10k base).
- Founder Jay Azhang (Bloomberg, via Business Standard): "LLMs can't really make money by themselves. You need basically a very sophisticated harness and scaffolding and data platform in order to even give them a chance." Models "tend to mistime their trades, incorrectly size positions and buy and sell too often." "Giving an LLM money right now and just having it go — that's not a thing yet."
- Persistent directional biases ("personalities"): models showed consistent long/short leans and different sizing; Datawallet's on-chain read described GPT-5 opening "simultaneous positions across all assets with leverage above 17x" and holding losers "until invalidation instead of reducing early." Not uniform herding but time-varying inter-model correlations Nof1 is still analyzing.
- Quantitative-reasoning weakness: Nof1 notes models must "parse noisy market features … reason under strict rules, and return a structured action, all inside a limited context window," and documented self-referential confusion and rule-gaming in harness tests.

**Other benchmarks (VERIFIED):**
- **TradeRank** (live simulated LLM arena, 56 models across 9 seasons since Jan 2026; 2,826 trades, $910k simulated): only 46.2% of model-seasons profitable. A key finding: in one falling-market season, of 156 directional opens by 8 base agents, 151 were longs into a falling market — herding into the wrong direction; only three finished positive and all three were contrarian "reverse" agents. "The busiest model rarely wins."
- **StrategyArena:** ~72 simulated strategies across BTC/ETH/SOL/BNB/XRP/Gold/Silver on a 30-minute tick, with an "Open Arena" for external agents.
- **Agent Market Arena (ACM Web Conference 2026):** first lifelong real-time LLM trading benchmark; implements InvestorAgent/TradeAgent/HedgeFundAgent/DeepFundAgent across GPT-4o, GPT-4.1, Claude, Gemini.
- **StockBench (arXiv 2510.02209, Oct 2025):** "despite their strong performance on financial QA benchmarks, most LLM agents fail to outperform this simple [buy-and-hold] baseline"; strong QA/reasoning does not translate to trading skill; instruct models made more arithmetic/position-sizing errors.
- **"LLM as a Risk Manager" (arXiv 2602.07048):** using an LLM to semantically filter candidate trades on top of a statistical lead-lag strategy "reduces average loss magnitude by 48.1% for different-event pairs (from $642 to $333)." This is direct support for the LLM-as-filter, rules-execute design.

**Synthesis for Fulcrum:** what consistently fails is letting the LLM freely size, time and execute — it overtrades, mis-sizes, over-levers and holds losers. What helps is a structured harness where the LLM proposes/filters and hard-coded rules handle sizing, stops and exits (Nof1 itself fed each model's own trading plan — price target, stop, invalidation — back into future calls).

### Section 6 — Wallet-Scoring Pipeline (whale data as a signal filter, not a blind copy)

**Data sources & APIs:**
- **Nansen:** ~10,000 "smart money" wallets labeled out of 500M+ addresses; Pro ~$49–69/month. API exposes Flow Intelligence, PnL Leaderboard, PnL Summary, TGM Holders; documented copytrading workflow filters wallets with `roi_percent_realised > 50` and `win_rate > 0.6`, ordered by `pnl_usd_realised`.
- **Arkham, Dune, GMGN, Birdeye, Helius (Solana RPC), Hyperliquid info API (public leaderboard), Polymarket data API, Etherscan/Solscan** — all provide programmatic on-chain data; Hyperliquid's is unusually clean because positions are fully public.

**Metrics that separate skill from noise (from the evidence):**
- Realized PnL over 100+ closed trades (not open MTM), not raw win rate — a 50% win-rate wallet that wins $10k and loses $1k is excellent; win rate alone flatters (the YieldFund >57%-wins-still-lose finding).
- Holding period (avoid ~100-second memecoin churners you cannot follow), liquidity/market cap of tokens traded, consistency across regimes (check performance in the March 2026 stress window specifically), win/loss magnitude ratio, and sybil/rotation detection (Bubblemaps-style cluster analysis to catch insider/wash wallets).
- Prefer wallets whose edge survives being copied with realistic latency — i.e., longer holding periods and liquid tokens.

**Open-source implementations:** Nansen's documented copytrading API workflow; community Solana copy bots (OdinBot and the terminal copytrade features in GMGN/Photon/BullX/Trojan/Axiom); the open-source `zwmjj/funding-rate-arb` backtest repo for carry. Use these as signal generators feeding Fulcrum's own risk layer, not as blind executors.

### Section 7 — Scam Pattern Note

The templated "one prompt turned $X into $Y" AI-trading posts (e.g., @0xCristal's "$43.47 → $41,495.53" ChatGPT "Astra" post, and a near-identical "$19.63 → $2,766.97" Grok Bot post a day earlier) are marketing funnels, not evidence. CryptoTimes' Sept 8 2026 fact-check found the posts publish no wallet address, no funding transaction and no trade list; the attached "ASTRA ANALYST" clip is a custom dashboard, not OpenAI's interface; and the "where it traded" link is a GMGN referral page — "a referral page is not a ledger." How to spot them: (1) impersonation of a known AI brand, (2) a specific projected return, (3) urgency, (4) a referral link instead of an on-chain proof, (5) no verifiable wallet/tx. Related real harm: ChatGPT has surfaced phishing links that drained ~$2.1–2.2M (FXRP), and fake GMGN sites are active drainers — so "AI told me to" is itself an attack surface. Treat every "AI turned pocket change into a fortune" post as a referral/affiliate scam until a matching on-chain wallet history is produced.

## Recommendations

**Verdict:** Abandon the whale-copy premise. The evidence does not support blind copying, the highest-return copy venues (Solana terminals, Hyperliquid) are the most decayed and/or US-restricted, and the vault mechanism that actually removes copy latency (Hyperliquid) is illegal for you to use. Rebuild Fulcrum as a disciplined, low-turnover systematic bot with an LLM in a filter/risk-manager role, on Alpaca paper first.

**Ranked design options by strength of evidence:**
1. **(Strongest) Trend/time-series momentum on Alpaca spot crypto, volatility-scaled, with hard stops.** Peer-reviewed support (Liu & Tsyvinski), runs today on your existing paper account, low turnover controls fees. This is the primary recommendation.
2. **LLM-as-filter overlay on the momentum core.** LLM screens the candidate list / vetoes trades against news and regime; rules do all sizing, stops and exits. Supported by StockBench, the LLM-Risk-Manager paper, and Nof1's own harness lessons.
3. **Whale data as a *signal*, not a copy.** Use Nansen/Hyperliquid-info scores (100+ closed trades, PnL magnitude, liquid tokens, regime consistency) as one input feature into the momentum/filter stack — never as an auto-executor.
4. **(Later, only if you accept the legal/technical constraints) Funding-rate carry.** Highest Sharpe when it works, but requires a perp venue you cannot legally reach as a US person, and yields have thinned to near-zero excess post-cash-rate — deprioritize.
5. **(Reject) The 5%/5% grid and blind copy-trading.** Structurally negative for you.

**Concrete step-by-step build path:**
1. **Keep Fulcrum on Alpaca crypto paper.** Wire the official Alpaca MCP server (65 tools, paper default) so the agent reads data and places paper orders.
2. **Replace the grid with a trend core.** Implement a time-series momentum rule (e.g., long when price is above its N-week trend and momentum is positive; flat/short otherwise), rebalanced weekly, on 3–8 liquid coins Alpaca supports.
3. **Add a volatility target and hard risk limits.** Scale each position inversely to realized volatility; set a per-trade stop and a portfolio max-drawdown kill-switch. This directly fixes the "no exit logic" problem.
4. **Add the LLM as a filter, not an executor.** The LLM proposes a plan (target, stop, invalidation), which is stored and fed back on later cycles (Nof1's technique); rules enforce it. The LLM may veto but not size.
5. **Instrument everything and measure the right things** (see below). Log every fill, fee and slippage estimate.
6. **Run 3+ months of paper across at least one trending and one choppy regime.** Only consider live capital if the post-fee paper Sharpe and drawdown clear the thresholds below.
7. **If you go live, start on Coinbase Advanced or Alpaca with tiny size**, both US-legal and agent-aware; add whale-signal features only after the core is profitable in paper.

**What to measure (and the thresholds that change the plan):**
- **Net (post-fee) Sharpe over ≥90 days across regimes.** Go-live threshold: net Sharpe ≥ 1.0 and max drawdown ≤ 20%. Below that, stay in paper.
- **Turnover and fee drag as % of equity.** If fees exceed ~3–5% annualized of equity, cut trade frequency (the Nof1 lesson: their highest-turnover models bled 13–17% to fees).
- **Realized vs backtested slippage.** If live slippage is >2× modeled, reduce size or widen entry logic.
- **Win/loss magnitude ratio**, not win rate. If average loss > average win, the strategy is fragile regardless of win rate.
- **Kill-switch trigger:** portfolio drawdown breaching the cap halts trading pending review.

**Architecture (boxes-and-arrows, text form):**
```
[Market Data: Alpaca MCP] ──► [Feature Engine: trend, volatility, momentum,
                                (optional) whale-wallet scores from Nansen/Hyperliquid-info]
                                        │
                                        ▼
                        [Signal Generator: rules-based trend/momentum]
                                        │
                                        ▼
                        [LLM Filter/Risk Manager]  ◄── (news, regime check; may VETO, cannot size)
                                        │
                                        ▼
        [Risk Layer: volatility-scaled sizing, hard stops, max-drawdown kill-switch]
                                        │
                                        ▼
                        [Execution: Alpaca paper orders via MCP]
                                        │
                                        ▼
        [Logger/Analytics: fills, fees, slippage, Sharpe, drawdown, turnover]
                                        │
                                        └────► feeds back plan (target/stop/invalidation) to LLM next cycle
```

## Caveats
- **Numbers move.** HLP TVL/APR, vault stats, Coinbase fee tiers and perp-DEX market shares are point-in-time and cited to specific 2026 snapshots; re-check before acting.
- **Copy-trading aggregate PnL was positive** (+709k USDT in YieldFund) even though <half of copiers won — the average is carried by a few leaders, which is a survivorship trap for an individual.
- **Funding-arb Sharpe figures (5–10, or the academic 38.82/23.55) are gross and model-dependent;** independent backtesting shows most historical PnL predates 2022 and net-of-cash-rate returns have compressed toward zero, and the same academic study found CEX versions of the trade were outright negative (−7.34/−7.93 Sharpe). Treat headline Sharpes skeptically.
- **The "homogeneity/herding" claim about LLMs is only partly supported.** Nof1 documented divergent per-model "personalities" plus time-varying inter-model correlation; TradeRank documented clear herding into longs in a falling market. Both are true in different runs.
- **US regulatory status is fluid.** Hyperliquid may pursue a compliant US entity (Aug 2026 White House comment) and Kalshi has been winning court cases; Polymarket's US status is evolving after ICE's investment. Verify current legality before using any restricted venue.
- **Unverifiable marketing claims** (viral AI-trading "X→Y" posts, Solana "70% win rate" copy leaderboards, GMGN referral funnels) are excluded from the evidence base by design; treat them as scams absent on-chain proof.
- **Nothing here is investment or legal advice.** Paper-trade and consult a qualified professional before deploying capital, especially regarding US derivatives/perps law.