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

**4 deterministic signal kinds:**
1. `sweep` — single trade ≥ block, or 15s same-side flow ≥ window, **and** price within 0.3% of 24h high/low.
2. `momentum` — 60s delta ≥ momentum, one side ≥ 70% share, price makes new 24h high/low.
3. `absorption` — 60s total ≥ absorption, 60s move ≤ 0.08%, sitting at a level without breaking it.
4. `divergence` — 300s net delta ≥ divergence against the 300s price direction.

**Levels (all ATR-based, ATR = ATR(14) on 1h klines, Wilder):**
- Entry zone: price −0.25×ATR … price +0.10×ATR (long), mirrored for short.
- Stop loss: `min(price − 1.5×ATR, entry_low − 0.2×ATR)` (long; mirrored short) — always outside the entry zone.
- TP1 = 3×ATR, TP2 = 5×ATR (R:R ≈ 1:2 / 1:3.3).

**Quality filters (this is where the channel's reputation lives):**
- TP1 distance < `ORDERFLOW_MIN_TP1_PCT` (1.2%) → drop the signal.
- S/R proximity 0.3%, per-direction cooldown 900s, global gap 1800s, same-direction min move 0.8%, global budget 20 alerts/hour.
- Grade gate `ORDERFLOW_MIN_GRADE=B` (needs context). When context is unavailable (`—`): divergence is blocked, other kinds additionally require flow ≥ momentum threshold.
- Bad ticks: parser rejects `p<=0 or q<=0`; `_finalize` rejects prices >25% away from the reference price.

**Alert card (Traditional Chinese, Markdown → Telegram HTML)** shows direction light, entry + limit zone, stop with % + ATR multiple, TP1/TP2 with %, R:R, grade, nature, notional, size, key level, context line, and an execution note ("30 min unfilled / price beyond zone by 0.5×ATR → skip").

## 5. Context & grading (`market_context.py`)
Per symbol, every 300s: 4× klines (15m/1h/4h/1d) + `futures/data/openInterestHist` (15m) + `premiumIndex`.
Outputs ADX/±DI per frame, CVD(20 bars), OI change 15m/1h with a 4-quadrant matrix (long buildup / short buildup / short covering / long liquidation), funding rate.
`grade_signal()` starts at 55, ±12 for 1h/4h trend alignment, ±10 CVD alignment, OI matrix ±10/+5, funding crowding −10, kind bonus, ADX<18 −20 / ≥25 +5 → **A ≥ 78, B ≥ 62, C**.
Context is cached to `data/context/{SYMBOL}.json` (6h) and reused during REST outages; `context_line()` appends "⚠️ 數據 N 分鐘前" past 15 min.

## 6. News pipeline
- Sources: Finnhub general news, Al Jazeera RSS, Google News feeds, Marketaux (throttled to one call / 30 min).
- Strict 20-minute freshness window; unparseable timestamps are rejected.
- Dedupe by URL/title hashes in `state.json`.
- Body via trafilatura (falls back to feed summary).
- **DeepSeek importance filter**: JSON `{score 0-10, category, reason, symbols}`, threshold 6; fail-open.
- **Translation chain**: Google (deep-translator) → **DeepSeek batch** → MyMemory (last resort). Translation cache in `state["news_translations"]` (800 entries).
- Google's free endpoint and MyMemory share the same upstream quota and both return `You made too many requests …`; that is expected, DeepSeek covers it.

## 7. CFTC & equities
- CFTC: newest report is **always last Tuesday's positions** (published the following Friday 15:30 ET) → a 9-day lag is normal, not a bug. Card shows thousand barrels (1 contract = 1,000 bbl), USD notional from the live WS price, OI share, 4-week net-position trend, report vs release date, next release.
- Equities: Finnhub metrics → PE/PEG applicability check → DeepSeek JSON `{brief, bull, bear, rating, confidence}` → compact ≤10-line card, 6h cache in `data/equities/`.

## 8. Reliability design (hard-won)
- **Binance quirk:** on this endpoint `@aggTrade`, `@ticker`, `@kline_*`, `@miniTicker`, `!ticker@arr`, `@markPrice` are **all silent** (verified against BTC control). Only `@trade` and `@bookTicker` deliver data. Never reintroduce ticker/kline streams for baselines.
- **Baselines** (24h high/low + ATR) come from REST (`ticker/24hr` weight 1, `klines` weight 2), persisted to `data/baseline/{SYMBOL}.json`, and bootstrapped from `baseline_seed.json` on a fresh deploy.
- **418/429 handling:** `RestGate` is process-wide; `parse_ban_seconds()` reads `banned until <epoch ms>`, all engines go silent until then, retries get jitter. Throttling must key off the *attempt* timestamp, never the success timestamp.
- Render free tier = 0.1 CPU, **ephemeral disk**, **shared egress IP** → bans are largely other tenants' doing. Avoid rapid consecutive deploys (each restart re-opens 23 WS + REST refreshes).
- `ORDERFLOW_SNAPSHOT_MAX_AGE_SEC = 10800` (3h) so long bans degrade gracefully instead of going silent.

## 9. Operations
- `GET /health` → `{"status":"ok"}` (cheap, no upstream calls).
- `GET /status` → engine health JSON: `engines`, `ws_connected`, `trades_total`, `signals_total`, `rest_blocked_sec`, `missing_baseline`, `settings` (live ATR interval / TP-SL multiples / filters), plus per-symbol `price`, `atr`, `snapshot_age_sec`, `context_age_sec`.
- Heartbeat log every 5 min: `引擎心跳：WS x/23 已連線｜成交累計 …｜訊號累計 …｜REST 熔斷剩餘 …`.
- CLIs: `--serve`, `--once`, `--loop`, `--news`, `--binance-news`, `--orderflow`, `--cftc`, `--equity`, `--health`, `--ping`, `--ws-test N`, `--dump-baseline`.

## 10. Current tuning state (as of the latest change)
`ATR interval 1h`; `SL 1.5×ATR`; `TP1 3×ATR`; `TP2 5×ATR`; `min TP1 1.2%`; `min grade B`; `cooldown 900s`; `global gap 1800s`; `min move 0.8%`.
Expected volume: **≤ 2 signal cards per hour**, each with TP1 ≥ 1.2% and R:R ≥ 1:2.

## 11. Known traps / gotchas
- Editing `.env` alone does nothing on Render — `render.yaml` holds the production env vars; keep both in sync.
- `.env` and `data/` are gitignored; `baseline_seed.json` is intentionally committed.
- DeepSeek reasoning model may burn tokens in `reasoning_content`; always allow a large `max_tokens` and fall back to parsing `reasoning_content`.
- Binance `@trade` occasionally pushes `{"p":"0","q":"0","X":"NA","st":1}` placeholder events — must be dropped.
- CFTC throttle: read flags *before* fetching, write the timestamp only after a successful fetch (old code wrote it before reading, so it never ran).
- Render's free instance restarts on every push; during a rate-limit ban this can leave a fresh instance blind — the seed file exists for exactly that.

## 12. Sensible next steps (not yet built)
- Signal ledger + automatic win-rate report (biggest credibility win for a paid channel).
- Follow-up "TP1 hit / SL hit" edit messages so subscribers see outcomes.
- Optional BTC/SOL-specific tuning; more TradFi perps (XPT, XPD, COPPER, NATGAS, QQQ, SPY).
- Simplified Chinese (简体) variant of the cards if the audience shifts.
