"""連線健檢：逐一確認每一條外部 API 管線是否暢通。"""

from __future__ import annotations

import time

import feedparser
import requests

from binance_orderflow import fetch_atr, fetch_snapshot
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


def _check_binance(cfg: Config) -> str:
    """Binance USDT-M：逐一確認 BINANCE_SYMBOLS 的 24h ticker 與 15m ATR。"""
    session = requests.Session()
    ok: list[str] = []
    failed: list[str] = []
    for code in cfg.binance_symbols:
        try:
            snapshot = fetch_snapshot(session, code)
            atr = fetch_atr(session, code, cfg.orderflow_kline_interval, cfg.orderflow_atr_period)
            ok.append(f"{code} ${snapshot.last_price:,.4f}（ATR {atr:,.4f}）")
        except Exception as exc:  # noqa: BLE001 - 單一標的失敗不影響其他
            failed.append(f"{code} ✗ {type(exc).__name__}")
    detail = f"{len(ok)}/{len(cfg.binance_symbols)} 標的正常"
    if ok:
        detail += "｜" + "｜".join(ok[:4]) + ("…" if len(ok) > 4 else "")
    if failed:
        detail += "｜失敗：" + ",".join(failed)
    return detail


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
        ("Binance", _check_binance, False),
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
    if cfg.orderflow_auto_calibrate:
        threshold_text = (
            f"自動校準（{cfg.orderflow_calibration_factor:g}×15s 均流，"
            f"地板 ${cfg.orderflow_min_window_usd:,.0f}／天花板 ${cfg.orderflow_max_window_usd:,.0f}）"
        )
    else:
        threshold_text = (
            f"固定（單筆 ${cfg.orderflow_block_usd:,.0f}／"
            f"{cfg.orderflow_window_sec}s ${cfg.orderflow_window_usd:,.0f}）"
        )
    print(
        f"🛢  多標的: {len(cfg.binance_symbols)} 個｜門檻 {threshold_text}"
        f"｜關鍵位 ±{cfg.orderflow_sr_proximity_pct:.2f}%｜止損 {cfg.orderflow_atr_mult:g}×ATR"
        f"｜全頻道上限 {cfg.orderflow_max_alerts_per_hour} 則/小時"
    )
    if required_failed:
        print("❌ 核心連線失敗：請檢查 .env 與網路／代理設定。")
        return 1
    print("✅ 核心連線正常；資料源若出現 ⚠️，可考慮在 .env 設定 DATA_PROXY_URL。")
    return 0
