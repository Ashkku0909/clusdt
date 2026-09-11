"""資金流警報（100% 量化、無 LLM）：量價 OI 微觀結構 + CFTC 週度籌碼。

Telegram 輸出遵循固定格式（可讀性優先、數字驅動、不含敘事）：

    🚨 【NYMEX 原油期貨 資金進出警報】
    ━━━━━━━━━━━━━━━━━━
    📊 標的: ...
    🧭 資金信號: ...
    📈 最新報價: $XX.XX (+X.XX%)
    📦 成交量倍數: X.XXx（5 日均）/ X.XXx（20 日均）
    📊 未平倉量變動: ...
    🔍 籌碼判定:
    • [單句、含數字的客觀研判]
    ━━━━━━━━━━━━━━━━━━

防洗版機制：
- 同一商品於同一交易日的量價訊號僅推播一次（`flow:broadcast:<symbol>`）。
- CFTC 週報以報告日期為閘門（`cftc:last_report:<symbol>`），僅新報告才評估。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cftc_data import CftcPosition, get_positions
from futures_data import (
    SYMBOLS,
    FlowSignal,
    FuturesSnapshot,
    classify_flow,
    et_today,
    fetch_snapshot,
)

if TYPE_CHECKING:  # pragma: no cover - 僅型別提示，避免循環匯入
    from pipeline import Tools

log = logging.getLogger(__name__)

OI_HISTORY_SECTION = "oi_history"
BASELINE_SECTION = "flow_baselines"
FLOW_FLAG_PREFIX = "flow:broadcast"
CFTC_FLAG_PREFIX = "cftc:last_report"
OI_HISTORY_MAX_DAYS = 120


# --------------------------------------------------------------------- #
# 格式化（固定模板）
# --------------------------------------------------------------------- #
def _ratio(value: float | None) -> str:
    return f"{value:.2f}x" if value is not None else "—"


def format_flow_alert(signal: FlowSignal) -> str:
    """量價 OI 微觀結構警報（標準格式）。"""
    snap = signal.snapshot
    if snap.oi_change is not None and snap.open_interest is not None:
        oi_line = f"Δ {snap.oi_change:+,} 口（總計 {snap.open_interest:,} 口，基準 {snap.oi_prev_date}）"
    elif snap.open_interest is not None:
        oi_line = f"基準累積中（總計 {snap.open_interest:,} 口）"
    else:
        oi_line = "資料暫不可用"

    return "\n".join(
        [
            "🚨 【NYMEX 原油期貨 資金進出警報】",
            "━━━━━━━━━━━━━━━━━━",
            f"📊 標的: {snap.label}",
            f"🧭 資金信號: {signal.label}",
            f"📈 最新報價: ${snap.close:,.2f} ({snap.pct_change:+.2f}%)",
            f"📦 成交量倍數: {_ratio(snap.vol_ratio_5d)}（5 日均）/ {_ratio(snap.vol_ratio_20d)}（20 日均）",
            f"📊 未平倉量變動: {oi_line}",
            "",
            "🔍 籌碼判定:",
            f"• {signal.verdict}",
            "━━━━━━━━━━━━━━━━━━",
        ]
    )


def _signed(value: int | None) -> str:
    return f"{value:+,}" if value is not None else "—"


def format_cftc_alert(
    position: CftcPosition,
    live: FuturesSnapshot | None,
    spread: dict | None,
) -> str:
    """CFTC 週度 Managed Money 持倉警報（標準格式）。"""
    change = position.mm_net_change
    if change is None:
        direction = "⚪ 淨持倉變動未知"
    elif change > 0:
        direction = f"🟢 淨多增倉 {change:+,} 口"
    else:
        direction = f"🔴 淨多減倉 {change:+,} 口"

    price_line = f"${live.close:,.2f} ({live.pct_change:+.2f}%)" if live else "—"
    volume_line = (
        f"{_ratio(live.vol_ratio_5d)}（5 日均）/ {_ratio(live.vol_ratio_20d)}（20 日均）"
        if live
        else "—"
    )
    if position.open_interest is not None and position.oi_change is not None:
        oi_line = f"總 OI {position.open_interest:,} 口（週變 {position.oi_change:+,}）"
    elif position.open_interest is not None:
        oi_line = f"總 OI {position.open_interest:,} 口"
    else:
        oi_line = "—"

    net_label = "淨多" if position.mm_net >= 0 else "淨空"
    verdict = (
        f"MM {net_label} {abs(position.mm_net):,} 口（{position.report_date} 週報）："
        f"多單 {position.mm_long:,}（週變 {_signed(position.mm_long_change)}）／"
        f"空單 {position.mm_short:,}（週變 {_signed(position.mm_short_change)}）"
    )
    if spread:
        verdict += f"；Brent-WTI 現貨價差 ${spread['spread']:.2f}（{spread['percentile']} 分位）"
    verdict += "。"

    return "\n".join(
        [
            "🚨 【NYMEX 原油期貨 資金進出警報】",
            "━━━━━━━━━━━━━━━━━━",
            f"📊 標的: CFTC 週報｜Managed Money｜{position.market}",
            f"🧭 資金信號: {direction}",
            f"📈 最新報價: {price_line}",
            f"📦 成交量倍數: {volume_line}",
            f"📊 未平倉量變動: {oi_line}",
            "",
            "🔍 籌碼判定:",
            f"• {verdict}",
            "━━━━━━━━━━━━━━━━━━",
        ]
    )


# --------------------------------------------------------------------- #
# 掃描流程
# --------------------------------------------------------------------- #
def run_flow_cycle(cfg, tools: "Tools", dry_run: bool = False, force: bool = False) -> int:
    """掃描 WTI / Brent 量價 OI 微觀結構；符合門檻才推播。回傳成功發送數。"""
    oi_history_all = tools.state.get_section(OI_HISTORY_SECTION)
    baselines = tools.state.get_section(BASELINE_SECTION)
    flags = tools.state.get_flags()
    today = et_today()
    state_dirty = False
    sent = 0

    for symbol, meta in SYMBOLS.items():
        label = meta["label"]
        oi_history = oi_history_all.get(symbol) or {}
        snapshot = fetch_snapshot(symbol, label, oi_history)
        if snapshot is None:
            log.warning("資金流：%s 快照取得失敗", label)
            continue

        signal = classify_flow(snapshot, cfg.flow_volume_ratio)
        log.info(
            "資金流：%s → %s（漲跌 %+.2f%%、量能 %s、ΔOI %s）",
            label,
            signal.label,
            snapshot.pct_change,
            _ratio(snapshot.vol_ratio),
            f"{snapshot.oi_change:+,}" if snapshot.oi_change is not None else "n/a",
        )

        # 持久化：每日 OI 快照 + 量能基準（乾跑不寫入）
        if not dry_run:
            if snapshot.open_interest is not None:
                oi_history[today] = snapshot.open_interest
                oi_history_all[symbol] = dict(sorted(oi_history.items())[-OI_HISTORY_MAX_DAYS:])
            baselines[symbol] = {
                "date": today,
                "session_date": snapshot.session_date,
                "volume": snapshot.volume,
                "avg5": snapshot.avg_volume_5d,
                "avg20": snapshot.avg_volume_20d,
            }
            state_dirty = True

        flag_key = f"{FLOW_FLAG_PREFIX}:{symbol}"
        already = str(flags.get(flag_key, ""))
        strong = signal.is_strong or abs(snapshot.pct_change) >= cfg.flow_move_alert_pct
        if not force and (not strong or already.startswith(f"{snapshot.session_date}|")):
            log.info("資金流：%s 未達推播門檻或本交易日已推播，略過", label)
            continue

        log.info("資金流：%s 觸發推播 → %s", label, signal.label)
        alert = format_flow_alert(signal)
        if tools.tg.send_alert(alert, dry_run=dry_run):
            sent += 1
            if not dry_run:
                tools.state.update_flags({flag_key: f"{snapshot.session_date}|{signal.code}"})

    if not dry_run and state_dirty:
        tools.state.set_section(OI_HISTORY_SECTION, oi_history_all)
        tools.state.set_section(BASELINE_SECTION, baselines)
    return sent


def run_cftc_cycle(cfg, tools: "Tools", dry_run: bool = False, force: bool = False) -> int:
    """檢查 CFTC 週度 Managed Money 持倉；新報告且 |ΔNet| 超門檻才推播。"""
    positions = get_positions(cfg)
    if not positions:
        log.warning("CFTC：無法取得任何市場持倉資料")
        return 0

    try:
        spread_context = tools.spreads.get_market_context()
    except Exception:
        spread_context = None

    flags = tools.state.get_flags()
    sent = 0
    for symbol, position in positions.items():
        change = position.mm_net_change
        flag_key = f"{CFTC_FLAG_PREFIX}:{symbol}"
        is_new = str(flags.get(flag_key, "")) != position.report_date

        if not force:
            if not is_new:
                log.info("CFTC：%s 已處理（報告日 %s）", position.market, position.report_date)
                continue
            if change is None or abs(change) < cfg.cftc_flow_threshold:
                log.info(
                    "CFTC：%s 週變 %s 未達門檻（%s 口），僅記錄進度",
                    position.market,
                    f"{change:+,}" if change is not None else "n/a",
                    f"{cfg.cftc_flow_threshold:,}",
                )
                if not dry_run:
                    tools.state.update_flags({flag_key: position.report_date})
                continue

        log.info(
            "CFTC：%s 觸發推播（週變 %s，報告日 %s）",
            position.market,
            f"{change:+,}" if change is not None else "n/a",
            position.report_date,
        )
        live: FuturesSnapshot | None = None
        try:
            oi_history = tools.state.get_section(OI_HISTORY_SECTION).get(symbol) or {}
            live = fetch_snapshot(symbol, SYMBOLS.get(symbol, {}).get("label", symbol), oi_history)
        except Exception:  # noqa: BLE001
            log.warning("CFTC：%s 即時量價快照取得失敗（將留白）", position.market)

        alert = format_cftc_alert(position, live, spread_context)
        if tools.tg.send_alert(alert, dry_run=dry_run):
            sent += 1
            if not dry_run:
                tools.state.update_flags({flag_key: position.report_date})
    return sent
