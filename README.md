# clusdt — 幣安原油訂單流與即時新聞 Telegram 機器人

**市場訊號 100% 規則化**：以 **Binance USDT-M 公共 WebSocket／REST** 為唯一權威來源，
支援多標的——**CL 原油、BTC、ETH、SOL、XRP、LINK、ENA、XAU 黃金、XAG 白銀**——
逐筆主動吃單、機構級大單、動能突破、巨量吸收、量價背離、24h 關鍵位與 ATR 止損；
每個標的依 24h 成交額**自動校準門檻**（BTC 與 LINK 流量差 ~80 倍）。
新聞管道為「嚴格時間窗＋內文爬取＋免費直譯」，並以 **DeepSeek Flash 僅做 0–10 重要性評分＋受影響資產標記**
過濾無關新聞（不生成敘事、不做情緒判斷、不構成投資建議）；CFTC Managed Money 籌碼為輔助。

## 運作流程

```mermaid
flowchart LR
    W["Binance WebSocket<br/>CLUSDT@trade 逐筆成交"] --> T{"單筆 >= $500k<br/>或 15s 同向 >= $1M?"}
    T -->|否| X2["略過"]
    T -->|是| S{"距 24h 高／低點<br/><= 0.3%?"}
    S -->|否| X3["中段噪音壓制"]
    S -->|是| O["訂單流警報<br/>(1.5×ATR 止損、固定模板)"]
    K["Binance REST<br/>24h 高低 + 15m ATR"] --> S
    K --> O
    O --> F["Telegram<br/>(固定格式、429 退避、逾時靜默丟棄)"]
    A["Finnhub / Al Jazeera RSS<br/>(Marketaux 節流輪詢)"] --> B{"時間戳可驗證<br/>且 <= 20 分鐘?"}
    B -->|否| X["丟棄"]
    B -->|是| C["URL／標題雜湊去重"]
    C --> D["trafilatura 內文擷取<br/>(前 2-3 段)"]
    D --> E["免費直譯<br/>Google -> MyMemory"]
    E --> F
    Z["CFTC Socrata<br/>Managed Money (067651)"] --> R["週度持倉快報"]
    R --> F
    F --> G["data/state.json<br/>news_seen / 冷卻與進度旗標"]
```

- **市場訊號零 LLM**：訂單流輸出全為規則判定；LLM 僅用於新聞重要性評分（DeepSeek Flash），不生成分析文字
- **唯一權威行情**：已完全移除 `yfinance`（延遲報價／換月價差問題）；市場數據 100% 來自 Binance
- **嚴格時效**：時間戳無法驗證或超過 `NEWS_MAX_AGE_MINUTES` 一律丟棄
- **訂單流訊號**：主動吃單方向（`m` 欄位）＋金額門檻＋關鍵位過濾＋ATR 止損，全部為確定性規則

## 目錄結構

| 檔案 | 說明 |
| --- | --- |
| `main.py` | CLI 入口（once / loop / news / orderflow / ws-test / cftc / health / ping / serve） |
| `binance_orderflow.py` | **幣安訂單流引擎**：`@trade` 逐筆監聽、$500k 單筆／$1M 15s 門檻、24h 關鍵位、ATR 止損、重連退避 |
| `news_pipeline.py` | 即時新聞管線：20 分鐘窗、UTC 驗證、去重、爬取、直譯、格式化 |
| `news_importance.py` | DeepSeek Flash 新聞重要性評分（0–10，過濾低價值新聞＋受影響資產） |
| `market_context.py` | 多周期上下文：ADX(15m/1h/4h/1d)、CVD、OI 增減倉矩陣、資金費率；訊號評級 A/B/C |
| `equities.py` | 股票基本面＋新聞分析（Finnhub + DeepSeek，PE/PEG 適用性檢查，快取 `data/equities`） |
| `binance_news.py` | Binance 官方公告（上幣／下架／空投／維護／TradFi），精簡 4 行卡片 |
| `telegram_dispatcher.py` | 發送層：429 退避、逾時靜默丟棄、最小發送間隔 |
| `news_sources.py` | Finnhub / Al Jazeera RSS / Marketaux 抓取與能源關鍵字過濾 |
| `cftc_data.py` | CFTC Socrata 週報 Managed Money 持倉與推播週期（067651） |
| `telegram_client.py` | Markdown→HTML、分段、純文字退回（被 dispatcher 繼承） |
| `pipeline.py` | 週期掃描：新聞 + CFTC（訂單流引擎為 `--serve` 常駐任務） |
| `health.py` | 資料源健檢（Telegram / Binance / Marketaux / Finnhub / AJ） |
| `state.py` | 去重與進度狀態（`data/state.json` 的 `news_seen` / flags） |
| `config.py` | `.env` 讀取與參數集中管理 |
| `start_bot.cmd` | Windows 一鍵啟動（循環模式） |

> **已徹底清除**（依「零 LLM＋去 yfinance」要求刪除檔案）：`llm.py`、`prompts.py`、
> `flow_monitor.py`、`futures_data.py`、`flow_tracker.py`、`deduplicator.py`、
> `price_monitor.py`、`price_metrics.py`

## 安裝

本專案已建立獨立虛擬環境 `.venv`（Python 3.12），如需重建：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 設定（.env）

複製 `.env.example` 為 `.env` 後填入。重要欄位：

| 變數 | 說明 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Bot 金鑰與推送目標聊天室 |
| `TELEGRAM_PROXY_URL` | Telegram 代理；本機實測直連成功，留空即可 |
| `FINNHUB_API_KEY` | 主要即時新聞源（60 次/分） |
| `MARKETAUX_API_KEY` | 補充新聞源（免費 100 次/日，以 `MARKETAUX_MIN_INTERVAL_SEC` 節流） |
| `NEWS_MAX_AGE_MINUTES` | 新聞時間窗（預設 20 分鐘，逾時丟棄） |
| `ARTICLE_PARAGRAPH_LIMIT` | 內文擷取段數（預設 3） |
| `TRANSLATE_TARGET` | 翻譯目標語言（預設 `zh-TW`） |
| `MARKETAUX_MIN_INTERVAL_SEC` | Marketaux 輪詢最小間隔秒數（預設 1800） |
| `BINANCE_SYMBOL` | 單一合約代碼（預設 `CLUSDT`；`OILUSDT` 為無效代碼） |
| `BINANCE_SYMBOLS` | 多標的清單（逗號分隔；預設 CLUSDT,BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,LINKUSDT,ENAUSDT,XAUUSDT,XAGUSDT） |
| `ORDERFLOW_AUTO_CALIBRATE` / `ORDERFLOW_CALIBRATION_FACTOR` | 自動門檻校準（預設開啟；係數 4.5 × 15 秒平均流量） |
| `ORDERFLOW_MIN_WINDOW_USD` / `ORDERFLOW_MAX_WINDOW_USD` | 校準地板／天花板（預設 $100k / $12M） |
| `ORDERFLOW_MAX_ALERTS_PER_HOUR` | 全頻道每小時警報上限（預設 20；0 = 不限） |
| `ORDERFLOW_MIN_GRADE` | 最低訊號評級（`C` 不過濾；`B` 只發 B 以上；`A` 只發 A） |
| `EQUITY_SYMBOLS` / `EQUITY_CACHE_TTL_SEC` | 股票分析清單（預設 GOOGL,TSLA）與快取秒數（預設 21600） |
| `ORDERFLOW_BLOCK_USD` | 單筆主動吃單門檻（預設 500000） |
| `ORDERFLOW_WINDOW_USD` | 滾動窗口同向累計門檻（預設 1000000） |
| `ORDERFLOW_WINDOW_SEC` | 滾動窗口秒數（預設 15） |
| `ORDERFLOW_SR_PROXIMITY_PCT` | 距 24h 高／低點百分比內才發訊（預設 0.3） |
| `ORDERFLOW_ATR_PERIOD` / `ORDERFLOW_ATR_MULT` | ATR 週期（預設 14）與止損倍數（預設 1.5） |
| `ORDERFLOW_KLINE_INTERVAL` | ATR 計算 K 線週期（預設 `15m`） |
| `ORDERFLOW_COOLDOWN_SEC` | 每方向冷卻秒數（預設 60） |
| `ORDERFLOW_MIN_GAP_SEC` | 全域最小警報間隔秒數（任何方向，預設 300；調大＝更安靜） |
| `ORDERFLOW_MIN_MOVE_PCT` | 同方向再次警報所需最小價格推移 %（預設 0.12） |
| `ORDERFLOW_SNAPSHOT_MAX_AGE_SEC` | 行情基準最大容忍年齡秒數（預設 1800；逾時暫停發訊） |
| `ORDERFLOW_ENABLE_MOMENTUM` / `ORDERFLOW_ENABLE_ABSORPTION` / `ORDERFLOW_ENABLE_DIVERGENCE` | 額外訊號開關（預設 1＝啟用） |
| `ORDERFLOW_MOMENTUM_USD` / `ORDERFLOW_MOMENTUM_SHARE` | 動能突破門檻（預設 $2M / 70%） |
| `ORDERFLOW_ABSORPTION_USD` / `ORDERFLOW_ABSORPTION_RANGE_PCT` | 巨量吸收門檻（預設 $3M / 0.08%） |
| `ORDERFLOW_DIVERGENCE_USD` / `ORDERFLOW_DIVERGENCE_PRICE_PCT` | 量價背離門檻（預設 $3M / 0.10%） |
| `NEWS_IMPORTANCE_FILTER` / `NEWS_MIN_IMPORTANCE` | DeepSeek Flash 新聞過濾（預設開啟；分數 < 6 過濾） |
| `BINANCE_NEWS_ENABLED` / `BINANCE_NEWS_MAX_AGE_HOURS` / `BINANCE_NEWS_LIMIT_PER_CYCLE` | Binance 官方公告監控（預設開啟；只看 12 小時內；每輪最多 3 則） |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | 新聞評分金鑰與模型（預設 `deepseek-flash`；未設金鑰＝不過濾） |
| `ORDERFLOW_TA_REFRESH_SEC` | 24h 高低／ATR 刷新間隔（預設 300，秒） |
| `LOOP_INTERVAL_SECONDS` | Render／`--serve` 背景輪詢間隔秒數（預設 300） |
| `CFTC_FLOW_THRESHOLD` | CFTC 推播門檻（0 = 每次新報告都推播） |
| `MAX_ALERTS_PER_RUN` | 每輪最多推播則數（預設 5） |
| `DATA_PROXY_URL` | 資料源代理；留空為直連 |
| `LOG_LEVEL` | `INFO` 較安靜；`DEBUG` 顯示完整請求日誌 |

> `DEEPSEEK_*`、`FRED_API_KEY`、`PRICE_ALERT_THRESHOLD_PCT`、`TOPIC_COOLDOWN_SECONDS`
> 為歷史 LLM 版遺留欄位，現已停用（保留於 `.env` 供備查）。

## 使用方式

```powershell
.venv\Scripts\python.exe main.py --health               # 資料源健檢（建議先跑）
.venv\Scripts\python.exe main.py --ping                 # 發送連線測試訊息
.venv\Scripts\python.exe main.py --orderflow            # 多標的快照（現價/24h/ATR/校準門檻）
.venv\Scripts\python.exe main.py --orderflow --symbol BTCUSDT  # 單一標的詳細資料
.venv\Scripts\python.exe main.py --equity GOOGL TSLA    # 股票基本面＋新聞分析（快取）
.venv\Scripts\python.exe main.py --equity --force       # 分析並推播到 Telegram
.venv\Scripts\python.exe main.py --binance-news         # Binance 官方公告掃描（上幣/下架/維護）
.venv\Scripts\python.exe main.py --orderflow --force    # 加發一則示範警報（驗證 Telegram 格式）
.venv\Scripts\python.exe main.py --ws-test 60           # 實時監聽 60 秒（印統計，不發送任何訊息）
.venv\Scripts\python.exe main.py --news                 # 即時新聞掃描（20 分鐘窗 + 直譯）
.venv\Scripts\python.exe main.py --cftc                 # CFTC 週度籌碼檢查
.venv\Scripts\python.exe main.py --once                 # 一輪完整掃描（新聞 + CFTC）
.venv\Scripts\python.exe main.py --loop --interval 300  # 常駐：每 5 分鐘一輪（建議值）
.venv\Scripts\python.exe main.py --once --dry-run       # 只印出不發送（不更新狀態）
.venv\Scripts\python.exe main.py --serve                # 本機啟動 FastAPI + 訂單流引擎 + 週期掃描
```

或 `start_bot.cmd` 直接啟動循環模式。

## 部署到 Render（免費 Web Service）

1. Repo 已含 `Procfile` 與 `render.yaml`（Start Command：`uvicorn main:app --host 0.0.0.0 --port $PORT`）
2. Render Dashboard → **New → Blueprint** → 連接此 GitHub Repo → 自動建立 `clusdt-bot`
3. 於 Render 後台填入環境變數（`sync: false` 欄位）：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`FINNHUB_API_KEY`、`MARKETAUX_API_KEY`
4. 免費方案閒置 15 分鐘會休眠 → 用 **UptimeRobot** 每 5 分鐘 GET `https://<你的服務>.onrender.com/health`
   保持喚醒（`/` 與 `/health` 皆回傳 `{"status":"ok"}`）
5. 服務啟動時自動：`--serve` 以 **asyncio 任務** 常駐 Binance 訂單流引擎（WebSocket 斷線指數退避重連），
   並以 daemon 執行緒執行新聞＋CFTC 週期掃描（間隔由 `LOOP_INTERVAL_SECONDS` 控制，預設 300 秒）

> ⚠️ 免費方案磁碟為臨時（ephemeral）：`data/state.json` 重新部署後會重置，去重紀錄從零開始。
> ⚠️ 請避免「本機 loop + Render」同時執行（兩邊狀態各自獨立，會造成重複推播）。

## 排程建議（Windows 工作排程器）

- 程式：`d:\github\clusdt\.venv\Scripts\python.exe`
- 引數：`main.py --once`
- 起始於：`d:\github\clusdt`
- 建議每 5–10 分鐘觸發一次；Marketaux 已有節流保護（`MARKETAUX_MIN_INTERVAL_SEC`）

## 幣安訂單流模組（純規則、零 LLM）

`binance_orderflow.py` 以 Binance USDT-M 公共端點為唯一來源（`CLUSDT` 原油永續）：

| 項目 | 規則 |
| --- | --- |
| 資料串流 | 每個標的一條 `wss://fstream.binance.com/stream?streams=<symbol>@trade`（逐筆成交，含 `m` 主動方向） |
| 支援標的 | `BINANCE_SYMBOLS`：CLUSDT、BTCUSDT、ETHUSDT、SOLUSDT、XRPUSDT、LINKUSDT、ENAUSDT、XAUUSDT、XAGUSDT（可自由增減） |
| 自動校準 | 每標的 `15s 窗口 = 4.5 ×（24h 成交額 / 5760）`，單筆 = 窗口/2、動能/吸收/背離 = 2–3×窗口；含地板 `$100k` 與天花板 `$12M` |
| 全頻道預算 | `ORDERFLOW_MAX_ALERTS_PER_HOUR`（預設 20）跨所有標的共用，防多品種同時刷頻 |
| 主動方向 | `m=False` → 主動買（做多 🟢）；`m=True` → 主動賣（做空 🔴） |
| 開頭號誌燈 | 做多 = 綠燈 🟢；做空 = 紅燈 🔴（紅綠燈語意，一眼分辨 Call / Put） |
| 觸發 A | 單筆吃單名義金額 >= `ORDERFLOW_BLOCK_USD`（預設 $500,000） |
| 觸發 B | `ORDERFLOW_WINDOW_SEC` 秒（預設 15）內同向累計 >= `ORDERFLOW_WINDOW_USD`（預設 $1,000,000） |
| 位置過濾 | 成交價需距 24h 高或低點 <= `ORDERFLOW_SR_PROXIMITY_PCT`（預設 0.3%），否則壓制 |
| 關鍵位置 | 高＋買＝突破／測試前高阻力位（依進場價相對位置取詞）；高＋賣＝防禦前高阻力位；低＋買＝測試前低支撐位；低＋賣＝跌破／測試前低支撐位 |
| 性質 | 低點主動買／高點主動賣＝關鍵位強勢防守；其餘＝連續市價吃單 (主動做市主力) |
| 止損 | 做多＝進場 − 1.5×ATR；做空＝進場 + 1.5×ATR（ATR14＝15m K 線 EMA(TR,14)） |
| 動能突破 momentum | 60 秒同向 delta ≥ $2M 且主動佔比 ≥ 70%，價格創 24h 新高／新低（`ORDERFLOW_MOMENTUM_*`） |
| 巨量吸收 absorption | 60 秒雙向總量 ≥ $3M 但價格推不動（≤0.08%）且貼近 24h 高／低但未突破 → 低點賣壓被接走＝做多、高點買盤被吃掉＝做空（`ORDERFLOW_ABSORPTION_*`） |
| 量價背離 divergence | 5 分鐘淨主動 delta ≥ $3M 與價格方向相反 → 價漲量賣＝派發（做空）、價跌量買＝吸籌（做多）（`ORDERFLOW_DIVERGENCE_*`） |
| 多周期評級 | 進場前以 ADX(15m/1h/4h/1d)、CVD、OI 增減倉、資金費率評分：**A ≥78 / B ≥62 / C**；`ORDERFLOW_MIN_GRADE` 可只發 B 以上 |
| 進場與止盈 | 警報附「進場區間（限價 ±0.02–0.05×ATR）」與 **TP1 = 1.5×ATR、TP2 = 3×ATR**，不追價 |
| 防洗版 | 三層：每方向冷卻 `ORDERFLOW_COOLDOWN_SEC`（60s）＋全域最小間隔 `ORDERFLOW_MIN_GAP_SEC`（300s，任何方向）＋同方向最小價格推移 `ORDERFLOW_MIN_MOVE_PCT`（0.12%） |
| 韌性 | WebSocket 斷線 Exponential Backoff 5s→60s；24h/ATR 每 300 秒 REST 背景刷新；REST 失敗指數退避（418/429 硬封鎖 1 小時、絕不逐筆重試）；基準逾時自動停發 |

實際觀測（校準用）：CLUSDT 每秒約 21 筆、單筆 p99 ≈ $44k、單筆最大 ≈ $63k、
15 秒窗口資金峰值 買 $694k／賣 $638k——因此 $500k／$1M 為「機構級」門檻，日常不誤報。

警報格式（固定模板）：

```
� 【幣安 CL原油 異動】
━━━━━━━━━━━━━━━━━━
方向：做多 🟢
進場位：$96.55
止損位：$96.12 (ATR 1.5x)
性質：連續市價吃單 (主動做市主力)
大單金額：$1,350,000
成交量：13,992.00 手
關鍵位置：測試前低支撐位 $95.89
━━━━━━━━━━━━━━━━━━
```

> 開頭號誌燈隨方向自動切換（做多＝綠燈、做空＝紅燈）；實例：做空時為 `🔴 【幣安 CL原油 異動】`。

> 注意：fapi 的 `@aggTrade` 串流目前對所有代碼皆無推送（ETHUSDT 對照組同樣靜默），
> 而 `@trade` 逐筆推送且自帶相同語意的 `m` 欄位，故引擎以 `@trade` 為唯一來源。
> CFTC：僅 NYMEX WTI（合約 067651），輸出 Managed Money 多單／空單／淨持倉與週變化

## 即時新聞模組（嚴格時效、零 LLM）

- **時間戳解析（UTC 嚴格制）**：以 `python-dateutil` 解析；**僅接受帶明確時區**的
  ISO-8601／RFC 字串（如 `+02:00`／`GMT`），像 `15:11` 這種無時區裸字串一律拒收
  （避免 CEST/EDT 區域時區造成的 ±1 小時誤差）。來源欄位無效時，改爬文章 HTML 的
  `article:published_time`／`pubdate`／`<time datetime>` meta 標籤
- **時間窗**：`NEWS_MAX_AGE_MINUTES`（預設 20 分鐘），以 `datetime.now(timezone.utc)` 計算
- **顯示格式**：`🕒 發布: X 分鐘前 (HH:MM 本地時間)`（本地 = UTC+8 香港/台北時間）
- **內文擷取**：trafilatura 取前 `ARTICLE_PARAGRAPH_LIMIT` 段（含 Google News 轉址自動解析）
- **雜訊清洗**：過濾股票指標行（GF Score、P/S、P/E、市值、殖利率等）與錯誤頁文字
- **直譯**：Google 免費端點優先，失敗自動改用 MyMemory（單次 500 字元限制、長文自動分段）；
  回傳錯誤頁（Error 500 等）會被攔截並重試，全數失敗則丟棄該則
- **去重**：URL 與標題 SHA1 雜湊，持久化於 `state.json` 的 `news_seen`（保留 48 小時）
- **重要性過濾**：DeepSeek Flash 對每則新聞評 0–10 分（供給／需求／地緣政治／庫存／宏觀），
  低於 `NEWS_MIN_IMPORTANCE`（預設 6）直接丟棄並記錄於 `state.json` 的 `news_filtered`；
  未設金鑰或呼叫失敗時 fail-open 照常推播；推播訊息會附「🎯 重要性 X/10（類別）」

## API 額度備忘

| 服務 | 免費額度 | 本專案用量 |
| --- | --- | --- |
| Finnhub | 60 次/分 | 每輪 1 次 |
| Marketaux | 100 次/日（每次回 3 篇） | 節流後最多 1 次/30 分（約 48 次/日） |
| Binance USDT-M（fapi/fstream） | 無金鑰、公共端點 | WS 常駐 1 條串流；REST 24h ticker 與 15m K 線每 300 秒各 1 次 |
| CFTC Socrata | 無金鑰、免費 | 每輪 2 次查詢 |
| 翻譯（Google/MyMemory） | 免費端點 | 每則新聞 1–5 次呼叫；Google 失敗時自動冷卻 10 分鐘 |

## 疑難排解

| 症狀 | 處理 |
| --- | --- |
| `ProxyError ... 127.0.0.1:7890` | 代理未啟動；清空 `TELEGRAM_PROXY_URL` 改用直連（已實測可行） |
| `No module named 'pydantic_core._pydantic_core'`（或 jiter） | pip 快取安裝到錯誤平台 wheel；`pip install --force-reinstall --no-cache-dir 套件名` |
| 新聞一直沒推播 | 檢查是否真的在 `NEWS_MAX_AGE_MINUTES` 窗內；剛啟動時無新稿屬正常 |
| 標題出現「Error 500」 | 已攔截：Google 翻譯端點限流時自動冷卻 10 分鐘並改用 MyMemory |
| Telegram `can't parse entities` | 客戶端會自動退回純文字重送；仍失敗請看 log |
| Marketaux 回傳 0 則 | 檢查額度（402/429）與 `MARKETAUX_QUERIES`；週末新聞較少屬正常 |
| Finnhub `Invalid API key` | 重新申請金鑰後填入 `.env` |
| 想重複推播同一則新聞 | 刪除 `data/state.json` 對應 URL（或整個檔案） |
| 訂單流未推播 | 屬正常（僅關鍵位附近的大單才發）；先跑 `--ws-test 60` 看統計（門檻／S/R／冷卻壓制數） |
| 訂單流一直重連 | Binance 網路波動；引擎自動 5→60 秒指數退避，無需人工處理 |
| CFTC 未推播 | 僅在新報告日出現在推播；若 `CFTC_FLOW_THRESHOLD` > 0 需超過門檻 |
| Render 服務休眠 | UptimeRobot 定時 GET `/health`（每 5 分鐘）；或改用付費方案 |

## 免責聲明

本工具僅供資訊彙整與研究參考；所有輸出均為公開數據與機器直譯文字，不構成任何投資建議。
投資決策應自行評估風險。
