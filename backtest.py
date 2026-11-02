"""VWAP ± σ 均值回歸策略回測器（4h 波段｜低波動體制專用）。

目的：在把 VWAP 策略接進實盤引擎之前，先用真實 4h K 線證明它有沒有邊際。
回測是「bar 級近似」：只驗證「低波動 + 觸及 σ 帶 + 回歸均值」是否具備統計優勢，
實盤引擎另外用逐筆成交量（CVD）做確認，因此回測結果屬於保守下限。

用法：
    python backtest.py                      # 23 檔標的，預設參數
    python backtest.py --symbols CLUSDT,XAUUSDT --refresh
    python backtest.py --grid               # 參數網格掃描
    python backtest.py --compare            # 有/無體制過濾對照
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import DEFAULT_SYMBOLS, DATA_DIR

REST_BASE = "https://fapi.binance.com"
KLINE_CACHE = DATA_DIR / "backtest"
INTERVAL = "4h"
BARS_PER_DAY = 6.0
FEE_PCT = 0.10  # taker 單邊 0.05% × 2
MAX_BARS = 1500


# --------------------------------------------------------------------- #
# 資料
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class Bar:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


def fetch_klines(
    session: requests.Session, symbol: str, limit: int = MAX_BARS, pages: int = 1
) -> list[Bar]:
    """抓取 4h K 線；pages > 1 時向前分頁（每頁上限 1500 根 ≈ 250 天）。"""
    collected: list[Bar] = []
    end_time: int | None = None
    for _ in range(max(1, pages)):
        params = {"symbol": symbol, "interval": INTERVAL, "limit": min(limit, 1500)}
        if end_time is not None:
            params["endTime"] = end_time
        response = session.get(f"{REST_BASE}/fapi/v1/klines", params=params, timeout=(8, 30))
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        page = [
            Bar(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                quote_volume=float(row[7]),
            )
            for row in rows
        ]
        collected = page + collected
        end_time = int(rows[0][0]) - 1
        if len(rows) < min(limit, 1500):
            break
        time.sleep(0.35)
    # 去重並依時間排序
    unique: dict[int, Bar] = {}
    for bar in collected:
        unique[bar.open_time] = bar
    return [unique[key] for key in sorted(unique)]


def load_bars(
    session: requests.Session, symbol: str, refresh: bool = False, pages: int = 1
) -> list[Bar]:
    KLINE_CACHE.mkdir(parents=True, exist_ok=True)
    path = KLINE_CACHE / f"{symbol}_{INTERVAL}_{pages}p.json"
    if path.exists() and not refresh:
        age = time.time() - path.stat().st_mtime
        if age < 6 * 3600:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return [Bar(**row) for row in raw]
            except (OSError, ValueError, TypeError):
                pass
    bars = fetch_klines(session, symbol, pages=pages)
    try:
        path.write_text(
            json.dumps([asdict(bar) for bar in bars]), encoding="utf-8"
        )
    except OSError:
        pass
    return bars


def walk_forward(bars_map: dict[str, list[Bar]], params: Params, split: float = 0.6) -> str:
    """樣本內／樣本外分割：同一組參數在兩個時期是否都有效。"""
    in_sample: list[Result] = []
    out_sample: list[Result] = []
    for symbol, bars in bars_map.items():
        cut = int(len(bars) * split)
        in_sample.append(run_symbol(symbol, bars[:cut], params))
        out_sample.append(run_symbol(symbol, bars[cut:], params))
    head = aggregate(in_sample, "前半（樣本內）")
    tail = aggregate(out_sample, "後半（樣本外）")
    return f"      {format_row(head)}\n      {format_row(tail)}"


# --------------------------------------------------------------------- #
# 指標（全部確定性、無外部依賴）
# --------------------------------------------------------------------- #
def wilder(values: list[float], period: int) -> list[float]:
    """Wilder 平滑（RMA）：前 period 個取簡單平均，其後遞推。"""
    out: list[float] = [0.0] * len(values)
    if len(values) < period:
        return out
    first = sum(values[:period]) / period
    out[period - 1] = first
    prev = first
    for index in range(period, len(values)):
        prev = (prev * (period - 1) + values[index]) / period
        out[index] = prev
    return out


def compute_atr(bars: list[Bar], period: int = 14) -> list[float]:
    trs = [0.0]
    for index in range(1, len(bars)):
        prev_close = bars[index - 1].close
        trs.append(
            max(
                bars[index].high - bars[index].low,
                abs(bars[index].high - prev_close),
                abs(bars[index].low - prev_close),
            )
        )
    return wilder(trs, period)


def compute_adx(bars: list[Bar], period: int = 14) -> list[float]:
    plus_dm = [0.0]
    minus_dm = [0.0]
    trs = [0.0]
    for index in range(1, len(bars)):
        up = bars[index].high - bars[index - 1].high
        down = bars[index - 1].low - bars[index].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        prev_close = bars[index - 1].close
        trs.append(
            max(
                bars[index].high - bars[index].low,
                abs(bars[index].high - prev_close),
                abs(bars[index].low - prev_close),
            )
        )
    atr = wilder(trs, period)
    smooth_plus = wilder(plus_dm, period)
    smooth_minus = wilder(minus_dm, period)
    dx: list[float] = []
    for index in range(len(bars)):
        if atr[index] <= 0:
            dx.append(0.0)
            continue
        plus_di = 100.0 * smooth_plus[index] / atr[index]
        minus_di = 100.0 * smooth_minus[index] / atr[index]
        total = plus_di + minus_di
        dx.append(100.0 * abs(plus_di - minus_di) / total if total > 0 else 0.0)
    # ADX 需對 DX 再平滑一次；前 period 個 DX 無效，以 0 填充後偏移
    smoothed = wilder(dx[period:], period)
    adx = [0.0] * period + smoothed
    return adx[: len(bars)] + [0.0] * max(0, len(bars) - len(adx))


def compute_vwap_bands(bars: list[Bar], window: int) -> tuple[list[float], list[float]]:
    """滾動成交量加權均值與成交量加權標準差（真正的 VWAP σ，非常態 SMA σ）。"""
    vwaps = [0.0] * len(bars)
    sigmas = [0.0] * len(bars)
    for index in range(window - 1, len(bars)):
        segment = bars[index - window + 1 : index + 1]
        total_vol = sum(bar.volume for bar in segment)
        if total_vol <= 0:
            continue
        typical = [(bar.high + bar.low + bar.close) / 3.0 for bar in segment]
        vwap = sum(tp * bar.volume for tp, bar in zip(typical, segment)) / total_vol
        variance = sum(
            bar.volume * (tp - vwap) ** 2 for tp, bar in zip(typical, segment)
        ) / total_vol
        vwaps[index] = vwap
        sigmas[index] = math.sqrt(max(variance, 0.0))
    return vwaps, sigmas


def percentile_rank(history: list[float], value: float) -> float:
    """value 在 history 中的百分位（0-1）；樣本不足回傳 1.0（保守：不視為低波動）。"""
    clean = [item for item in history if item > 0]
    if len(clean) < 20:
        return 1.0
    below = sum(1 for item in clean if item <= value)
    return below / len(clean)


# --------------------------------------------------------------------- #
# 參數與回測
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class Params:
    vwap_window: int = 20
    sigma_entry: float = 2.0
    sigma_stop: float = 0.6  # 停損 = 進場價 - sigma_stop × σ（順勢方向）
    tp_mode: str = "vwap"  # vwap | sigma1 | opposite
    adx_max: float = 20.0
    atr_pctile_max: float = 0.40
    atr_pctile_lookback: int = 100
    require_rejection: bool = True
    min_edge_pct: float = 0.0  # 進場價到 VWAP 的最小距離（%），確保 TP 值得做
    max_hold_bars: int = 12  # 4h × 12 = 2 天
    regime_filter: bool = True
    direction: str = "both"  # both | long | short
    entry_mode: str = "close"  # close（回歸確認後進）| limit（掛在 σ 帶上）
    trend_ma: int = 0  # > 0：只順著 SMA(trend_ma) 方向做回調
    min_band_width_pct: float = 0.0
    max_band_width_pct: float = 99.0
    min_risk_pct: float = 0.0  # 停損最小距離（模擬引擎的資產類別下限，對抗手續費侵蝕）
    min_target_pct: float = 0.0  # TP1 最小距離（太近的不做）


@dataclass(slots=True)
class Trade:
    symbol: str
    direction: str
    entry_index: int
    entry_time: int
    entry: float
    stop: float
    target: float
    exit_index: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    r_multiple: float = 0.0
    pnl_pct: float = 0.0


@dataclass(slots=True)
class Result:
    trades: list[Trade] = field(default_factory=list)

    def summary(self, label: str = "") -> dict:
        total = len(self.trades)
        if total == 0:
            return {"label": label, "trades": 0}
        wins = [t for t in self.trades if t.r_multiple > 0]
        losses = [t for t in self.trades if t.r_multiple <= 0]
        gross_win = sum(t.r_multiple for t in wins)
        gross_loss = -sum(t.r_multiple for t in losses)
        hold = [t.bars_held for t in self.trades]
        return {
            "label": label,
            "trades": total,
            "win_rate": 100.0 * len(wins) / total,
            "avg_r": sum(t.r_multiple for t in self.trades) / total,
            "expectancy_pct": sum(t.pnl_pct for t in self.trades) / total,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "avg_bars": statistics.mean(hold) if hold else 0.0,
            "tp_hits": sum(1 for t in self.trades if t.exit_reason == "tp"),
            "sl_hits": sum(1 for t in self.trades if t.exit_reason == "sl"),
            "timeouts": sum(1 for t in self.trades if t.exit_reason == "time"),
        }


def run_symbol(symbol: str, bars: list[Bar], params: Params) -> Result:
    result = Result()
    if len(bars) < params.vwap_window + params.atr_pctile_lookback + 20:
        return result

    atr = compute_atr(bars, 14)
    adx = compute_adx(bars, 14)
    vwaps, sigmas = compute_vwap_bands(bars, params.vwap_window)
    atr_pct_series = [
        (atr[i] / bars[i].close * 100.0) if atr[i] > 0 and bars[i].close > 0 else 0.0
        for i in range(len(bars))
    ]
    # 趨勢均線（僅在需要時計算；用真正的收盤 SMA，不含未來資料）
    trend_ma_series: list[float] = []
    if params.trend_ma > 0:
        closes = [bar.close for bar in bars]
        for index in range(len(bars)):
            if index + 1 < params.trend_ma:
                trend_ma_series.append(0.0)
            else:
                segment = closes[index + 1 - params.trend_ma : index + 1]
                trend_ma_series.append(sum(segment) / len(segment))

    index = params.vwap_window + 20
    while index < len(bars) - 1:
        bar = bars[index]
        # 關鍵：訊號必須只用「已收盤」的 K 棒計算（index-1），否則就是偷看未來
        prev = index - 1
        vwap, sigma = vwaps[prev], sigmas[prev]
        if vwap <= 0 or sigma <= 0:
            index += 1
            continue

        # --- 體制過濾：低波動 + 無趨勢 ---
        if params.regime_filter:
            history = atr_pct_series[max(0, prev - params.atr_pctile_lookback) : prev]
            pctile = percentile_rank(history, atr_pct_series[prev])
            if pctile > params.atr_pctile_max:
                index += 1
                continue
            if adx[prev] > params.adx_max:
                index += 1
                continue

        lower = vwap - params.sigma_entry * sigma
        upper = vwap + params.sigma_entry * sigma
        band_width_pct = 2.0 * params.sigma_entry * sigma / vwap * 100.0
        if not (
            params.min_band_width_pct <= band_width_pct <= params.max_band_width_pct
        ):
            index += 1
            continue
        trend_ma_value = trend_ma_series[prev] if trend_ma_series else 0.0
        trend_long_ok = params.trend_ma <= 0 or (
            trend_ma_value > 0 and bar.open > trend_ma_value
        )
        trend_short_ok = params.trend_ma <= 0 or (
            trend_ma_value > 0 and bar.open < trend_ma_value
        )
        direction = ""
        if (
            params.direction in ("both", "long")
            and trend_long_ok
            and bar.low <= lower
        ):
            if not params.require_rejection or bar.close > lower:
                direction = "long"
        if (
            not direction
            and params.direction in ("both", "short")
            and trend_short_ok
            and bar.high >= upper
        ):
            if not params.require_rejection or bar.close < upper:
                direction = "short"
        if not direction:
            index += 1
            continue

        # 進場價：limit = 掛在 σ 帶上成交；close = 等 K 棒收盤確認回歸
        if params.entry_mode == "limit":
            entry = lower if direction == "long" else upper
        else:
            entry = bar.close
        if direction == "long":
            edge_pct = (vwap - entry) / entry * 100.0
            stop = entry - params.sigma_stop * sigma
            if params.tp_mode == "vwap":
                target = vwap
            elif params.tp_mode == "sigma1":
                target = vwap + 1.0 * sigma
            else:
                target = vwap + params.sigma_entry * sigma
        else:
            edge_pct = (entry - vwap) / entry * 100.0
            stop = entry + params.sigma_stop * sigma
            if params.tp_mode == "vwap":
                target = vwap
            elif params.tp_mode == "sigma1":
                target = vwap - 1.0 * sigma
            else:
                target = vwap - params.sigma_entry * sigma

        risk = abs(entry - stop)
        if params.min_risk_pct > 0:
            floor = params.min_risk_pct / 100.0 * entry
            if risk < floor:
                risk = floor
                stop = entry - risk if direction == "long" else entry + risk
        if params.min_target_pct > 0:
            if abs(target - entry) / entry * 100.0 < params.min_target_pct:
                index += 1
                continue
        if risk <= 0 or edge_pct < params.min_edge_pct:
            index += 1
            continue
        if direction == "long" and target <= entry:
            index += 1
            continue
        if direction == "short" and target >= entry:
            index += 1
            continue

        trade = Trade(
            symbol=symbol,
            direction=direction,
            entry_index=index,
            entry_time=bar.open_time,
            entry=entry,
            stop=stop,
            target=target,
        )

        # --- 掛單成交於本根 K 棒：本根剩餘走勢同樣可能直接觸停損（保守，不可忽略）---
        if params.entry_mode == "limit":
            if (direction == "long" and bar.low <= stop) or (
                direction == "short" and bar.high >= stop
            ):
                net_pct = -risk / entry * 100.0 - FEE_PCT
                trade.exit_price = stop
                trade.exit_reason = "sl_same_bar"
                trade.exit_index = index
                trade.bars_held = 0
                trade.pnl_pct = net_pct
                trade.r_multiple = net_pct / (risk / entry * 100.0)
                result.trades.append(trade)
                index += 1
                continue

        # --- 逐 bar 前進模擬出場（同 bar 同時觸及時假設先觸停損，保守）---
        exit_price = entry
        exit_reason = "time"
        held = params.max_hold_bars
        for offset in range(1, params.max_hold_bars + 1):
            cursor = index + offset
            if cursor >= len(bars):
                held = offset - 1
                exit_price = bars[-1].close
                exit_reason = "time"
                break
            row = bars[cursor]
            if direction == "long":
                if row.low <= stop:
                    exit_price, exit_reason, held = stop, "sl", offset
                    break
                if row.high >= target:
                    exit_price, exit_reason, held = target, "tp", offset
                    break
            else:
                if row.high >= stop:
                    exit_price, exit_reason, held = stop, "sl", offset
                    break
                if row.low <= target:
                    exit_price, exit_reason, held = target, "tp", offset
                    break
            exit_price = row.close
        else:
            exit_reason = "time"

        raw_pct = (
            (exit_price - entry) / entry * 100.0
            if direction == "long"
            else (entry - exit_price) / entry * 100.0
        )
        net_pct = raw_pct - FEE_PCT
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.exit_index = index + held
        trade.bars_held = held
        trade.pnl_pct = net_pct
        trade.r_multiple = net_pct / (risk / entry * 100.0) if risk > 0 else 0.0
        result.trades.append(trade)

        # 同一標的持倉期間不重複進場
        index = min(index + held, len(bars) - 1) + 1

    return result


def aggregate(results: list[Result], label: str) -> dict:
    merged = Result()
    for result in results:
        merged.trades.extend(result.trades)
    return merged.summary(label)


def format_row(row: dict) -> str:
    if not row.get("trades"):
        return f"{row.get('label', ''):<28} 0 筆"
    pf = row["profit_factor"]
    pf_text = "∞" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"{row['label']:<28} {row['trades']:>4} 筆  "
        f"勝率 {row['win_rate']:>5.1f}%  "
        f"平均R {row['avg_r']:>+5.2f}  "
        f"期望值 {row['expectancy_pct']:>+5.2f}%  "
        f"PF {pf_text:>5}  "
        f"均持倉 {row['avg_bars']:>4.1f} 根  "
        f"TP/SL/超時 {row['tp_hits']}/{row['sl_hits']}/{row['timeouts']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="VWAP ± σ 均值回歸回測")
    parser.add_argument("--symbols", default="", help="逗號分隔（預設全部 23 檔）")
    parser.add_argument("--refresh", action="store_true", help="強制重新下載 K 線")
    parser.add_argument("--grid", action="store_true", help="參數網格掃描")
    parser.add_argument("--compare", action="store_true", help="有/無體制過濾對照")
    parser.add_argument("--sigma-entry", type=float, default=2.0)
    parser.add_argument("--sigma-stop", type=float, default=0.6)
    parser.add_argument("--adx-max", type=float, default=20.0)
    parser.add_argument("--atr-pctile", type=float, default=0.40)
    parser.add_argument("--vwap-window", type=int, default=20)
    parser.add_argument("--hold", type=int, default=12)
    parser.add_argument("--min-edge", type=float, default=0.0)
    parser.add_argument("--trend-ma", type=int, default=0)
    parser.add_argument("--entry-mode", default="close", choices=("close", "limit"))
    parser.add_argument("--min-risk", type=float, default=0.0, help="停損最小距離（%%）")
    parser.add_argument("--min-target", type=float, default=0.0, help="TP 最小距離（%%）")
    parser.add_argument("--pages", type=int, default=1, help="向前分頁数（每頁 250 天）")
    parser.add_argument("--variants", action="store_true", help="比較不同策略假設")
    args = parser.parse_args()

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    symbols = symbols or list(DEFAULT_SYMBOLS)

    session = requests.Session()
    bars_map: dict[str, list[Bar]] = {}
    for symbol in symbols:
        try:
            bars = load_bars(session, symbol, refresh=args.refresh, pages=args.pages)
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol} K 線取得失敗：{exc}")
            continue
        if len(bars) < 200:
            print(f"  {symbol} K 線不足（{len(bars)} 根），略過")
            continue
        bars_map[symbol] = bars
        span_days = len(bars) * 4 / 24
        print(
            f"  {symbol:<10} {len(bars):>4} 根 ≈ {span_days:>5.0f} 天  "
            f"({datetime.fromtimestamp(bars[0].open_time / 1000, timezone.utc):%Y-%m-%d} → "
            f"{datetime.fromtimestamp(bars[-1].open_time / 1000, timezone.utc):%Y-%m-%d})"
        )
    if not bars_map:
        print("沒有可用資料")
        return 1

    base = Params(
        vwap_window=args.vwap_window,
        sigma_entry=args.sigma_entry,
        sigma_stop=args.sigma_stop,
        adx_max=args.adx_max,
        atr_pctile_max=args.atr_pctile,
        max_hold_bars=args.hold,
        min_edge_pct=args.min_edge,
        trend_ma=args.trend_ma,
        entry_mode=args.entry_mode,
        min_risk_pct=args.min_risk,
        min_target_pct=args.min_target,
    )

    if args.variants:
        print("\n" + "=" * 132)
        print("【策略假設對照】哪一種 VWAP σ 帶做法真的具備邊際？")
        print("=" * 132)
        variants: list[tuple[str, dict]] = [
            ("A 基準：觸帶收盤進／TP=VWAP", {}),
            ("B 掛單在 σ 帶上成交", {"entry_mode": "limit"}),
            ("C 順勢回調（SMA100 方向）", {"trend_ma": 100}),
            ("D 順勢 + 掛帶", {"trend_ma": 100, "entry_mode": "limit"}),
            ("E 深帶 σ2.5／停 1.0σ", {"sigma_entry": 2.5, "sigma_stop": 1.0}),
            ("F 深帶 + 順勢 + 掛帶", {"sigma_entry": 2.5, "sigma_stop": 1.0, "trend_ma": 100, "entry_mode": "limit"}),
            ("G TP 到反向帶", {"tp_mode": "opposite", "sigma_stop": 1.0}),
            ("H 順勢 + TP反向帶 + 掛帶", {"tp_mode": "opposite", "sigma_stop": 1.0, "trend_ma": 100, "entry_mode": "limit"}),
            ("I 無體制限制（只靠 σ 帶）", {"regime_filter": False}),
            ("J 只做多（多頭市場偏差）", {"direction": "long"}),
            ("K 順勢 + 只做多", {"trend_ma": 100, "direction": "long"}),
            ("L 順勢 + 長持倉 24 根", {"trend_ma": 100, "max_hold_bars": 24, "sigma_stop": 1.0}),
            (
                "M σ2.8/停1.0/SMA200（關閉低波動過濾）",
                {
                    "sigma_entry": 2.8,
                    "sigma_stop": 1.0,
                    "trend_ma": 200,
                    "entry_mode": "limit",
                    "regime_filter": False,
                },
            ),
            (
                "N σ2.8/停1.0/SMA200（含低波動過濾）★",
                {"sigma_entry": 2.8, "sigma_stop": 1.0, "trend_ma": 200, "entry_mode": "limit"},
            ),
        ]
        scores: list[tuple[float, str]] = []
        for label, overrides in variants:
            params = Params(**{**asdict(base), **overrides})
            rows = [run_symbol(symbol, bars, params) for symbol, bars in bars_map.items()]
            summary = aggregate(rows, label)
            print("  " + format_row(summary))
            print(walk_forward(bars_map, params))
            if summary.get("trades", 0) >= 30:
                scores.append((summary["avg_r"], label))
        if scores:
            scores.sort(reverse=True)
            print("\n  按平均 R 排序（樣本 ≥ 30 筆）：")
            for avg_r, label in scores[:5]:
                print(f"    {avg_r:+.3f}  {label}")
        return 0

    print("\n" + "=" * 132)
    if args.compare:
        print("【對照】體制過濾到底有沒有用？（同參數，唯一差別是是否啟用低波動過濾）")
        print("=" * 132)
        for label, use_filter in (("全部體制（不過濾）", False), ("只做低波動趨勢不明", True)):
            params = Params(**{**asdict(base), "regime_filter": use_filter})
            rows = [run_symbol(symbol, bars, params) for symbol, bars in bars_map.items()]
            print("  " + format_row(aggregate(rows, label)))
        print()

    if args.grid:
        print("=" * 132)
        print("【敏感度】F 家族：σ 帶深度 × 停損寬度 × 順勢均線（掛單進場，全部走查驗證）")
        print("=" * 132)
        stability: list[tuple[float, str]] = []
        for sigma_entry in (2.2, 2.5, 2.8, 3.0):
            for sigma_stop in (0.8, 1.0, 1.5):
                for trend_ma in (50, 100, 200):
                    params = Params(
                        **{
                            **asdict(base),
                            "sigma_entry": sigma_entry,
                            "sigma_stop": sigma_stop,
                            "trend_ma": trend_ma,
                            "entry_mode": "limit",
                        }
                    )
                    rows = [run_symbol(symbol, bars, params) for symbol, bars in bars_map.items()]
                    full = aggregate(rows, "")
                    if full.get("trades", 0) < 40:
                        continue

                    ins: list[Result] = []
                    outs: list[Result] = []
                    for symbol, bars in bars_map.items():
                        cut = int(len(bars) * 0.6)
                        ins.append(run_symbol(symbol, bars[:cut], params))
                        outs.append(run_symbol(symbol, bars[cut:], params))
                    head = aggregate(ins, "in")
                    tail = aggregate(outs, "out")
                    worst = min(head.get("avg_r", -9), tail.get("avg_r", -9))
                    label = f"σ{sigma_entry} 停{sigma_stop}σ SMA{trend_ma}"
                    print(
                        f"  {label:<26} 全 {full['trades']:>3}筆 R{full['avg_r']:+.2f} PF{full['profit_factor']:.2f}"
                        f"  樣本內 {head.get('trades', 0):>3}筆 R{head.get('avg_r', 0):+.2f}"
                        f"  樣本外 {tail.get('trades', 0):>3}筆 R{tail.get('avg_r', 0):+.2f}"
                    )
                    stability.append((worst, label))
        if stability:
            stability.sort(reverse=True)
            print("\n  按「樣本內/外較差表現」排序（最穩定者在前）：")
            for worst, label in stability[:6]:
                print(f"    {worst:+.3f}  {label}")
        return 0

    print("=" * 132)
    print("【逐標的】VWAP ± σ 均值回歸（低波動體制）")
    print("=" * 132)
    rows: list[Result] = []
    for symbol, bars in bars_map.items():
        result = run_symbol(symbol, bars, base)
        rows.append(result)
        if result.trades:
            print("  " + format_row(result.summary(symbol)))
        else:
            print(f"  {symbol:<10} 0 筆（樣本期間無合格的低波動觸帶訊號）")
    print("-" * 132)
    print("  " + format_row(aggregate(rows, "合計")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
