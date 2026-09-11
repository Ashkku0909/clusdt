"""連線健檢：逐一確認每一條外部 API 管線是否暢通。"""

from __future__ import annotations

import time

import feedparser
import requests
import yfinance as yf

from config import Config
from news_sources import ALJAZEERA_RSS_URLS, DEFAULT_HEADERS, is_aljazeera_relevant

TIMEOUT = (6, 15)


def _check_telegram(cfg: Config) -> str:
    if not cfg.telegram_bot_token:
        raise RuntimeError("未設定 TELEGRAM_BOT_TOKEN")
    response = requests.get(
        f"https://api.telegram.org/bot{cfg.telegram_bot_token}/getMe",
        proxies=cfg.telegram_proxies,
        timeout=TIMEOUT,
    )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"getMe 失敗：{payload.get('description', response.status_code)}")
    username = payload["result"].get("username", "unknown")
    transport = f"via {cfg.telegram_proxy_url}" if cfg.telegram_proxy_url else "直連"
    return f"@{username}（{transport}）"


def _check_yfinance(cfg: Config) -> str:
    ticker = yf.Ticker("CL=F")
    hist = ticker.history(period="1d", interval=cfg.flow_candle_interval, auto_adjust=False)
    if hist is None or hist.empty:
        raise RuntimeError("CL=F 無 K 線資料")
    last = hist.iloc[-1]
    return f"CL=F {cfg.flow_candle_interval} K 線正常：{len(hist)} 筆，最近收盤 {float(last['Close']):.2f}"


def _check_marketaux(cfg: Config) -> str:
    if not cfg.marketaux_api_key:
        raise RuntimeError("未設定 MARKETAUX_API_KEY")
    response = requests.get(
        "https://api.marketaux.com/v1/news/all",
        params={"api_token": cfg.marketaux_api_key, "language": "en", "limit": 1, "search": "crude oil"},
        timeout=TIMEOUT,
        proxies=cfg.data_proxies,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:150]}")
    data = response.json().get("data") or []
    if not data:
        return "連線正常（查詢暫無結果）"
    return f"最新: {(data[0].get('title') or '')[:40]}…"


def _check_finnhub(cfg: Config) -> str:
    if not cfg.finnhub_api_key:
        raise RuntimeError("未設定 FINNHUB_API_KEY")
    response = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": "XOM", "token": cfg.finnhub_api_key},
        timeout=TIMEOUT,
        proxies=cfg.data_proxies,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:150]}")
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return f"XOM 最新 ${payload.get('c', '?')}（前收 ${payload.get('pc', '?')}）"


def _check_aljazeera(cfg: Config) -> str:
    response = requests.get(
        ALJAZEERA_RSS_URLS[0],
        headers=DEFAULT_HEADERS,
        timeout=TIMEOUT,
        proxies=cfg.data_proxies,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    feed = feedparser.parse(response.content)
    hits = sum(
        1
        for entry in feed.entries
        if is_aljazeera_relevant(f"{entry.get('title', '')} {entry.get('summary', '')}")
    )
    return f"RSS 正常：{len(feed.entries)} 篇，能源／中東相關 {hits} 篇"


def run_health(cfg: Config) -> int:
    checks = [
        ("Telegram", _check_telegram, True),
        ("yfinance", _check_yfinance, False),
        ("Marketaux", _check_marketaux, False),
        ("Finnhub", _check_finnhub, False),
        ("AlJazeera", _check_aljazeera, False),
    ]
    print("\n🩺 系統連線健檢")
    print("─" * 64)
    required_failed = False
    for name, checker, required in checks:
        start = time.perf_counter()
        try:
            detail = checker(cfg)
            icon = "✅"
        except Exception as exc:  # noqa: BLE001 - 健檢需吞掉錯誤並顯示
            detail = f"{type(exc).__name__}: {exc}"
            icon = "❌" if required else "⚠️"
            if required:
                required_failed = True
        elapsed = (time.perf_counter() - start) * 1000
        print(f"{icon} {name:<9} {elapsed:>7.0f} ms  {detail}")
    print("─" * 64)
    print(
        f"⚙️  Telegram 代理: {cfg.telegram_proxy_url or '直連'}"
        f"｜資料代理: {cfg.data_proxy_url or '直連'}"
        f"｜新聞時效窗: {cfg.news_max_age_minutes} 分鐘｜每輪上限: {cfg.max_alerts_per_run} 則"
    )
    if required_failed:
        print("❌ 核心連線失敗：請檢查 .env 與網路／代理設定。")
        return 1
    print("✅ 核心連線正常；資料源若出現 ⚠️，可考慮在 .env 設定 DATA_PROXY_URL。")
    return 0
