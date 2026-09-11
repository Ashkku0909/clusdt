"""CFTC 週度持倉報告（Disaggregated Futures Only，100% 量化）。

資料集：`publicreporting.cftc.gov` → `72hh-3qpy`（Disaggregated - Futures Only）
追蹤「Managed Money」於兩大原油市場的淨持倉與週變化：

- `067651` WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE（對應 CL=F）
- `06765T` BRENT LAST DAY - NEW YORK MERCANTILE EXCHANGE（對應 BZ=F）

週度資金流轉折判定：|Δ 淨持倉| > 門檻（預設 10,000 口）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from config import Config
from news_sources import DEFAULT_HEADERS

log = logging.getLogger(__name__)

DATA_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
SELECT_FIELDS = (
    "report_date_as_yyyy_mm_dd,market_and_exchange_names,contract_units,"
    "m_money_positions_long_all,m_money_positions_short_all,m_money_positions_spread,"
    "change_in_m_money_long_all,change_in_m_money_short_all,"
    "open_interest_all,change_in_open_interest_all"
)

MARKETS: dict[str, dict[str, str]] = {
    "CL=F": {
        "label": "NYMEX WTI（WTI-PHYSICAL, 067651）",
        "code": "067651",
        "keyword": "WTI",
    },
    "BZ=F": {
        "label": "NYMEX Brent Last Day（06765T）",
        "code": "06765T",
        "keyword": "BRENT",
    },
}


@dataclass
class CftcPosition:
    symbol: str
    market: str
    report_date: str
    mm_long: int
    mm_short: int
    mm_net: int
    mm_net_prev: int | None
    mm_net_change: int | None
    mm_long_change: int | None
    mm_short_change: int | None
    open_interest: int | None
    oi_change: int | None
    contract_units: str


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_market_history(cfg: Config, code: str, limit: int = 5) -> list[dict]:
    """依合約代碼取得最近數週資料（新 → 舊）。"""
    response = requests.get(
        DATA_URL,
        params={
            "$select": SELECT_FIELDS,
            "$where": f"cftc_contract_market_code='{code}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": limit,
        },
        headers=DEFAULT_HEADERS,
        timeout=(8, 40),
        proxies=cfg.data_proxies,
    )
    if response.status_code != 200:
        raise RuntimeError(f"CFTC HTTP {response.status_code}: {response.text[:150]}")
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"CFTC 回應異常：{str(rows)[:150]}")
    return rows


def build_position(symbol: str, rows: list[dict]) -> CftcPosition | None:
    """由（新→舊）資料列組出最新一期持倉與週變化。"""
    usable: list[dict] = []
    for row in rows:
        long_ = _to_int(row.get("m_money_positions_long_all"))
        short = _to_int(row.get("m_money_positions_short_all"))
        if long_ is None or short is None:
            continue
        if long_ == 0 and short == 0:
            continue  # 無 Managed Money 申報之市場列
        usable.append(row)
    if not usable:
        return None

    latest = usable[0]
    long_ = _to_int(latest.get("m_money_positions_long_all")) or 0
    short = _to_int(latest.get("m_money_positions_short_all")) or 0
    net = long_ - short

    prev_row = usable[1] if len(usable) > 1 else None
    net_prev = None
    long_prev = None
    short_prev = None
    if prev_row is not None:
        long_prev = _to_int(prev_row.get("m_money_positions_long_all"))
        short_prev = _to_int(prev_row.get("m_money_positions_short_all"))
        if long_prev is not None and short_prev is not None:
            net_prev = long_prev - short_prev

    # 週變化：優先以相鄰兩期淨值差計算；否則採用官方 change_* 欄位
    delta_long = _to_int(latest.get("change_in_m_money_long_all"))
    delta_short = _to_int(latest.get("change_in_m_money_short_all"))
    if net_prev is not None:
        net_change: int | None = net - net_prev
        long_change: int | None = (long_ - long_prev) if long_prev is not None else delta_long
        short_change: int | None = (short - short_prev) if short_prev is not None else delta_short
    elif delta_long is not None and delta_short is not None:
        net_change = delta_long - delta_short
        long_change = delta_long
        short_change = delta_short
    else:
        net_change = long_change = short_change = None

    report_date = str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10]
    return CftcPosition(
        symbol=symbol,
        market=str(latest.get("market_and_exchange_names", "")).strip(),
        report_date=report_date,
        mm_long=long_,
        mm_short=short,
        mm_net=net,
        mm_net_prev=net_prev,
        mm_net_change=net_change,
        mm_long_change=long_change,
        mm_short_change=short_change,
        open_interest=_to_int(latest.get("open_interest_all")),
        oi_change=_to_int(latest.get("change_in_open_interest_all")),
        contract_units=str(latest.get("contract_units", "") or ""),
    )


def get_positions(cfg: Config) -> dict[str, CftcPosition]:
    """取得各市場最新一期 Managed Money 持倉；單一市場失敗不影響其他。"""
    result: dict[str, CftcPosition] = {}
    for symbol, meta in MARKETS.items():
        try:
            rows = fetch_market_history(cfg, meta["code"])
        except Exception as exc:  # noqa: BLE001
            log.warning("CFTC %s 取得失敗：%s", meta["label"], exc)
            continue
        position = build_position(symbol, rows)
        if position is None:
            log.warning("CFTC %s 無可用 Managed Money 資料", meta["label"])
            continue
        if meta["keyword"] not in position.market.upper():
            log.warning("CFTC %s 市場名稱不符預期：%s", meta["label"], position.market)
        result[symbol] = position
    return result
