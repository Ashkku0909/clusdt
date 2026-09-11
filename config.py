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
    deepseek_model: str = "deepseek-chat"
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
    translate_target: str = "zh-TW"
    marketaux_min_interval_sec: int = 1800
    # Binance 即時訂單流（唯一權威市場資料源；零 LLM）
    binance_symbol: str = "CLUSDT"
    orderflow_block_usd: float = 500_000.0
    orderflow_window_usd: float = 1_000_000.0
    orderflow_window_sec: int = 15
    orderflow_sr_proximity_pct: float = 0.3
    orderflow_atr_period: int = 14
    orderflow_atr_mult: float = 1.5
    orderflow_kline_interval: str = "15m"
    orderflow_cooldown_sec: int = 60
    orderflow_ta_refresh_sec: int = 300
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
        deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-v4-pro"),
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
        marketaux_min_interval_sec=_env_int("MARKETAUX_MIN_INTERVAL_SEC", 1800),
        binance_symbol=_env("BINANCE_SYMBOL", "CLUSDT").upper(),
        orderflow_block_usd=_env_float("ORDERFLOW_BLOCK_USD", 500_000.0),
        orderflow_window_usd=_env_float("ORDERFLOW_WINDOW_USD", 1_000_000.0),
        orderflow_window_sec=_env_int("ORDERFLOW_WINDOW_SEC", 15),
        orderflow_sr_proximity_pct=_env_float("ORDERFLOW_SR_PROXIMITY_PCT", 0.3),
        orderflow_atr_period=_env_int("ORDERFLOW_ATR_PERIOD", 14),
        orderflow_atr_mult=_env_float("ORDERFLOW_ATR_MULT", 1.5),
        orderflow_kline_interval=_env("ORDERFLOW_KLINE_INTERVAL", "15m"),
        orderflow_cooldown_sec=_env_int("ORDERFLOW_COOLDOWN_SEC", 60),
        orderflow_ta_refresh_sec=_env_int("ORDERFLOW_TA_REFRESH_SEC", 300),
        cftc_flow_threshold=_env_int("CFTC_FLOW_THRESHOLD", 0),
        cftc_check_interval_sec=_env_int("CFTC_CHECK_INTERVAL_SEC", 1800),
    )


# 推播功能必需欄位（--health 以外的模式都會檢查）
REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
    ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
)
