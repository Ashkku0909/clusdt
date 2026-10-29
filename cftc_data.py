"""CFTC 週度持倉報告（Disaggregated Futures Only，100% 量化）。

資料集：`publicreporting.cftc.gov` → `72hh-3qpy`（Disaggregated - Futures Only）
追蹤「Managed Money」於兩大原油市場的淨持倉與週變化：

- `067651` WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE（對應 CL=F）
- `06765T` BRENT LAST DAY - NEW YORK MERCANTILE EXCHANGE（對應 BZ=F）

週度資金流轉折判定：|Δ 淨持倉| > 門檻（預設 10,000 口）。
本模組同時提供 `run_cftc_cycle()` 推播週期（固定模板、零 LLM）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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
    barrels_per_contract: int = 1000
    net_history: tuple[int, ...] = ()  # 新 → 舊（最多 4 週）


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _barrels_per_contract(units: str) -> int:
    """CFTC 回傳的 contract_units，例如 '(CONTRACTS OF 1,000 BARRELS)' → 1000。"""
    match = re.search(r"([\d,]+)\s*BARREL", (units or "").upper())
    if match:
        try:
            return max(1, int(match.group(1).replace(",", "")))
        except ValueError:
            pass
    return 1000


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
    net_history: list[int] = []
    for row in usable[:4]:
        row_long = _to_int(row.get("m_money_positions_long_all")) or 0
        row_short = _to_int(row.get("m_money_positions_short_all")) or 0
        net_history.append(row_long - row_short)
    units = str(latest.get("contract_units", "") or "")
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
        contract_units=units,
        barrels_per_contract=_barrels_per_contract(units),
        net_history=tuple(net_history),
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


# --------------------------------------------------------------------- #
# 週報推播週期（固定模板、零 LLM）
# --------------------------------------------------------------------- #
CFTC_FLAG = "cftc:last_report:CL=F"
CFTC_CHECK_FLAG = "cftc:last_check_ts"


def _flag_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _signed(value: int | None) -> str:
    return f"{value:+,}" if value is not None else "—"


WEEKDAY_ZH = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _weekday_label(day: date, with_month: bool = True) -> str:
    stamp = day.strftime("%m-%d") if with_month else day.strftime("%Y-%m-%d")
    return f"{stamp}（{WEEKDAY_ZH[day.weekday()]}）"


def _fmt_kbbl(contracts: int | None, barrels_per_contract: int, signed: bool = False) -> str:
    """口 → 千桶（WTI 1 口 = 1,000 桶，故口數即千桶數）。"""
    if contracts is None:
        return "—"
    thousands = abs(contracts) * barrels_per_contract / 1000.0
    if signed:
        sign = "-" if contracts < 0 else "+"
        return f"{sign}{thousands:,.0f}"
    return f"{thousands:,.0f}"


MARKET_TITLES = {
    "CL=F": "NYMEX WTI",
    "BZ=F": "NYMEX Brent Last Day",
}


def _reference_price() -> float | None:
    """從訂單流引擎的 WebSocket 快取取 CLUSDT 現價（零 I/O；取不到就略過）。"""
    try:
        from binance_orderflow import LATEST_PRICES
    except Exception:  # noqa: BLE001 - 循環匯入或模組未載入
        return None
    price = LATEST_PRICES.get("CLUSDT")
    return price if price and price > 0 else None


def format_cftc_alert(position: CftcPosition) -> str:
    bpc = position.barrels_per_contract
    barrels = position.mm_net * bpc
    price = _reference_price()
    notional = f"｜名義 {barrels * price / 1_0000_0000:+.1f} 億美元" if price else ""
    oi_share = (
        f"{position.mm_net / position.open_interest * 100:+.1f}%"
        if position.open_interest
        else "—"
    )

    lines = [
        f"📊 **【CFTC 週度機構持倉 · {MARKET_TITLES.get(position.symbol, position.symbol)}】**",
        "━━━━━━━━━━━━━━━━━━",
        f"🧭 **管理基金淨持倉**: {_fmt_kbbl(position.mm_net, bpc, signed=True)} 千桶"
        f"（週變 {_fmt_kbbl(position.mm_net_change, bpc, signed=True)}{notional}）",
        f"　│ 佔 OI {oi_share}",
        f"📈 **多單**: {_fmt_kbbl(position.mm_long, bpc)} 千桶"
        f"（週變 {_fmt_kbbl(position.mm_long_change, bpc, signed=True)}）",
        f"📉 **空單**: {_fmt_kbbl(position.mm_short, bpc)} 千桶"
        f"（週變 {_fmt_kbbl(position.mm_short_change, bpc, signed=True)}）",
    ]
    if position.open_interest is not None:
        lines.append(
            f"📦 **總持倉 OI**: {_fmt_kbbl(position.open_interest, bpc)} 千桶"
            f"（週變 {_fmt_kbbl(position.oi_change, bpc, signed=True)}）"
        )

    # 近幾週淨持倉趨勢（機構是持續加倉還是減倉）
    history = list(position.net_history)  # 新 → 舊
    if len(history) >= 2:
        sequence = " → ".join(_fmt_kbbl(value, bpc) for value in reversed(history))
        diffs = [history[i] - history[i + 1] for i in range(len(history) - 1)]
        streak = 1
        for diff in diffs[1:]:  # 第一筆已計入 streak
            if (diffs[0] > 0 and diff > 0) or (diffs[0] < 0 and diff < 0):
                streak += 1
            else:
                break
        if diffs[0] > 0:
            trend = f"連續加倉 {streak} 週" if streak >= 2 else "本週加倉"
        elif diffs[0] < 0:
            trend = f"連續減倉 {streak} 週" if streak >= 2 else "本週減倉"
        else:
            trend = "本週持平"
        lines.append(f"📊 **近 {len(history)} 週淨持倉**: {sequence} 千桶（{trend}）")

    report_day = _parse_date(position.report_date)
    if report_day is not None:
        release_day = report_day + timedelta(days=3)  # 週二收盤 → 週五 15:30 ET 發布
        next_release = report_day + timedelta(days=10)  # 下一期（下週二持倉）的週五發布
        delay = (datetime.now(timezone.utc).date() - report_day).days
        lines.append(
            f"🗓 **報告日**: {report_day.isoformat()}（週二收盤）｜上期發布 {_weekday_label(release_day)}"
        )
        lines.append(
            f"　│ 資料延遲 {delay} 天｜下次發布 {_weekday_label(next_release)}15:30 ET"
        )
    else:
        lines.append(f"🗓 **報告日**: {position.report_date}")

    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "💡 1 口 = 1,000 桶（1 千桶）；週度倉位為背景資訊，即時方向請看 CLUSDT 訂單流",
    ]
    return "\n".join(lines)


def run_cftc_cycle(cfg, dispatcher, state, dry_run: bool = False, force: bool = False) -> int:
    """檢查 CFTC 週報（僅 NYMEX WTI，067651）；新報告才推播。"""
    # 先看節流再打 API：舊版先抓資料再寫時戳，導致 last_check 永遠等於「現在」而一律略過
    flags = state.get_flags()
    last_check = _flag_float(flags.get(CFTC_CHECK_FLAG))
    if not force and last_check and (time.time() - last_check) < cfg.cftc_check_interval_sec:
        log.info("CFTC：距上次檢查 %.0f 秒 < %d 秒，略過", time.time() - last_check, cfg.cftc_check_interval_sec)
        return 0

    try:
        positions = get_positions(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("CFTC 取得失敗：%s", exc)
        return 0
    position = positions.get("CL=F")
    if position is None:
        log.warning("CFTC：無 WTI 持倉資料")
        return 0

    if not dry_run:
        state.update_flags({CFTC_CHECK_FLAG: str(time.time())})

    is_new = str(flags.get(CFTC_FLAG, "")) != position.report_date
    change = position.mm_net_change

    if not force:
        if not is_new:
            log.info("CFTC：已處理（報告日 %s）", position.report_date)
            return 0
        if cfg.cftc_flow_threshold > 0 and (change is None or abs(change) < cfg.cftc_flow_threshold):
            log.info(
                "CFTC：週變 %s 未達門檻（%s 口），僅記錄進度",
                _signed(change),
                f"{cfg.cftc_flow_threshold:,}",
            )
            if not dry_run:
                state.update_flags({CFTC_FLAG: position.report_date})
            return 0

    log.info("CFTC：%s 觸發推播（淨持倉 %s 口，週變 %s）", position.report_date, f"{position.mm_net:,}", _signed(change))
    alert = format_cftc_alert(position)
    if dispatcher.send_alert(alert, dry_run=dry_run):
        if not dry_run:
            state.update_flags({CFTC_FLAG: position.report_date})
        return 1
    return 0
