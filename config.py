"""讀取 .env 設定，集中管理所有執行期參數。

所有模組都應透過 `load_config()` 取得設定，不要直接讀取 os.environ，
以確保欄位名稱與預設值只有一處定義。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# override=False：已存在的系統環境變數優先於 .env
load_dotenv(BASE_DIR / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return (value if value is not None else default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if not raw:
        return default
    items = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return items or default


DEFAULT_SYMBOLS: tuple[str, ...] = (
    "CLUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "ENAUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "BNBUSDT",
    "COINUSDT",
    "HOODUSDT",
    "SPCXUSDT",
    "CRCLUSDT",
    "MUUSDT",
    "SNDKUSDT",
    # Magnificent 7（各自獨立合約）
    "NVDAUSDT",
    "MSFTUSDT",
    "AAPLUSDT",
    "AMZNUSDT",
    "METAUSDT",
    "GOOGLUSDT",
    "TSLAUSDT",
)


@dataclass(frozen=True)
class Config:
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_proxy_url: str = ""
    # 資料來源代理（FRED / Marketaux / Finnhub）
    data_proxy_url: str = ""
    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    # Data providers
    fred_api_key: str = ""
    marketaux_api_key: str = ""
    finnhub_api_key: str = ""
    alpha_vantage_api_key: str = ""
    # Runtime
    log_level: str = "INFO"
    news_article_limit: int = 5
    macro_lookback_months: int = 12
    max_alerts_per_run: int = 5
    price_alert_threshold_pct: float = 2.5
    marketaux_queries: tuple[str, ...] = ("crude oil", "OPEC")
    topic_cooldown_seconds: int = 3600
    # 即時新聞管線（零 LLM）
    news_max_age_minutes: int = 20
    article_paragraph_limit: int = 3
    translate_target: str = "zh-CN"
    mymemory_email: str = ""
    marketaux_min_interval_sec: int = 1800
    # Binance 即時訂單流（唯一權威市場資料源；零 LLM）
    binance_symbol: str = "CLUSDT"
    binance_symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    orderflow_block_usd: float = 500_000.0
    orderflow_window_usd: float = 1_000_000.0
    orderflow_window_sec: int = 60  # sweep 聚合窗口（60s）
    orderflow_sr_proximity_pct: float = 0.3
    orderflow_atr_period: int = 14
    orderflow_atr_mult: float = 1.6
    orderflow_kline_interval: str = "4h"
    orderflow_tp1_atr_mult: float = 1.2
    orderflow_tp2_atr_mult: float = 2.4
    orderflow_tp3_atr_mult: float = 4.0
    # 資產類別的最低 TP1 / SL 距離（%）
    orderflow_min_tp1_pct_metals: float = 0.55
    orderflow_min_sl_pct_metals: float = 0.65
    orderflow_min_tp1_pct_majors: float = 0.85
    orderflow_min_sl_pct_majors: float = 1.00
    orderflow_min_tp1_pct_alts: float = 1.20
    orderflow_min_sl_pct_alts: float = 1.40
    orderflow_flow_meta_ttl_sec: int = 3600  # OI / 資金費率 快取 TTL（降 REST 風險）
    orderflow_cooldown_sec: int = 7200
    orderflow_ta_refresh_sec: int = 1800
    orderflow_min_gap_sec: int = 2700
    orderflow_min_move_pct: float = 2.5
    orderflow_min_move_atr_mult: float = 1.5
    orderflow_breakout_cushion_atr: float = 0.55
    orderflow_require_context: bool = True
    orderflow_context_max_age_sec: int = 2700
    orderflow_context_stagger_sec: float = 6.0
    orderflow_heal_interval_sec: float = 8.0
    orderflow_snapshot_max_age_sec: int = 10800
    # 額外訊號（多樣性；可分別關閉）
    orderflow_enable_momentum: bool = True
    orderflow_momentum_usd: float = 2_000_000.0
    orderflow_momentum_share: float = 0.70
    orderflow_enable_absorption: bool = True
    orderflow_absorption_usd: float = 3_000_000.0
    orderflow_absorption_range_pct: float = 0.08
    orderflow_enable_divergence: bool = True
    orderflow_divergence_usd: float = 3_000_000.0
    orderflow_divergence_price_pct: float = 0.10
    # VWAP ± σ 均值回歸（限價掛單型；預設開啟，回測走查為正）
    # 回測結論：σ2.8 + SMA200 順勢 + σ 帶掛單（23 檔／750 天，樣本外 +0.4R、PF 1.6）
    orderflow_enable_vwap: bool = True
    orderflow_vwap_window: int = 20
    orderflow_vwap_sigma_entry: float = 2.8
    orderflow_vwap_sigma_stop: float = 1.0
    orderflow_vwap_trend_ma: int = 200
    orderflow_vwap_approach_sigma: float = 0.35
    # 多標的門檻自動校準（依 24h 成交額；每標的獨立）與全域警報預算
    orderflow_auto_calibrate: bool = True
    orderflow_calibration_factor: float = 4.5
    orderflow_min_window_usd: float = 250_000.0
    orderflow_max_window_usd: float = 12_000_000.0
    orderflow_max_alerts_per_hour: int = 20
    orderflow_min_grade: str = "B"  # A/B/C；C = 不過濾
    orderflow_enable_equities: bool = True  # 0 = 靜音股票永續（只留幣/金屬/能源）
    # 股票基本面分析（Finnhub + DeepSeek；快取於 data/equities）
    equity_symbols: tuple[str, ...] = (
        "GOOGL",
        "TSLA",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "COIN",
        "HOOD",
        "SPCX",
        "CRCL",
        "MU",
        "SNDK",
    )
    equity_cache_ttl_sec: int = 21600
    # 基本面只在「明顯便宜」時才做（4H 波段進場不需要估值）
    equity_value_trigger_drawdown_pct: float = 25.0  # 自 52 週高點回撤 ≥ 此值
    equity_value_trigger_from_52w_low_pct: float = 12.0  # 或距 52 週低點 ≤ 此值
    # Binance 官方公告（上幣／下架／維護）
    binance_news_enabled: bool = True
    binance_news_max_age_hours: int = 12
    binance_news_limit_per_cycle: int = 3
    # 新聞重要性過濾（DeepSeek Flash；未設金鑰時自動跳過）
    news_importance_filter: bool = True
    news_min_importance: int = 6
    # 成本控制：新聞只做「會進分析」的事（標題粗筛 → 評分 → 才抓內文與翻譯）
    news_verifiable_ts_only: bool = True  # 時戳無法驗證 → 直接丟棄，不逐則爬 HTML
    news_headline_prefilter: bool = True  # 先用標題關鍵字粗筛，再花 LLM 評分
    news_score_before_body: bool = True  # 先以標題＋摘要評分，通過才拉全文
    news_max_scrape_per_cycle: int = 6  # 每輪最多拉幾篇全文（保護 trafilatura 與頻寬）
    # CFTC 週度籌碼
    cftc_flow_threshold: int = 0
    cftc_check_interval_sec: int = 1800
    data_dir: Path = DATA_DIR

    @property
    def telegram_proxies(self) -> dict[str, str] | None:
        url = self.telegram_proxy_url
        return {"http": url, "https": url} if url else None

    @property
    def data_proxies(self) -> dict[str, str] | None:
        url = self.data_proxy_url
        return {"http": url, "https": url} if url else None


def load_config() -> Config:
    queries = tuple(
        part.strip() for part in _env("MARKETAUX_QUERIES", "crude oil,OPEC").split(",") if part.strip()
    )
    return Config(
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        telegram_proxy_url=_env("TELEGRAM_PROXY_URL"),
        data_proxy_url=_env("DATA_PROXY_URL"),
        deepseek_api_key=_env("DEEPSEEK_API_KEY"),
        deepseek_base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-flash"),
        fred_api_key=_env("FRED_API_KEY"),
        marketaux_api_key=_env("MARKETAUX_API_KEY"),
        finnhub_api_key=_env("FINNHUB_API_KEY"),
        alpha_vantage_api_key=_env("ALPHA_VANTAGE_API_KEY"),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        news_article_limit=_env_int("NEWS_ARTICLE_LIMIT", 5),
        macro_lookback_months=_env_int("MACRO_LOOKBACK_MONTHS", 12),
        max_alerts_per_run=_env_int("MAX_ALERTS_PER_RUN", 5),
        price_alert_threshold_pct=_env_float("PRICE_ALERT_THRESHOLD_PCT", 2.5),
        marketaux_queries=queries or ("crude oil", "OPEC"),
        topic_cooldown_seconds=_env_int("TOPIC_COOLDOWN_SECONDS", 3600),
        news_max_age_minutes=_env_int("NEWS_MAX_AGE_MINUTES", 20),
        article_paragraph_limit=_env_int("ARTICLE_PARAGRAPH_LIMIT", 3),
        translate_target=_env("TRANSLATE_TARGET", "zh-TW"),
        mymemory_email=_env("MYMEMORY_EMAIL", "").strip(),
        marketaux_min_interval_sec=_env_int("MARKETAUX_MIN_INTERVAL_SEC", 1800),
        binance_symbol=_env("BINANCE_SYMBOL", "CLUSDT").upper(),
        binance_symbols=_env_list("BINANCE_SYMBOLS", DEFAULT_SYMBOLS),
        orderflow_block_usd=_env_float("ORDERFLOW_BLOCK_USD", 500_000.0),
        orderflow_window_usd=_env_float("ORDERFLOW_WINDOW_USD", 1_000_000.0),
        orderflow_window_sec=_env_int("ORDERFLOW_WINDOW_SEC", 60),
        orderflow_sr_proximity_pct=_env_float("ORDERFLOW_SR_PROXIMITY_PCT", 0.3),
        orderflow_atr_period=_env_int("ORDERFLOW_ATR_PERIOD", 14),
        orderflow_atr_mult=_env_float("ORDERFLOW_ATR_MULT", 1.6),
        orderflow_kline_interval=_env("ORDERFLOW_ATR_INTERVAL", _env("ORDERFLOW_KLINE_INTERVAL", "4h")),
        orderflow_tp1_atr_mult=_env_float("ORDERFLOW_TP1_ATR_MULT", 1.2),
        orderflow_tp2_atr_mult=_env_float("ORDERFLOW_TP2_ATR_MULT", 2.4),
        orderflow_tp3_atr_mult=_env_float("ORDERFLOW_TP3_ATR_MULT", 4.0),
        orderflow_min_tp1_pct_metals=_env_float("ORDERFLOW_MIN_TP1_PCT_METALS", 0.55),
        orderflow_min_sl_pct_metals=_env_float("ORDERFLOW_MIN_SL_PCT_METALS", 0.65),
        orderflow_min_tp1_pct_majors=_env_float("ORDERFLOW_MIN_TP1_PCT_MAJORS", 0.85),
        orderflow_min_sl_pct_majors=_env_float("ORDERFLOW_MIN_SL_PCT_MAJORS", 1.00),
        orderflow_min_tp1_pct_alts=_env_float("ORDERFLOW_MIN_TP1_PCT_ALTS", 1.20),
        orderflow_min_sl_pct_alts=_env_float("ORDERFLOW_MIN_SL_PCT_ALTS", 1.40),
        orderflow_flow_meta_ttl_sec=_env_int("ORDERFLOW_FLOW_META_TTL_SEC", 3600),
        orderflow_cooldown_sec=_env_int("ORDERFLOW_COOLDOWN_SEC", 7200),
        orderflow_ta_refresh_sec=_env_int("ORDERFLOW_TA_REFRESH_SEC", 1800),
        orderflow_min_gap_sec=_env_int("ORDERFLOW_MIN_GAP_SEC", 2700),
        orderflow_min_move_pct=_env_float("ORDERFLOW_MIN_MOVE_PCT", 2.5),
        orderflow_min_move_atr_mult=_env_float("ORDERFLOW_MIN_MOVE_ATR_MULT", 1.5),
        orderflow_breakout_cushion_atr=_env_float("ORDERFLOW_BREAKOUT_CUSHION_ATR", 0.55),
        orderflow_require_context=_env_bool("ORDERFLOW_REQUIRE_CONTEXT", True),
        orderflow_context_max_age_sec=_env_int("ORDERFLOW_CONTEXT_MAX_AGE_SEC", 2700),
        orderflow_context_stagger_sec=_env_float("ORDERFLOW_CONTEXT_STAGGER_SEC", 6.0),
        orderflow_heal_interval_sec=_env_float("ORDERFLOW_HEAL_INTERVAL_SEC", 8.0),
            orderflow_snapshot_max_age_sec=_env_int("ORDERFLOW_SNAPSHOT_MAX_AGE_SEC", 10800),
        orderflow_enable_momentum=_env_bool("ORDERFLOW_ENABLE_MOMENTUM", True),
        orderflow_momentum_usd=_env_float("ORDERFLOW_MOMENTUM_USD", 2_000_000.0),
        orderflow_momentum_share=_env_float("ORDERFLOW_MOMENTUM_SHARE", 0.70),
        orderflow_enable_absorption=_env_bool("ORDERFLOW_ENABLE_ABSORPTION", True),
        orderflow_absorption_usd=_env_float("ORDERFLOW_ABSORPTION_USD", 3_000_000.0),
        orderflow_absorption_range_pct=_env_float("ORDERFLOW_ABSORPTION_RANGE_PCT", 0.08),
        orderflow_enable_divergence=_env_bool("ORDERFLOW_ENABLE_DIVERGENCE", True),
        orderflow_enable_vwap=_env_bool("ORDERFLOW_ENABLE_VWAP", True),
        orderflow_vwap_window=_env_int("ORDERFLOW_VWAP_WINDOW", 20),
        orderflow_vwap_sigma_entry=_env_float("ORDERFLOW_VWAP_SIGMA_ENTRY", 2.8),
        orderflow_vwap_sigma_stop=_env_float("ORDERFLOW_VWAP_SIGMA_STOP", 1.0),
        orderflow_vwap_trend_ma=_env_int("ORDERFLOW_VWAP_TREND_MA", 200),
        orderflow_vwap_approach_sigma=_env_float("ORDERFLOW_VWAP_APPROACH_SIGMA", 0.35),
        orderflow_divergence_usd=_env_float("ORDERFLOW_DIVERGENCE_USD", 3_000_000.0),
        orderflow_divergence_price_pct=_env_float("ORDERFLOW_DIVERGENCE_PRICE_PCT", 0.10),
        orderflow_auto_calibrate=_env_bool("ORDERFLOW_AUTO_CALIBRATE", True),
        orderflow_calibration_factor=_env_float("ORDERFLOW_CALIBRATION_FACTOR", 4.5),
        orderflow_min_window_usd=_env_float("ORDERFLOW_MIN_WINDOW_USD", 250_000.0),
        orderflow_max_window_usd=_env_float("ORDERFLOW_MAX_WINDOW_USD", 12_000_000.0),
        orderflow_max_alerts_per_hour=_env_int("ORDERFLOW_MAX_ALERTS_PER_HOUR", 20),
        orderflow_min_grade=_env("ORDERFLOW_MIN_GRADE", "B").upper(),
        orderflow_enable_equities=_env_bool("ORDERFLOW_ENABLE_EQUITIES", True),
        equity_symbols=_env_list(
            "EQUITY_SYMBOLS",
            ("GOOGL", "TSLA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "COIN", "HOOD", "SPCX", "CRCL", "MU", "SNDK"),
        ),
        equity_cache_ttl_sec=_env_int("EQUITY_CACHE_TTL_SEC", 21600),
        equity_value_trigger_drawdown_pct=_env_float("EQUITY_VALUE_TRIGGER_DRAWDOWN_PCT", 25.0),
        equity_value_trigger_from_52w_low_pct=_env_float("EQUITY_VALUE_TRIGGER_FROM_52W_LOW_PCT", 12.0),
        binance_news_enabled=_env_bool("BINANCE_NEWS_ENABLED", True),
        binance_news_max_age_hours=_env_int("BINANCE_NEWS_MAX_AGE_HOURS", 12),
        binance_news_limit_per_cycle=_env_int("BINANCE_NEWS_LIMIT_PER_CYCLE", 3),
        news_importance_filter=_env_bool("NEWS_IMPORTANCE_FILTER", True),
        news_min_importance=_env_int("NEWS_MIN_IMPORTANCE", 6),
        news_verifiable_ts_only=_env_bool("NEWS_VERIFIABLE_TS_ONLY", True),
        news_headline_prefilter=_env_bool("NEWS_HEADLINE_PREFILTER", True),
        news_score_before_body=_env_bool("NEWS_SCORE_BEFORE_BODY", True),
        news_max_scrape_per_cycle=_env_int("NEWS_MAX_SCRAPE_PER_CYCLE", 6),
        cftc_flow_threshold=_env_int("CFTC_FLOW_THRESHOLD", 0),
        cftc_check_interval_sec=_env_int("CFTC_CHECK_INTERVAL_SEC", 1800),
    )


# 推播功能必需欄位（--health 以外的模式都會檢查）
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
    ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
)
