"""NYMEX 原油期貨量價與未平倉量（OI）微觀結構資料（資料源：yfinance）。

輸出：
- `FuturesSnapshot`：最新「已完成」交易日的收盤、漲跌幅、量能倍數（5/20 日均）、
  目前 OI 與相對前次快照的 Δ OI。
- `FlowSignal`：依「價 × 量 × OI」規則判定的資金流向
  （Long Buildup / Short Buildup / Short Covering / Long Liquidation）。

注意：yfinance `info` 僅提供「即時」OI 快照（無歷史序列），
Δ OI 依賴 StateStore 持久化的每日 OI 紀錄逐日累積。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

SYMBOLS: dict[str, dict[str, str]] = {
    "CL=F": {
        "label": "NYMEX WTI 主力合約 (CL)",
        "short": "WTI",
        "cftc_code": "067651",
    },
    "BZ=F": {
        "label": "Brent 主力合約 (BZ)",
        "short": "Brent",
        "cftc_code": "06765T",
    },
}

# 規格：量能倍數 >= 1.5x 視為「放量」
VOLUME_RATIO_THRESHOLD = 1.5


def et_today() -> str:
    """美東今日日期（OI 快照紀錄鍵）。"""
    return datetime.now(ET).date().isoformat()


@dataclass
class FuturesSnapshot:
    symbol: str
    label: str
    session_date: str  # 評估基準：最後一個已完成交易日（美東）
    close: float
    prev_close: float
    pct_change: float  # 相對前一交易日（%）
    volume: float
    vol_ratio_5d: float | None
    vol_ratio_20d: float | None
    avg_volume_5d: float | None
    avg_volume_20d: float | None
    open_interest: int | None
    oi_change: int | None
    oi_prev_date: str | None

    @property
    def vol_ratio(self) -> float | None:
        """判定用主量能倍數（優先 5 日均）。"""
        if self.vol_ratio_5d is not None:
            return self.vol_ratio_5d
        return self.vol_ratio_20d


@dataclass
class FlowSignal:
    snapshot: FuturesSnapshot
    code: str  # long_buildup / short_buildup / short_covering / long_liquidation / ...
    label: str  # 例：🟢 多頭主力增倉
    verdict: str  # 客觀、含數字的一句話研判

    @property
    def is_strong(self) -> bool:
        return self.code in ("long_buildup", "short_buildup", "volume_up", "volume_down")


def fetch_snapshot(
    symbol: str,
    label: str,
    oi_history: dict[str, int] | None = None,
) -> FuturesSnapshot | None:
    """抓取單一商品快照；任何失敗皆回傳 None（不讓單一商品中斷整輪）。"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="3mo", interval="1d", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 歷史資料取得失敗：%s", symbol, exc)
        return None

    if hist is None or hist.empty or len(hist) < 6:
        log.warning("%s 歷史資料不足（%s 筆）", symbol, 0 if hist is None else len(hist))
        return None

    hist = hist[hist["Close"].notna()]
    if len(hist) < 6:
        return None

    # 最後一列若為「當日未完成」K 線，訊號改用前一個已完成交易日
    last_index = hist.index[-1]
    last_date = (
        last_index.tz_convert(ET).date()
        if getattr(last_index, "tzinfo", None) is not None
        else last_index.date()
    )
    is_live = last_date == datetime.now(ET).date()
    completed = hist.iloc[:-1] if (is_live and len(hist) > 7) else hist
    if len(completed) < 6:
        completed = hist

    row = completed.iloc[-1]
    prev_row = completed.iloc[-2]
    session_index = completed.index[-1]
    session_date = (
        session_index.tz_convert(ET).strftime("%Y-%m-%d")
        if getattr(session_index, "tzinfo", None) is not None
        else session_index.strftime("%Y-%m-%d")
    )

    close = float(row["Close"])
    prev_close = float(prev_row["Close"])
    pct_change = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
    volume = float(row["Volume"] or 0.0)

    prior_volumes = completed.iloc[:-1]["Volume"].dropna()
    avg5 = float(prior_volumes.tail(5).mean()) if len(prior_volumes) >= 3 else None
    avg20 = float(prior_volumes.tail(20).mean()) if len(prior_volumes) >= 10 else None
    ratio5 = (volume / avg5) if (avg5 and avg5 > 0) else None
    ratio20 = (volume / avg20) if (avg20 and avg20 > 0) else None

    # 即時 OI（單次 info 呼叫；失敗不影響其他欄位）
    open_interest: int | None = None
    try:
        info = ticker.info or {}
        raw_oi = info.get("openInterest")
        if raw_oi is not None:
            open_interest = int(raw_oi)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s info 取得失敗（OI 暫不可用）：%s", symbol, exc)

    # Δ OI：與「最近一筆非今日」的持久化快照比較
    oi_change: int | None = None
    oi_prev_date: str | None = None
    if open_interest is not None and oi_history:
        today = et_today()
        for prev_date in sorted(oi_history.keys(), reverse=True):
            if prev_date == today:
                continue
            oi_change = open_interest - int(oi_history[prev_date])
            oi_prev_date = prev_date
            break

    return FuturesSnapshot(
        symbol=symbol,
        label=label,
        session_date=session_date,
        close=close,
        prev_close=prev_close,
        pct_change=pct_change,
        volume=volume,
        vol_ratio_5d=ratio5,
        vol_ratio_20d=ratio20,
        avg_volume_5d=avg5,
        avg_volume_20d=avg20,
        open_interest=open_interest,
        oi_change=oi_change,
        oi_prev_date=oi_prev_date,
    )


def classify_flow(snapshot: FuturesSnapshot, vol_threshold: float = VOLUME_RATIO_THRESHOLD) -> FlowSignal:
    """依「價 × 量 × OI」規則判定資金流型態（純規則、無 LLM）。"""
    up = snapshot.pct_change > 0
    ratio = snapshot.vol_ratio
    high_volume = ratio is not None and ratio >= vol_threshold
    oi = snapshot.oi_change
    oi_up = oi is not None and oi > 0
    oi_down = oi is not None and oi < 0

    vol_txt = f"{ratio:.2f}x" if ratio is not None else "無基準"
    oi_txt = f"{oi:+,} 口" if oi is not None else "未確認"
    base = f"{snapshot.session_date} 收盤 {snapshot.pct_change:+.2f}%、量能 {vol_txt}"

    # --- 放量（>= 門檻）---
    if high_volume and up:
        if oi_up:
            return FlowSignal(
                snapshot, "long_buildup", "🟢 多頭主力增倉",
                f"{base}、OI 擴張 {oi_txt} → 價漲量增且持倉上升，判定為多頭主力增倉（Long Buildup）。",
            )
        if oi is None:
            return FlowSignal(
                snapshot, "long_buildup", "🟢 多頭主力增倉（OI 未確認）",
                f"{base} → 價漲量增；OI 基準累積中，先以量價判定為多頭增倉。",
            )
        return FlowSignal(
            snapshot, "volume_up", "⚪ 放量上行（OI 縮減）",
            f"{base}、OI {oi_txt} → 價漲量增但持倉未擴張，較符合空頭回補特徵，追價風險偏高。",
        )
    if high_volume and not up:
        if oi_up:
            return FlowSignal(
                snapshot, "short_buildup", "🔴 空頭資金砸盤",
                f"{base}、OI 擴張 {oi_txt} → 價跌量增且持倉上升，判定為空頭主動打壓（Short Buildup）。",
            )
        if oi is None:
            return FlowSignal(
                snapshot, "short_buildup", "🔴 空頭資金砸盤（OI 未確認）",
                f"{base} → 價跌量增；OI 基準累積中，先以量價判定為空頭打壓。",
            )
        return FlowSignal(
            snapshot, "volume_down", "⚪ 放量下行（OI 縮減）",
            f"{base}、OI {oi_txt} → 價跌量增但持倉未擴張，偏向多頭平倉壓力，留意止跌訊號。",
        )

    # --- 縮量（未達門檻）---
    if up and oi_down:
        return FlowSignal(
            snapshot, "short_covering", "⚪ 空頭被動回補",
            f"{base}、OI 縮減 {oi_txt} → 價漲量縮伴隨持倉下降，判定為空頭回補（Short Covering）。",
        )
    if (not up) and oi_down:
        return FlowSignal(
            snapshot, "long_liquidation", "⚪ 多頭止損平倉",
            f"{base}、OI 縮減 {oi_txt} → 價跌量縮伴隨持倉下降，判定為多頭平倉（Long Liquidation）。",
        )
    if up:
        return FlowSignal(
            snapshot, "quiet_up", "⚪ 縮量反彈",
            f"{base}、OI {oi_txt} → 量能未達 {vol_threshold:.1f}x，反彈力道存疑（縮量反彈）。",
        )
    return FlowSignal(
        snapshot, "quiet_down", "⚪ 縮量陰跌",
        f"{base}、OI {oi_txt} → 量能未達 {vol_threshold:.1f}x，下跌動能有限（縮量陰跌）。",
    )
