# clusdt — Project Handoff (for another AI agent)

## 1. What this project is
A **paid Telegram channel bot** (`@MentalOutcrypto_bot`, chat `-1004424278724`) that pushes:
1. **Order-flow trading signals** for 23 Binance USDT-M perpetuals (crypto + TradFi like XAU/XAG/CL + tokenised stocks).
2. **Energy/market news alerts**, translated to Traditional Chinese, with an LLM importance filter.
3. **Binance official announcement alerts** (listings/delistings/airdrops/maintenance).
4. **CFTC weekly positioning** (Managed Money, NYMEX WTI + Brent).
5. **Equity fundamentals cards** (13 US stocks, on demand).

Repo: `github.com/Ashkku0909/clusdt` (branch `main`).
Deployed: **Render free web service** → `https://clusdt.onrender.com` (`uvicorn main:app`).
Local: Windows, Python 3.12 venv at `d:\github\clusdt\.venv`.

## 2. Hard architectural rules
- **Market signals are 100% deterministic. No LLM anywhere in the trading path.**
- LLM (DeepSeek `deepseek-flash`) is used **only** for: news importance scoring, asset tagging, translation fallback, and equity narrative summarisation. It never invents prices/levels.
- **Binance USDT-M public REST + WebSocket is the only market data source.** yfinance was fully removed.
- One `OrderflowEngine` instance per symbol (23 engines, 23 WS connections), sharing one process-wide `RestGate` circuit breaker and one `AlertBudget`.

## 3. Module map
| File | Responsibility |
|---|---|
| `main.py` | CLI + FastAPI app (`/`, `/health`, `/status`), lifespan starts 23 engines, background worker thread runs news+CFTC every `LOOP_INTERVAL_SECONDS` (300s) |
| `binance_orderflow.py` | Core engine: WS ingestion, 4 signal types, grading gate, ATR-based levels, alert formatting, `RestGate`, baseline disk cache, CLIs (`--orderflow`, `--ws-test`, `--dump-baseline`) |
| `market_context.py` | Multi-timeframe context: ADX(14) 15m/1h/4h/1d (Wilder), CVD from kline taker-buy, OI history 15m, funding rate; `grade_signal()` A/B/C scoring; disk cache |
| `news_pipeline.py` | News fetch/dedupe/20-min window, trafilatura scraping, translation chain, Telegram formatting |
| `news_importance.py` | DeepSeek JSON scoring 0–10 + category + affected symbols; `< NEWS_MIN_IMPORTANCE` filtered |
| `llm_translate.py` | DeepSeek batch translator (`{"lines": [...]}`) used as fallback |
| `binance_news.py` | Binance CMS announcements (`bapi/composite/v1/public/cms/article/list/query`, pageSize ≤ 20) |
| `cftc_data.py` | Socrata dataset `72hh-3qpy`, contracts `067651` (WTI) / `06765T` (Brent), weekly Managed Money |
| `equities.py` | Finnhub profile/quote/metrics/news → DeepSeek JSON brief (≤10 lines), 6h cache |
| `news_sources.py` | Finnhub, Al Jazeera RSS, Marketaux (30-min throttle), strict UTC timestamp parsing |
| `pipeline.py` | `run_full_cycle` = news + binance_news + cftc |
| `state.py` | `data/state.json` sections: `news_seen`, `news_filtered`, `binance_news_seen`, `news_translations`, flags |
| `telegram_dispatcher.py` / `telegram_client.py` | Markdown→HTML, 429 back-off, timeouts |
| `health.py` | `--health` connectivity check for all pipelines |
| `baseline_seed.json` | Committed snapshot of 24h high/low + 1h ATR for all 23 symbols (bootstraps a fresh deploy while REST is rate-limited) |
| `config.py` | All settings, env-driven (`_env`, `_env_int`, `_env_float`, `_env_bool`, `_env_list`) |

## 4. Signal engine specifics
**Symbols (23):** CLUSDT, BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, LINKUSDT, ENAUSDT, XAUUSDT, XAGUSDT, BNBUSDT, COINUSDT, HOODUSDT, SPCXUSDT, CRCLUSDT, MUUSDT, SNDKUSDT, NVDAUSDT, MSFTUSDT, AAPLUSDT, AMZNUSDT, METAUSDT, GOOGLUSDT, TSLAUSDT.

**Auto-calibrated thresholds** (per symbol):
`window_usd = clamp(4.5 × 24hQuoteVolume / 5760, $100k, $12M)`; `block = 0.5×window`; `momentum = 2×window`; `absorption = divergence = 3×window`.

**4 deterministic flow signal kinds:**
1. `sweep` — single trade ≥ block, or **60s** same-side flow ≥ window, **and** price within 0.3% of 24h high/low.
2. `momentum` — **300s** delta ≥ momentum, one side ≥ 70% share, price makes new 24h high/low (skipped if already extended >0.8×ATR beyond the extreme).
3. `absorption` — **300s** total ≥ absorption, 300s move ≤ 0.08%, sitting at a level without breaking it.
4. `divergence` — **900s** net delta ≥ divergence against the 900s price direction.

Plus a 5th, structurally different kind — **`vwap`**, a pending limit order at the 4H VWAP 2.8σ band. See §4b.

**Levels (ATR = ATR(14) on 4h klines, Wilder):**
- Entry zone **always brackets the signal price** (max 0.25×ATR away on each side): momentum −0.20/+0.05×ATR, others −0.15/+0.10×ATR, extended to include the key level when it is within 0.25×ATR. A signal whose level is already >0.25×ATR away is dropped (`suppressed_entry`) because the limit order could never fill.
- Stop loss: 1.6×ATR, and always ≥0.30×ATR outside the entry zone.
- **3-tier exits**: TP1 1.2×ATR (≈0.75R, close 50% + SL to breakeven), TP2 2.4×ATR (1.5R, close 30%), TP3 4.0×ATR (2.5R, runner 20%).
- Asset-class distance floors widen (not mute): metals 0.55%/0.65%, majors 0.85%/1.00%, alts 1.20%/1.40% of price for TP1/SL.

**Quality filters (this is where the channel's reputation lives):**- TP1 distance < `ORDERFLOW_MIN_TP1_PCT` (2.5%, or 1.8% for XAU/XAG) → drop the signal; stop distance < `ORDERFLOW_MIN_SL_PCT` (1.0%) → drop.
- S/R proximity 0.3%, per-direction cooldown **7200s**, global gap **2700s**, same-direction move ≥2.5% or ≥1.5×ATR, global budget 20 alerts/hour.
- **Context is mandatory** (`ORDERFLOW_REQUIRE_CONTEXT=1`): no signal may ever carry `評級：—`; if context is missing or older than 2700s the signal is dropped and the symbol is queued for self-healing.
- Grade gate `ORDERFLOW_MIN_GRADE=B` with **A ≥ 82 / B ≥ 70**; hard veto (grade capped at C) when 1d+4h both oppose the direction, or when CVD opposes a `momentum`/`sweep`.
- Context older than 20 min costs −10 points.
- Bad ticks: parser rejects `p<=0 or q<=0`; `_finalize` rejects prices >25% away from the reference price.

**Alert card (Traditional Chinese, Markdown → Telegram HTML)** shows direction light, entry + limit zone, stop with % + ATR multiple, TP1/TP2 with %, R:R, grade, nature, notional, size, key level, context line, and an execution note ("30 min unfilled / price beyond zone by 0.5×ATR → skip").

### 4b. VWAP ± σ mean reversion (`backtest.py` justifies every parameter)
The 5th signal kind, and the only one that is a **pending limit order** rather than an immediate entry.

**Rule:** rolling 20-bar 4H VWAP with volume-weighted σ (`σ = sqrt(Σ V·(P−VWAP)² / ΣV)`). Place a limit at **2.8σ** from VWAP, only on the side of the 4H **SMA200** (uptrend → buy dips only), stop **1.0σ**, TP1 = VWAP mean, TP2 = mean +1σ, TP3 = opposite band, time stop 12×4H bars. Fire the alert when price comes within **0.35σ** of the band so the subscriber has time to place the order.

**Backtest evidence (23 symbols, ~750 days of 4H klines, `python backtest.py --pages 3 --grid`):**

| Variant | Trades | Win% | Avg R | PF | In-sample | Out-of-sample |
|---|---|---|---|---|---|---|
| touch band, enter on close | 186 | 30.1 | −0.27 | 0.65 | −0.16 | −0.27 |
| limit fill at band only | 1083 | 39.3 | +0.18 | 1.27 | +0.24 | +0.12 |
| **σ2.8 + SMA200 + limit fill** | 275 | 47.6 | **+0.36** | **1.63** | +0.34 | **+0.40** |
| same, low-volatility filter off | 958 | 46.1 | **+0.39** | 1.68 | +0.37 | **+0.44** |

**Three findings that contradict the intuition:**
1. **Low volatility is NOT the edge.** Adding an ATR-percentile + ADX regime filter changed nothing (both variants ~+0.4R); the edge comes from trend alignment + deep deviation + passive fill. So no volatility filter is implemented.
2. **Waiting for the bar to close destroys the edge** (+0.36R → −0.27R). You must be the one providing liquidity at the band, not chasing the bounce. This is why the card says 限價掛單 with a distance from spot.
3. **Shallow bands (1.5–2.2σ) barely break even.** Every combination with σ ≥ 2.5 was positive out-of-sample (27/27 grid points), which is the strongest evidence available that this is not curve-fitted.

**Bias controls in the backtester** (each one flipped the result, so they matter): indicators for bar *i* use only bars up to *i−1*; a limit fill is allowed to be stopped out on the **entry bar**; fees 0.10% round trip; the asset-class stop floor is applied (`--min-risk`), because with a raw 0.6σ stop the fee alone eats a third of every R.

**Card differences for `vwap`:** shows 現價 plus a separate 限價掛單 line with distance from spot, labels the stop in **×σ** (not ×ATR), replaces the flow bullet with 均值結構 (deviation in σ / band width) and 趨勢對齊, and sets the validity window to the current 4H bar instead of 60 minutes.


## 5. Context & grading (`market_context.py`)
Per symbol, every 300s: 4× klines (15m/1h/4h/1d) + `futures/data/openInterestHist` (15m) + `premiumIndex`.
Outputs ADX/±DI per frame, CVD(20 bars), OI change 15m/1h with a 4-quadrant matrix (long buildup / short buildup / short covering / long liquidation), funding rate.
`grade_signal()` starts at 55, ±12 for 1h/4h trend alignment, ±10 CVD alignment, OI matrix ±10/+5, funding crowding −10, kind bonus, ADX<18 −20 / ≥25 +5 → **A ≥ 78, B ≥ 62, C**.
Context is cached to `data/context/{SYMBOL}.json` (6h) and reused during REST outages; `context_line()` appends "⚠️ 數據 N 分鐘前" past 15 min.

## 6. News pipeline
- Sources: Finnhub general news, Al Jazeera RSS, Google News feeds, Marketaux (throttled to one call / 30 min).
- **News never feeds the trading engine.** It is a separate broadcast channel; no signal depends on it. So every unit of work here must justify itself.
- **Cost order (cheap → expensive), enforced in `run_news_cycle`:**
  1. Parse the feed timestamp. Items outside the 20-minute window are dropped with **zero network calls**.
  2. Items with **no** timestamp are dropped (`NEWS_VERIFIABLE_TS_ONLY=1`). The old code fetched the article HTML just to read its `meta` tag — with ~140 items per cycle that was ~140 needless HTTP requests for articles that were almost all stale anyway.
  3. `headline_prefilter()` — free keyword gate that drops price-prediction / how-to / "top 10" / sponsored pieces.
  4. `score_news()` runs on **title + feed summary only** (`NEWS_SCORE_BEFORE_BODY=1`); the article body is fetched only for items that pass. Body scraping is additionally capped at `NEWS_MAX_SCRAPE_PER_CYCLE` (6).
  5. Translation runs last, only for the single item actually being pushed.
- Dedupe by URL/title hashes in `state.json`.
- **DeepSeek importance filter**: JSON `{score 0-10, category, reason, symbols}`, threshold 6; fail-open.
- **Translation chain**: Google (deep-translator) → **DeepSeek batch** → MyMemory (last resort). Translation cache in `state["news_translations"]` (800 entries).
- Google's free endpoint and MyMemory share the same upstream quota and both return `You made too many requests …`; that is expected, DeepSeek covers it. With the 20-minute window, `抓到 141 則 / 窗內 0 則` is the normal steady state.

## 7. CFTC & equities
- CFTC: newest report is **always last Tuesday's positions** (published the following Friday 15:30 ET) → a 9-day lag is normal, not a bug. Card shows thousand barrels (1 contract = 1,000 bbl), USD notional from the live WS price, OI share, 4-week net-position trend, report vs release date, next release.
- Equities: Finnhub metrics → PE/PEG applicability check → DeepSeek JSON `{brief, bull, bear, rating, confidence}` → compact ≤10-line card, 6h cache in `data/equities/`.
- **Fundamentals are gated by `value_trigger()`** (default: ≥25% drawdown from the 52-week high, or within 12% of the 52-week low). A 4H swing entry never needs a valuation, and valuation does not change between two 15-minute polls — so paying 4 Finnhub calls + 1 LLM call per symbol per cycle was pure waste. `--equity --force` restores the old always-analyse behaviour.

## 8. Reliability design (hard-won)
- **Binance quirk:** on this endpoint `@aggTrade`, `@ticker`, `@kline_*`, `@miniTicker`, `!ticker@arr`, `@markPrice` are **all silent** (verified against BTC control). Only `@trade` and `@bookTicker` deliver data. Never reintroduce ticker/kline streams for baselines.
- **Baselines** (24h high/low + ATR) come from REST (`ticker/24hr` weight 1, `klines` weight 2), persisted to `data/baseline/{SYMBOL}.json`, and bootstrapped from `baseline_seed.json` on a fresh deploy.
- **418/429 handling:** `RestGate` is process-wide; `parse_ban_seconds()` reads `banned until <epoch ms>`, all engines go silent until then, retries get jitter. Throttling must key off the *attempt* timestamp, never the success timestamp.
- Render free tier = 0.1 CPU, **ephemeral disk**, **shared egress IP** → bans are largely other tenants' doing. Avoid rapid consecutive deploys (each restart re-opens 23 WS + REST refreshes).
- `ORDERFLOW_SNAPSHOT_MAX_AGE_SEC = 10800` (3h) so long bans degrade gracefully instead of going silent.

## 9. Operations
- `GET /health` → `{"status":"ok"}` (cheap, no upstream calls).
- `GET /status` → engine health JSON: `engines`, `ws_connected`, `trades_total`, `signals_total`, `rest_blocked_sec`, `missing_baseline`, `settings` (live ATR interval / TP-SL multiples / VWAP params / filters), plus per-symbol `price`, `atr`, `snapshot_age_sec`, `context_age_sec`, `vwap`, `vwap_sigma`, `vwap_upper`, `vwap_lower`, `vwap_trend`, `vwap_signals`.
- **`signals_total` resets on every restart** — it is a process counter, not a history. The durable record is `signals_ledger.json`.
- **Signal ledger** (`signal_ledger.py`): every successfully dispatched card is appended to `signals_ledger.json`, which is **committed to git** so Render's ephemeral disk cannot lose it.
  - `python main.py --verify-signals` replays each entry against post-send 4H klines → `tp1/tp2/tp3/sl/expired/partial` with tiered-exit R accounting (TP1 50% → SL to breakeven, TP2 30%, TP3 20%).
  - Deliberately conservative: same-bar stop beats target, and an unfilled limit order is marked `expired` and **excluded** from win rate rather than counted as a loss.
  - `python signal_ledger.py --check SYMBOL dir ISO_TIME entry stop tp1 tp2 tp3 [limit|market]` scores any past card you paste in, with no ledger entry needed.
- `GET /status` → engine health JSON: `engines`, `ws_connected`, `trades_total`, `signals_total`, `rest_blocked_sec`, `missing_baseline`, `settings` (live ATR interval / TP-SL multiples / filters), plus per-symbol `price`, `atr`, `snapshot_age_sec`, `context_age_sec`.
- Heartbeat log every 5 min: `引擎心跳：WS x/23 已連線｜成交累計 …｜訊號累計 …｜REST 熔斷剩餘 …`.
- CLIs: `--serve`, `--once`, `--loop`, `--news`, `--binance-news`, `--orderflow`, `--cftc`, `--equity`, `--health`, `--ping`, `--ws-test N`, `--dump-baseline`.

## 10. Current tuning state (as of the latest change)
`ATR interval 4h`; `SL 1.6×ATR`; `TP1 1.2×` / `TP2 2.4×` / `TP3 4.0×ATR`; asset-class TP1/SL floors (metals 0.55/0.65, majors 0.85/1.00, alts 1.20/1.40); `min grade B` (A≥82); `cooldown 7200s`; `global gap 2700s`; same-direction move ≥2.5% or ≥1.5×ATR; context required, OI/funding cached 1h, TA refresh 1800s.
`ORDERFLOW_ENABLE_VWAP=1` with `window 20 / sigma_entry 2.8 / sigma_stop 1.0 / trend_ma 200 / approach 0.35`. Kline fetch grows to 225 bars (Binance weight still 2, no extra REST risk).
News: `VERIFIABLE_TS_ONLY=1`, `HEADLINE_PREFILTER=1`, `SCORE_BEFORE_BODY=1`, `MAX_SCRAPE_PER_CYCLE=6`. Equities: value trigger 25% drawdown / 12% from low.
**WS-native context**: each engine keeps 60 one-minute buckets from `@trade` and synthesises a live 1h trend/CVD, so `context_seed.json` (4h/1d) + WS 1h keeps grading working through 5-hour 418 bans (macro valid 6h during a ban).
Expected volume: a handful of swing cards per day, each with a 3-tier exit plan and explicit execution discipline.

## 11. Known traps / gotchas
- Editing `.env` alone does nothing on Render — `render.yaml` holds the production env vars; keep both in sync.
- `.env` and `data/` are gitignored; `baseline_seed.json` is intentionally committed.
- DeepSeek reasoning model may burn tokens in `reasoning_content`; always allow a large `max_tokens` and fall back to parsing `reasoning_content`.
- Binance `@trade` occasionally pushes `{"p":"0","q":"0","X":"NA","st":1}` placeholder events — must be dropped.
- CFTC throttle: read flags *before* fetching, write the timestamp only after a successful fetch (old code wrote it before reading, so it never ran).
- Render's free instance restarts on every push; during a rate-limit ban this can leave a fresh instance blind — the seed file exists for exactly that.
- Backtesting is only meaningful with the three bias controls in §4b. A naive band-touch backtest reports −0.27R for a strategy that is actually +0.36R, purely from look-ahead in the indicators and same-bar limit fills.

## 12. Sensible next steps (not yet built)
- Follow-up "TP1 hit / SL hit" edit messages so subscribers see outcomes (the ledger now knows the outcome; nothing pushes it yet).
- **Forward-test the live `vwap` kind against the backtest** — the code path is validated offline (`check_vwap.py`), but no production signal has fired yet; compare realized R against the +0.4R expectation before trusting it with position size.
- Optional BTC/SOL-specific tuning; more TradFi perps (XPT, XPD, COPPER, NATGAS, QQQ, SPY).
- Simplified Chinese (简体) variant of the cards if the audience shifts.
