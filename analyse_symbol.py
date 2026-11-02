"""單一標的完整結構分析（確定性指標；不呼叫 LLM、不發 Telegram）。

用途：回答「現在該進、該等、還是該走」時，把引擎實際看到的每一個數字攤開。
這不是投資建議，而是「如果你持有這個標的，系統目前看到什麼」。

用法：
    python analyse_symbol.py SOLUSDT
    python analyse_symbol.py SOLUSDT BTCUSDT
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import requests

from binance_orderflow import (
    compute_atr,
    compute_sma,
    compute_vwap_bands,
    fetch_klines,
    fetch_snapshot,
)
from config import load_config
from market_context import fetch_market_context, trend_zh


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0 if b else 0.0


def analyse(session: requests.Session, cfg, symbol: str) -> None:
    snap = fetch_snapshot(session, symbol)
    k4h = fetch_klines(session, symbol, "4h", 225)
    atr = compute_atr(k4h, 14)
    vwap, sigma = compute_vwap_bands(k4h, 20)
    sma200 = compute_sma(k4h, 200)
    price = snap.last_price

    print("\n" + "═" * 74)
    print(f"{symbol}  {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("═" * 74)

    # --- 價格與 24h 區間 ---
    span = snap.high_24h - snap.low_24h
    pos = (price - snap.low_24h) / span * 100.0 if span > 0 else 50.0
    print(f"現價        ${price:,.4f}")
    print(
        f"24h 區間    ${snap.low_24h:,.4f} – ${snap.high_24h:,.4f}"
        f"（區間位置 {pos:.0f}%，距高點 {_pct(price, snap.high_24h):+.2f}%）"
    )
    print(f"24h 成交額  ${snap.quote_volume_24h / 1e6:,.1f}M")

    # --- 4h 波動與趨勢 ---
    atr_pct = atr / price * 100.0
    print(
        f"\n4H ATR(14)  ${atr:,.4f}（{atr_pct:.2f}% of price）"
        f"｜SMA200 ${sma200:,.4f}（現價 {_pct(price, sma200):+.2f}% ⇒ "
        f"{'多頭結構' if price > sma200 else '空頭結構'}）"
    )

    # --- VWAP σ 帶 ---
    if vwap > 0 and sigma > 0:
        dev = (price - vwap) / sigma
        upper = vwap + 2.8 * sigma
        lower = vwap - 2.8 * sigma
        print(
            f"VWAP(20×4H) ${vwap:,.4f}｜σ ${sigma:,.4f}（{sigma / vwap * 100:.2f}%）"
            f"｜偏離 {dev:+.2f}σ"
        )
        print(f"  σ 帶：下軌 ${lower:,.4f} / 上軌 ${upper:,.4f}")
        if dev <= -2.8:
            state = "已跌破 -2.8σ（極端超賣，但趨勢未必轉多）"
        elif dev <= -1.5:
            state = "接近 -2.8σ 掛單區（等回調接多）" if price > sma200 else "偏離均值偏空"
        elif dev >= 2.8:
            state = "已突破 +2.8σ（極端超買）"
        elif dev >= 1.5:
            state = "接近 +2.8σ 掛單區（等反彈接空）" if price < sma200 else "偏離均值偏多"
        else:
            state = "在均值附近，無 σ 帶邊際"
        print(f"  ⇒ {state}")

    # --- 結構性參考位（不是預測，是「跌破哪裡算破壞、反彈到哪裡算回到均值」）---
    closed = k4h[:-1] if len(k4h) > 1 else k4h
    for label, n in (("近 5 天", 30), ("近 20 天", 120)):
        if len(closed) >= n:
            seg = closed[-n:]
            hi = max(float(r[2]) for r in seg)
            lo = min(float(r[3]) for r in seg)
            print(
                f"{label}高低  ${lo:,.4f} – ${hi:,.4f}"
                f"｜距高點 {_pct(price, hi):+.2f}%｜距低點 {_pct(price, lo):+.2f}%"
            )

    # --- 多週期上下文（1h / 4h / 1d：趨勢、ADX、CVD） ---
    try:
        ctx = fetch_market_context(session, symbol, include_flow=True)
    except Exception as exc:  # noqa: BLE001
        print(f"\n上下文取得失敗：{exc}")
        return
    cvd_text = {"1h": "20 小時", "4h": "80 小時", "1d": "20 天"}
    print("\n多週期結構（漲跌為該週期 120 根 K 累計；CVD 為該週期近 20 根主動買賣差）：")
    window_days = {"1h": 5, "4h": 20, "1d": 120}
    for interval in ("1h", "4h", "1d"):
        frame = ctx.frames.get(interval)
        if frame is None:
            continue
        print(
            f"  {interval:<3} {trend_zh(frame):<6} "
            f"ADX {frame.adx:>5.1f}（+DI {frame.plus_di:>4.1f} / -DI {frame.minus_di:>4.1f}）"
            f"｜近 {window_days[interval]:>3} 天 {frame.price_change_pct:+.2f}%"
            f"｜CVD({cvd_text[interval]}) ${frame.cvd_usd / 1e6:+.2f}M"
        )
    print(
        f"  資金費率 {ctx.funding_rate * 100:+.4f}%"
        f"｜OI ${ctx.oi_now_usd / 1e6:,.1f}M｜{ctx.oi_matrix_label}"
    )

    # --- 決策輔助（純規則，非投資建議） ---
    print("\n" + "─" * 74)
    d1 = ctx.frames.get("1d")
    h4 = ctx.frames.get("4h")
    h1 = ctx.frames.get("1h")
    above_sma = price > sma200
    bull = sum(1 for f in (d1, h4, h1) if f is not None and f.trend == "up")
    bear = sum(1 for f in (d1, h4, h1) if f is not None and f.trend == "down")
    print(f"系統讀數：SMA200 {'之上' if above_sma else '之下'}｜多頭週期 {bull}/3｜空頭週期 {bear}/3")

    # --- 距離本系統「真正的進場點」還有多遠（使用者最常誤解的地方）---
    if vwap > 0 and sigma > 0:
        long_trigger = vwap - 2.8 * sigma
        short_trigger = vwap + 2.8 * sigma
        if price > sma200:
            gap = _pct(long_trigger, price)
            print(f"VWAP 多單掛單點 ${long_trigger:,.4f}（距現價 {gap:+.2f}%）⇒ 需先跌到此處")
        else:
            gap = _pct(short_trigger, price)
            print(f"VWAP 空單掛單點 ${short_trigger:,.4f}（距現價 {gap:+.2f}%）⇒ 需先反彈到此處")

    if above_sma and bear <= 1:
        verdict = "持有／等回調加倉（趨勢未破壞）"
    elif above_sma and bear >= 2:
        verdict = "減碼觀察（趨勢結構鬆動，SMA200 仍撐）"
    elif not above_sma and bull >= 2:
        verdict = "空手者不宜追多（結構已轉弱但短線在反彈）"
    else:
        verdict = "離場／觀望（SMA200 之下且空頭週期居多）"
    print(f"規則結論：{verdict}")
    print("─" * 74)
    print("註：以上為確定性指標讀數，非投資建議；本系統不預測價格。")


def main() -> int:
    cfg = load_config()
    session = requests.Session()
    symbols = sys.argv[1:] or ["SOLUSDT"]
    for symbol in symbols:
        try:
            analyse(session, cfg, symbol.upper())
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol} 分析失敗：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
