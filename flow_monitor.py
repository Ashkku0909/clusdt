"""NYMEX WTI 盤中資金異動監控 + CFTC 週度籌碼（純數據、零 LLM）。

盤中訊號（預設 CL=F 5 分鐘 K 線）：
    Trigger：成交量 >= 5 期均量 × 1.5 或 |單根漲跌| >= 1.5%
    純結構分類：
        價漲 + 放量 → 🟢 多頭主力放量進場 (Long Buildup)
        價跌 + 放量 → 🔴 空頭主力放量砸盤 (Short Inflow)
        價漲 + 縮量 → ⚪ 縮量回補 (Short Covering)
        價跌 + 縮量 → ⚪ 縮量拋售 (Long Liquidation)

CFTC 週報：Managed Money 多單／空單／淨持倉與週變化（純數字輸出，無數值以外評論）。

防洗版：同一根 K 線僅推播一次；另有全域冷卻時間（`FLOW_ALERT_COOLDOWN_SEC`）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import yfinance as yf

from cftc_data import get_positions, CftcPosition

log = logging.getLogger(__name__)

SYMBOL = "CL=F"
SYMBOL_LABEL = "NYMEX WTI 主力合約 (CL)"

CANDLE_FLAG = f"flowmon:last_candle:{SYMBOL}"
ALERT_FLAG = f"flowmon:last_alert:{SYMBOL}"
CFTC_FLAG = "cftc:last_report:CL=F"
CFTC_CHECK_FLAG = "cftc:last_check_ts"

INTERVAL_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60, "1d": 1440}
DELAY_MARGIN_MIN = 10  # Yahoo 盤中 K 線資料延遲裕度（最後一根常仍在更新）


def _period_for(interval: str) -> str:
    return "3mo" if interval.endswith("d") else "2d"


def _flag_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- #
# 盤中量價訊號
# --------------------------------------------------------------------- #
@dataclass
class FlowCandle:
    symbol: str
    candle_time: str  # ISO UTC
    close: float
    delta_pct: float
    volume: float
    vol_ratio: float | None
    surge: bool
    triggered: bool
    label: str


def classify_label(delta_pct: float, surge: bool) -> str:
    """純結構分類（價 × 量），無任何質性判讀。"""
    up = delta_pct > 0
    if up and surge:
        return "🟢 多頭主力放量進場"
    if (not up) and surge:
        return "🔴 空頭主力放量砸盤"
    if up:
        return "⚪ 縮量回補"
    return "⚪ 縮量拋售"


def fetch_candle(symbol: str, cfg) -> FlowCandle | None:
    """取得最新「已完成」K 線並計算訊號；失敗回傳 None。"""
    interval = cfg.flow_candle_interval
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=_period_for(interval), interval=interval, auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s K 線取得失敗：%s", symbol, exc)
        return None

    if hist is None or hist.empty or len(hist) < 7:
        log.warning("%s K 線資料不足", symbol)
        return None
    hist = hist[hist["Close"].notna()]
    if len(hist) < 7:
        log.warning("%s K 線有效資料不足", symbol)
        return None

    # Yahoo 盤中最後一根 K 線常有延遲且仍在更新，必須排除；
    # 僅當最後一根已「夠舊」（超過週期 + 延遲裕度）才視為已定稿。
    interval_min = INTERVAL_MINUTES.get(interval, 5)
    last_index = hist.index[-1]
    last_dt = (
        last_index.tz_convert("UTC").to_pydatetime()
        if getattr(last_index, "tzinfo", None) is not None
        else last_index
    )
    settle_cutoff = datetime.now(timezone.utc) - timedelta(minutes=interval_min + DELAY_MARGIN_MIN)
    completed = hist.iloc[:-1] if last_dt > settle_cutoff else hist
    if len(completed) < 7:
        completed = hist

    candle = completed.iloc[-1]
    prev = completed.iloc[-2]
    prior_volumes = completed.iloc[:-1]["Volume"].dropna()

    close = float(candle["Close"])
    prev_close = float(prev["Close"])
    delta_pct = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
    volume = float(candle["Volume"] or 0.0)
    avg_volume = float(prior_volumes.tail(5).mean()) if len(prior_volumes) >= 3 else None
    vol_ratio = (volume / avg_volume) if (avg_volume and avg_volume > 0) else None
    surge = vol_ratio is not None and vol_ratio >= cfg.flow_volume_ratio
    triggered = surge or abs(delta_pct) >= cfg.flow_move_alert_pct

    candle_index = completed.index[-1]
    candle_time = (
        candle_index.tz_convert("UTC").strftime("%Y-%m-%dT%H:%MZ")
        if getattr(candle_index, "tzinfo", None) is not None
        else candle_index.strftime("%Y-%m-%dT%H:%MZ")
    )
    return FlowCandle(
        symbol=symbol,
        candle_time=candle_time,
        close=close,
        delta_pct=delta_pct,
        volume=volume,
        vol_ratio=vol_ratio,
        surge=surge,
        triggered=triggered,
        label=classify_label(delta_pct, surge),
    )


def format_flow_alert(candle: FlowCandle) -> str:
    ratio_txt = f"{candle.vol_ratio:.2f}x" if candle.vol_ratio is not None else "—"
    return "\n".join(
        [
            "🚨 **【NYMEX 原油期貨 資金異動警報】**",
            "━━━━━━━━━━━━━━━━━━",
            f"📊 **標的**: {SYMBOL_LABEL}",
            f"🧭 **資金流向**: {candle.label}",
            f"📈 **最新價位**: ${candle.close:,.2f} ({candle.delta_pct:+.2f}%)",
            f"📦 **量能狀態**: 成交量 {candle.volume:,.0f} 口 (為平均之 {ratio_txt} 倍)",
            "━━━━━━━━━━━━━━━━━━",
        ]
    )


def run_flow_cycle(cfg, dispatcher, state, dry_run: bool = False, force: bool = False) -> int:
    """一輪盤中資金流掃描；回傳成功推播數。"""
    candle = fetch_candle(SYMBOL, cfg)
    if candle is None:
        return 0

    ratio_txt = f"{candle.vol_ratio:.2f}x" if candle.vol_ratio is not None else "n/a"
    log.info(
        "資金流：%s → %s（%+.2f%%、量能 %s、K 線 %s）",
        SYMBOL_LABEL,
        candle.label,
        candle.delta_pct,
        ratio_txt,
        candle.candle_time,
    )

    flags = state.get_flags()
    last_candle = str(flags.get(CANDLE_FLAG, ""))
    last_alert = _flag_float(flags.get(ALERT_FLAG))
    now = time.time()

    if not force:
        if not candle.triggered:
            log.info("資金流：未達觸發門檻（量能 %s / 漲跌 %+.2f%%），僅記錄進度", ratio_txt, candle.delta_pct)
            if not dry_run:
                state.update_flags({CANDLE_FLAG: candle.candle_time})
            return 0
        if last_candle == candle.candle_time:
            log.info("資金流：同一根 K 線已處理，略過")
            return 0
        remaining = cfg.flow_alert_cooldown_sec - (now - last_alert)
        if last_alert and remaining > 0:
            log.info("資金流：冷卻中（剩餘 %.0f 秒），略過", remaining)
            return 0

    alert = format_flow_alert(candle)
    if dispatcher.send_alert(alert, dry_run=dry_run):
        if not dry_run:
            state.update_flags({CANDLE_FLAG: candle.candle_time, ALERT_FLAG: str(now)})
        return 1
    return 0


# --------------------------------------------------------------------- #
# CFTC 週度籌碼（Managed Money）
# --------------------------------------------------------------------- #
def _signed(value: int | None) -> str:
    return f"{value:+,}" if value is not None else "—"


def format_cftc_alert(position: CftcPosition) -> str:
    oi_line = (
        f"📦 **總 OI**: {position.open_interest:,} 口（週變 {_signed(position.oi_change)}）"
        if position.open_interest is not None
        else "📦 **總 OI**: —"
    )
    return "\n".join(
        [
            "🚨 **【NYMEX 原油期貨 資金異動警報】**",
            "━━━━━━━━━━━━━━━━━━",
            "📊 **標的**: CFTC 週報｜Managed Money｜NYMEX WTI (067651)",
            f"🧭 **淨持倉**: {position.mm_net:,} 口（週變 {_signed(position.mm_net_change)}）",
            f"📈 **多單**: {position.mm_long:,}（週變 {_signed(position.mm_long_change)}）",
            f"📉 **空單**: {position.mm_short:,}（週變 {_signed(position.mm_short_change)}）",
            oi_line,
            f"🗓 **報告日**: {position.report_date}",
            "━━━━━━━━━━━━━━━━━━",
        ]
    )


def run_cftc_cycle(cfg, dispatcher, state, dry_run: bool = False, force: bool = False) -> int:
    """檢查 CFTC 週報（僅 NYMEX WTI，067651）；新報告才推播。"""
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

    flags = state.get_flags()
    last_check = _flag_float(flags.get(CFTC_CHECK_FLAG))
    if not force and last_check and (time.time() - last_check) < cfg.cftc_check_interval_sec:
        log.info("CFTC：距上次檢查 %.0f 秒 < %d 秒，略過", time.time() - last_check, cfg.cftc_check_interval_sec)
        return 0

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
