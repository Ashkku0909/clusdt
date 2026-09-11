"""FRED 每日原油現貨價格監測（Brent / WTI）。

監測 DCOILBRENTEU（Brent 現貨）與 DCOILWTICO（WTI 現貨）：
- 偵測到新的交易日數據時，若單日波動超過門檻
  （.env 的 PRICE_ALERT_THRESHOLD_PCT），即觸發策略快報。
- 快報附上 1 日 / 5 日變化與 Brent-WTI 價差，供跨市場與價差判讀。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from config import Config
from news_sources import NewsItem

log = logging.getLogger(__name__)

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

SERIES: dict[str, dict[str, str]] = {
    "DCOILBRENTEU": {
        "label": "Brent 現貨",
        "page": "https://fred.stlouisfed.org/series/DCOILBRENTEU",
    },
    "DCOILWTICO": {
        "label": "WTI 現貨",
        "page": "https://fred.stlouisfed.org/series/DCOILWTICO",
    },
}
TRIGGER_SERIES = "DCOILBRENTEU"
DATE_FLAG = "price:last_date"
OBSERVATION_LIMIT = 8


@dataclass
class PriceUpdate:
    item: NewsItem
    flags: dict[str, str]
    triggered: bool


def _fetch_observations(cfg: Config, series_id: str, limit: int = OBSERVATION_LIMIT) -> list[dict]:
    params = {
        "series_id": series_id,
        "api_key": cfg.fred_api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    response = requests.get(FRED_OBS_URL, params=params, timeout=(8, 25), proxies=cfg.data_proxies)
    if response.status_code != 200:
        raise RuntimeError(f"FRED HTTP {response.status_code}: {response.text[:180]}")
    payload = response.json()
    if "observations" not in payload:
        raise RuntimeError(f"FRED 回應異常：{str(payload)[:180]}")
    return [
        obs
        for obs in payload["observations"]
        if str(obs.get("value", "")).strip() not in ("", ".")
    ]


def _stats(observations: list[dict]) -> dict | None:
    """計算最新值、單日變化與 5 日變化。"""
    if not observations:
        return None
    latest = observations[0]
    value = float(latest["value"])
    result: dict = {"date": latest["date"], "value": value}
    if len(observations) > 1:
        previous = float(observations[1]["value"])
        result["chg1d"] = value - previous
        result["pct1d"] = ((value - previous) / previous * 100.0) if previous else 0.0
    if len(observations) > 4:
        five_days = float(observations[4]["value"])
        result["chg5d"] = value - five_days
        result["pct5d"] = ((value - five_days) / five_days * 100.0) if five_days else 0.0
    return result


def _build_summary(stats: dict[str, dict]) -> str:
    lines: list[str] = []
    for series_id, meta in SERIES.items():
        item = stats.get(series_id)
        if not item:
            continue
        line = f"- {meta['label']}（{item['date']}）: {item['value']:.2f} 美元/桶"
        if "pct1d" in item:
            line += f"；單日 {item['chg1d']:+.2f}（{item['pct1d']:+.1f}%）"
        if "pct5d" in item:
            line += f"；5 日 {item['chg5d']:+.2f}（{item['pct5d']:+.1f}%）"
        lines.append(line)

    brent = stats.get("DCOILBRENTEU")
    wti = stats.get("DCOILWTICO")
    if brent and wti:
        spread = brent["value"] - wti["value"]
        lines.append(f"- Brent-WTI 價差: {spread:.2f} 美元/桶")

    return (
        "FRED 收錄之 EIA 每日原油現貨價格（最新交易日數據）：\n"
        + "\n".join(lines)
        + "\n（單位：美元/桶；週末與部分假日無觀測值，5 日變化為最近 5 個觀測日比較）"
    )


def check_price_update(cfg: Config, state) -> PriceUpdate | None:
    """有新交易日數據時回傳 PriceUpdate，否則回傳 None。

    `state` 需為 StateStore（使用 get_flags()）。
    """
    if not cfg.fred_api_key:
        log.warning("未設定 FRED_API_KEY，略過油價監測")
        return None

    observations: dict[str, list[dict]] = {}
    for series_id in SERIES:
        try:
            observations[series_id] = _fetch_observations(cfg, series_id)
        except Exception as exc:  # noqa: BLE001 - 單一系列失敗不影響其他
            log.warning("FRED %s 取得失敗：%s", series_id, exc)

    anchor = observations.get(TRIGGER_SERIES) or observations.get("DCOILWTICO") or []
    if not anchor:
        return None
    latest_date = anchor[0]["date"]

    seen = state.get_flags()
    if seen.get(DATE_FLAG) == latest_date:
        return None  # 此交易日已檢查過

    stats = {
        series_id: computed
        for series_id, obs in observations.items()
        if (computed := _stats(obs)) is not None
    }
    if not stats:
        return None

    moves = [item["pct1d"] for item in stats.values() if "pct1d" in item]
    biggest = max(moves, key=abs) if moves else None
    triggered = any(abs(move) >= cfg.price_alert_threshold_pct for move in moves)

    if biggest is None:
        title = f"國際油價快報（數據日 {latest_date}）"
    else:
        title = f"國際油價快報：單日最大波動 {biggest:+.1f}%（數據日 {latest_date}）"

    item = NewsItem(
        title=title,
        summary=_build_summary(stats),
        source="U.S. EIA / FRED（現貨價）",
        url=SERIES[TRIGGER_SERIES]["page"],
        published_utc=latest_date,
        sort_key=latest_date,
        provider="fred-price",
    )
    return PriceUpdate(item=item, flags={DATE_FLAG: latest_date}, triggered=triggered)
