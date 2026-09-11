"""Brent-WTI 價差量化指標（Crude Spread Analyzer）。

優先以 FRED 歷史序列（約 3 年、750+ 筆交易日）動態計算
Brent-WTI 價差與歷史分位數（Percentile Rank）；
FRED 不可用時退回內建歷史分位基準（靜態近似值）。

歷史結果會快取於 data/spread_history.json（TTL 12 小時），
避免每個週期重複抓取完整歷史。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from config import Config

log = logging.getLogger(__name__)

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
BRENT_SERIES = "DCOILBRENTEU"
WTI_SERIES = "DCOILWTICO"
HISTORY_LIMIT = 800  # 約 3 年交易日
CACHE_TTL_SECONDS = 12 * 3600

# FRED 不可用時的退避基準（過去 3 年 Brent-WTI 價差近似分佈，美元/桶）
FALLBACK_SPREADS = [2.5, 3.8, 4.2, 4.9, 5.5, 6.1, 7.0, 7.8, 8.9, 10.2, 11.5, 12.8]


class CrudeSpreadAnalyzer:
    def __init__(self, cfg: Config, cache_path: Path | None = None):
        self.cfg = cfg
        self.cache_path = cache_path or (cfg.data_dir / "spread_history.json")
        self._cache: dict | None = None

    # ------------------------------------------------------------------ #
    # 歷史資料快取
    # ------------------------------------------------------------------ #
    def _load_cache(self) -> dict | None:
        if self._cache is not None:
            return self._cache
        if self.cache_path.exists():
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("spreads"):
                    self._cache = payload
                    return payload
            except Exception as exc:  # noqa: BLE001
                log.warning("價差快取讀取失敗：%s", exc)
        return None

    def _save_cache(self, payload: dict) -> None:
        self._cache = payload
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("價差快取寫入失敗：%s", exc)

    # ------------------------------------------------------------------ #
    # FRED 抓取
    # ------------------------------------------------------------------ #
    def _fetch_series(self, series_id: str) -> dict[str, float]:
        """回傳 {date: value}（已剔除缺失值）。"""
        params = {
            "series_id": series_id,
            "api_key": self.cfg.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": HISTORY_LIMIT,
        }
        response = requests.get(FRED_OBS_URL, params=params, timeout=(8, 30), proxies=self.cfg.data_proxies)
        if response.status_code != 200:
            raise RuntimeError(f"FRED HTTP {response.status_code}: {response.text[:150]}")
        payload = response.json()
        if "observations" not in payload:
            raise RuntimeError(f"FRED 回應異常：{str(payload)[:150]}")
        series: dict[str, float] = {}
        for obs in payload["observations"]:
            value = str(obs.get("value", "")).strip()
            if value in ("", "."):
                continue
            series[obs["date"]] = float(value)
        return series

    def _refresh_history(self) -> dict | None:
        if not self.cfg.fred_api_key:
            return None
        try:
            brent = self._fetch_series(BRENT_SERIES)
            wti = self._fetch_series(WTI_SERIES)
        except Exception as exc:  # noqa: BLE001
            log.warning("FRED 價差歷史取得失敗，改用內建基準：%s", exc)
            return None

        common = sorted(set(brent) & set(wti))
        if len(common) < 50:
            log.warning("FRED 價差歷史樣本不足（%d 筆），改用內建基準", len(common))
            return None

        spreads = [round(brent[day] - wti[day], 2) for day in common]
        payload = {
            "fetched_at": time.time(),
            "sample_size": len(spreads),
            "start_date": common[0],
            "end_date": common[-1],
            "spreads": spreads,
            "latest_date": common[-1],
            "latest_brent": brent[common[-1]],
            "latest_wti": wti[common[-1]],
        }
        self._save_cache(payload)
        log.info(
            "價差歷史已更新：%d 筆（%s ~ %s），最新 Brent=%.2f / WTI=%.2f",
            len(spreads),
            common[0],
            common[-1],
            payload["latest_brent"],
            payload["latest_wti"],
        )
        return payload

    def _history(self) -> dict:
        cached = self._load_cache()
        if cached and time.time() - float(cached.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
            return cached
        refreshed = self._refresh_history()
        if refreshed:
            return refreshed
        if cached:  # 過期快取仍優於靜態基準
            return cached
        return {
            "fetched_at": 0,
            "sample_size": len(FALLBACK_SPREADS),
            "start_date": "n/a",
            "end_date": "n/a",
            "spreads": FALLBACK_SPREADS,
            "latest_date": "",
            "latest_brent": None,
            "latest_wti": None,
        }

    # ------------------------------------------------------------------ #
    # 指標計算
    # ------------------------------------------------------------------ #
    def calculate_spread_metrics(
        self,
        brent_price: float,
        wti_price: float,
        spreads: list[float] | None = None,
    ) -> dict:
        """計算 Brent-WTI 價差及歷史分位數（Percentile Rank）。"""
        history = spreads if spreads else self._history()["spreads"]
        spread = round(brent_price - wti_price, 2)
        percentile = int(sum(1 for value in history if value < spread) / len(history) * 100)
        percentile = min(max(percentile, 1), 99)
        return {
            "brent": brent_price,
            "wti": wti_price,
            "spread": spread,
            "percentile": percentile,
            "formatted_text": f"Brent-WTI ${spread:.2f} (歷史分位數: {percentile}%)",
        }

    def get_market_context(self) -> dict | None:
        """以快取／FRED 最新價計算即時價差；無資料時回傳 None。"""
        history = self._history()
        brent = history.get("latest_brent")
        wti = history.get("latest_wti")
        if brent is None or wti is None:
            return None
        context = self.calculate_spread_metrics(float(brent), float(wti), history["spreads"])
        context["latest_date"] = history.get("latest_date", "")
        context["sample_size"] = history.get("sample_size", 0)
        context["source"] = "FRED" if history.get("fetched_at") else "內建基準"
        return context
