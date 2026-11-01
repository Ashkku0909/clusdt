"""多周期市场上下文（最不滞后数据）：ADX、CVD、OI 增减仓、资金费率。

设计原则：
- 只用「成交与持仓」数据，不用 MACD/RSI 等滞后指标做决策
- CVD 由 K 线内建 taker buy volume（index 9）计算，无需额外请求
- OI 由 Binance openInterestHist（15m）计算 15 分钟 / 1 小时变化
- 评分（grade_signal）完全确定性，可单元测试；供訂單流引擎做訊號分級
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

REST_BASE = "https://fapi.binance.com"
FRAME_INTERVALS = ("1h", "4h", "1d")
CVD_BARS = 20
OI_PERIOD = "15m"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "context"
CACHE_MAX_AGE_SEC = 6 * 3600.0
FLOW_META_TTL_SEC = 3600.0  # OI / 資金費率 快取（大幅降低 418 風險）
_FLOW_META: dict[str, tuple[float, float, float, float, float]] = {}

TREND_ZH = {"up": "多頭", "down": "空頭", "range": "震盪"}


def trend_zh(frame: FrameContext | None) -> str:
    """卡片用：多頭 / 空頭 / 震盪。"""
    if frame is None:
        return "震盪"
    return TREND_ZH.get(frame.trend, "震盪")

OI_MATRIX_LABELS = {
    "long_buildup": "多頭增倉",
    "short_buildup": "空頭增倉",
    "short_covering": "空頭回補",
    "long_liquidation": "多頭平倉",
    "neutral": "持倉中性",
}


@dataclass(slots=True)
class FrameContext:
    interval: str
    adx: float
    plus_di: float
    minus_di: float
    atr: float
    cvd_usd: float
    price_change_pct: float
    trend: str  # up / down / range


@dataclass(slots=True)
class MarketContext:
    symbol: str
    price: float
    funding_rate: float  # 小數（0.0001 = 0.01%）
    oi_now_usd: float
    oi_change_15m_pct: float
    oi_change_1h_pct: float
    oi_matrix: str
    oi_matrix_label: str
    frames: dict[str, FrameContext] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def frame(self, interval: str) -> FrameContext | None:
        return self.frames.get(interval)


# --------------------------------------------------------------------- #
# 指標計算（純函數，可測試）
# --------------------------------------------------------------------- #
def compute_adx(klines: list[list], period: int = 14) -> tuple[float, float, float]:
    """ADX(14)（Wilder 平滑）→ (adx, plus_di, minus_di)。"""
    if len(klines) < period * 2 + 1:
        raise ValueError("K 線不足，無法計算 ADX")
    highs = [float(row[2]) for row in klines]
    lows = [float(row[3]) for row in klines]
    closes = [float(row[4]) for row in klines]

    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index in range(1, len(klines)):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )

    def wilder(seq: list[float]) -> list[float]:
        smoothed = [sum(seq[:period])]
        for value in seq[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + value)
        return smoothed

    tr_smooth = wilder(trs)
    plus_smooth = wilder(plus_dm)
    minus_smooth = wilder(minus_dm)
    plus_di = [100.0 * p / t if t else 0.0 for p, t in zip(plus_smooth, tr_smooth)]
    minus_di = [100.0 * m / t if t else 0.0 for m, t in zip(minus_smooth, tr_smooth)]
    dx = [
        100.0 * abs(p - m) / (p + m) if (p + m) else 0.0
        for p, m in zip(plus_di, minus_di)
    ]
    if len(dx) < period:
        raise ValueError("DX 序列不足，無法計算 ADX")
    adx = sum(dx[:period]) / period
    for value in dx[period:]:
        adx = (adx * (period - 1) + value) / period
    return adx, plus_di[-1], minus_di[-1]


def compute_cvd_usd(klines: list[list], bars: int = CVD_BARS) -> float:
    """近 N 根 K 線的主動買賣差（美元）= Σ (2×takerBuy − volume) × close。"""
    rows = klines[-bars:]
    total = 0.0
    for row in rows:
        volume = float(row[5])
        taker_buy = float(row[9])
        close = float(row[4])
        total += (2.0 * taker_buy - volume) * close
    return total


def compute_atr_simple(klines: list[list], period: int = 14) -> float:
    true_ranges: list[float] = []
    prev_close: float | None = None
    for row in klines[-(period + 40):]:
        high, low, close = float(row[2]), float(row[3]), float(row[4])
        if prev_close is None:
            true_ranges.append(high - low)
        else:
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    if len(true_ranges) < period:
        raise ValueError("K 線不足，無法計算 ATR")
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr += (tr - atr) / period
    return atr


def classify_oi_matrix(price_change_pct: float, oi_change_pct: float, threshold: float = 0.1) -> str:
    """OI × 價格 四象限（機構持倉讀法）。"""
    if oi_change_pct >= threshold and price_change_pct > 0:
        return "long_buildup"
    if oi_change_pct >= threshold and price_change_pct < 0:
        return "short_buildup"
    if oi_change_pct <= -threshold and price_change_pct > 0:
        return "short_covering"
    if oi_change_pct <= -threshold and price_change_pct < 0:
        return "long_liquidation"
    return "neutral"


# --------------------------------------------------------------------- #
# 抓取（阻塞；請以 to_thread 呼叫）
# --------------------------------------------------------------------- #
def _json(session: requests.Session, path: str, params: dict, timeout=(6.0, 20.0)):
    response = session.get(f"{REST_BASE}{path}", params=params, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"Binance HTTP {response.status_code}（{path}）：{response.text[:120]}")
    return response.json()


def save_context_cache(context: MarketContext, directory: Path | None = None) -> None:
    """把成功的上下文寫到磁碟，供限流／重啟期間沿用（附「N 分鐘前」標記）。"""
    target_dir = directory or CACHE_DIR
    payload = {
        "symbol": context.symbol,
        "price": context.price,
        "funding_rate": context.funding_rate,
        "oi_now_usd": context.oi_now_usd,
        "oi_change_15m_pct": context.oi_change_15m_pct,
        "oi_change_1h_pct": context.oi_change_1h_pct,
        "oi_matrix": context.oi_matrix,
        "oi_matrix_label": context.oi_matrix_label,
        "fetched_at": context.fetched_at.isoformat(),
        "frames": {
            interval: {
                "interval": frame.interval,
                "adx": frame.adx,
                "plus_di": frame.plus_di,
                "minus_di": frame.minus_di,
                "atr": frame.atr,
                "cvd_usd": frame.cvd_usd,
                "price_change_pct": frame.price_change_pct,
                "trend": frame.trend,
            }
            for interval, frame in context.frames.items()
        },
    }
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{context.symbol}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:  # noqa: BLE001 - 快取失敗不影響主流程
        log.debug("%s 上下文快取寫入失敗：%s", context.symbol, exc)


def load_context_cache(
    symbol: str, max_age_sec: float = CACHE_MAX_AGE_SEC, directory: Path | None = None
) -> MarketContext | None:
    """讀回磁碟快取；超過 max_age_sec 視為過期回傳 None。"""
    path = (directory or CACHE_DIR) / f"{symbol}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError):
        return None
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age > max_age_sec:
        return None
    try:
        frames = {
            interval: FrameContext(
                interval=row["interval"],
                adx=float(row["adx"]),
                plus_di=float(row["plus_di"]),
                minus_di=float(row["minus_di"]),
                atr=float(row["atr"]),
                cvd_usd=float(row["cvd_usd"]),
                price_change_pct=float(row["price_change_pct"]),
                trend=str(row["trend"]),
            )
            for interval, row in payload["frames"].items()
        }
        return MarketContext(
            symbol=payload["symbol"],
            price=float(payload["price"]),
            funding_rate=float(payload["funding_rate"]),
            oi_now_usd=float(payload["oi_now_usd"]),
            oi_change_15m_pct=float(payload["oi_change_15m_pct"]),
            oi_change_1h_pct=float(payload["oi_change_1h_pct"]),
            oi_matrix=str(payload["oi_matrix"]),
            oi_matrix_label=str(payload["oi_matrix_label"]),
            frames=frames,
            fetched_at=fetched_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


def fetch_market_context(
    session: requests.Session, symbol: str, bars: int = 120, include_flow: bool = True
) -> MarketContext:
    """抓取多周期上下文。

    include_flow=False 時不呼叫 openInterestHist / premiumIndex（最容易觸發 418 的兩個端點），
    直接用 1 小時內快取；沒有快取則以 0 代。引擎只在每小時第一次刷新時開啟它們。
    """
    frames: dict[str, FrameContext] = {}
    price = 0.0
    price_change_1h = 0.0
    for interval in FRAME_INTERVALS:
        klines = _json(session, "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": bars})
        if len(klines) < 40:
            raise RuntimeError(f"{symbol} {interval} K 線不足")
        adx, plus_di, minus_di = compute_adx(klines)
        atr = compute_atr_simple(klines)
        cvd = compute_cvd_usd(klines)
        closes = [float(row[4]) for row in klines]
        move = (closes[-1] - closes[0]) / closes[0] * 100.0 if closes[0] else 0.0
        if interval == "1h":
            price = closes[-1]
            ref = closes[-5] if len(closes) >= 5 else closes[0]
            price_change_1h = (closes[-1] - ref) / ref * 100.0 if ref else 0.0
        if adx >= 25 and plus_di > minus_di:
            trend = "up"
        elif adx >= 25 and minus_di > plus_di:
            trend = "down"
        else:
            trend = "range"
        frames[interval] = FrameContext(
            interval=interval,
            adx=adx,
            plus_di=plus_di,
            minus_di=minus_di,
            atr=atr,
            cvd_usd=cvd,
            price_change_pct=move,
            trend=trend,
        )

    oi_now = oi_15m = oi_1h = 0.0
    funding = 0.0
    cached_meta = _FLOW_META.get(symbol)
    meta_fresh = cached_meta is not None and (time.time() - cached_meta[0]) <= FLOW_META_TTL_SEC
    if cached_meta is not None and (meta_fresh or not include_flow):
        _, oi_now, oi_15m, oi_1h, funding = cached_meta
    if include_flow and not meta_fresh:
        try:
            oi_rows = _json(
                session,
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": OI_PERIOD, "limit": 25},
            )
            oi_values = [
                float(row["sumOpenInterestValue"]) for row in oi_rows if row.get("sumOpenInterestValue")
            ]
            oi_now = oi_values[-1] if oi_values else 0.0
            oi_15m = (
                (oi_values[-1] - oi_values[-2]) / oi_values[-2] * 100.0
                if len(oi_values) >= 2 and oi_values[-2]
                else 0.0
            )
            oi_1h = (
                (oi_values[-1] - oi_values[-5]) / oi_values[-5] * 100.0
                if len(oi_values) >= 5 and oi_values[-5]
                else 0.0
            )
            premium = _json(session, "/fapi/v1/premiumIndex", {"symbol": symbol})
            funding = float(premium.get("lastFundingRate") or 0.0)
            _FLOW_META[symbol] = (time.time(), oi_now, oi_15m, oi_1h, funding)
        except Exception as exc:  # noqa: BLE001 - OI/資金費率非必要，失敗用舊值/0
            log.debug("%s OI/資金費率更新失敗（沿用快取）：%s", symbol, exc)

    matrix = classify_oi_matrix(price_change_1h, oi_1h)
    return MarketContext(
        symbol=symbol,
        price=price,
        funding_rate=funding,
        oi_now_usd=oi_now,
        oi_change_15m_pct=oi_15m,
        oi_change_1h_pct=oi_1h,
        oi_matrix=matrix,
        oi_matrix_label=OI_MATRIX_LABELS.get(matrix, matrix),
        frames=frames,
    )


# --------------------------------------------------------------------- #
# 訊號評分（A/B/C；確定性）
# --------------------------------------------------------------------- #
GRADE_RANK = {"—": 2, "C": 0, "B": 1, "A": 2}
GRADE_A_SCORE = 82
GRADE_B_SCORE = 70
STALE_CONTEXT_PENALTY_MIN = 20  # 上下文超過 20 分鐘 → −10 分


def grade_signal(
    context: MarketContext | None,
    direction: str,
    kind: str,
    context_age: float | None = None,
) -> tuple[int, str, list[str]]:
    """回傳 (分數 0-100, 等級 A/B/C/—, 理由列表)；完全確定性。"""
    if context is None:
        return 0, "—", []
    score = 55
    reasons: list[str] = []
    frame_near = context.frame("1h")
    frame1h = context.frame("1h")
    frame4h = context.frame("4h")
    frame1d = context.frame("1d")

    # 高周期趨勢對齊（1h 主、4h 輔、1d 為背景）
    for frame in (frame1h, frame4h):
        if frame is None or frame.trend == "range":
            continue
        aligned = (frame.trend == "up" and direction == "long") or (frame.trend == "down" and direction == "short")
        if aligned:
            score += 12
            reasons.append(f"{frame.interval} 趨勢{'多' if frame.trend == 'up' else '空'}")
        else:
            score -= 12
            reasons.append(f"{frame.interval} 趨勢反向")

    # 硬否決：1d 與 4h 同時反向 → 逆勢單直接降為 C
    counter_trend = False
    if frame1d is not None and frame4h is not None:
        both_up = frame1d.trend == "up" and frame4h.trend == "up"
        both_down = frame1d.trend == "down" and frame4h.trend == "down"
        if (direction == "short" and both_up) or (direction == "long" and both_down):
            counter_trend = True
            reasons.append("1d+4h 逆勢否決")

    # CVD（1h 帧）方向
    cvd_aligned: bool | None = None
    if frame_near is not None and abs(frame_near.cvd_usd) > 0:
        cvd_aligned = (frame_near.cvd_usd > 0 and direction == "long") or (
            frame_near.cvd_usd < 0 and direction == "short"
        )
        if cvd_aligned:
            score += 10
            reasons.append("CVD 同向")
        else:
            score -= 8
            reasons.append("CVD 反向")

    # 硬否決：momentum / sweep 不得逆 CVD
    cvd_veto = kind in ("momentum", "sweep") and cvd_aligned is False

    # OI 增减仓矩阵
    oi_bonus = {
        ("long_buildup", "long"): 10,
        ("short_buildup", "short"): 10,
        ("short_covering", "long"): 5,
        ("long_liquidation", "short"): 5,
        ("long_liquidation", "long"): -10,
        ("short_covering", "short"): -10,
    }.get((context.oi_matrix, direction), 0)
    if oi_bonus:
        score += oi_bonus
    reasons.append(f"OI {context.oi_matrix_label}")

    # 资金费率拥挤度
    if direction == "long" and context.funding_rate > 0.0005:
        score -= 10
        reasons.append("資金費率偏多（擁擠）")
    elif direction == "short" and context.funding_rate < -0.0005:
        score -= 10
        reasons.append("資金費率偏空（擁擠）")

    # 信号类型加分与震荡抑制
    score += {"absorption": 8, "momentum": 6, "divergence": 4}.get(kind, 0)
    if frame_near is not None:
        if frame_near.adx < 18:
            score -= 20
            reasons.append(f"ADX1h {frame_near.adx:.0f}（震盪）")
        elif frame_near.adx >= 25:
            score += 5
            reasons.append(f"ADX1h {frame_near.adx:.0f}（趨勢）")

    # 上下文陳舊扣分（不信任舊數據的邊緣評分）
    if context_age is not None and context_age > STALE_CONTEXT_PENALTY_MIN * 60:
        score -= 10
        reasons.append(f"上下文 {context_age / 60:.0f} 分鐘前")

    score = max(0, min(100, int(round(score))))
    if counter_trend or cvd_veto:
        return score, "C", reasons
    grade = "A" if score >= GRADE_A_SCORE else ("B" if score >= GRADE_B_SCORE else "C")
    return score, grade, reasons


def context_line(context: MarketContext | None, reasons: list[str] | None = None) -> str:
    """內部偵錯用的一行上下文摘要（不再出現在付費頻道卡片）。"""
    if context is None:
        return "market context: unavailable"
    frame_near = context.frame("1h")
    frame1h = context.frame("1h")
    parts = []
    if frame_near is not None:
        parts.append(f"ADX1h {frame_near.adx:.0f}")
    if frame1h is not None:
        parts.append(f"1h {frame1h.trend.upper() if frame1h.trend != 'range' else 'RANGE'}")
    frame4h = context.frame("4h")
    if frame4h is not None:
        parts.append(f"4h {frame4h.trend.upper() if frame4h.trend != 'range' else 'RANGE'}")
    frame1d = context.frame("1d")
    if frame1d is not None:
        parts.append(f"1d {frame1d.trend.upper() if frame1d.trend != 'range' else 'RANGE'}")
    parts.append(f"CVD1h ${frame_near.cvd_usd / 1000:+,.0f}k" if frame_near else "CVD n/a")
    parts.append(f"OI {context.oi_matrix_label} {context.oi_change_1h_pct:+.2f}%")
    parts.append(f"資金費率 {context.funding_rate * 100:+.4f}%")
    if reasons:
        parts.append("、".join(reasons[:4]))
    return "｜".join(parts)
