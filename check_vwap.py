"""VWAP σ 帶訊號的離線驗證（不開 WS、不發 Telegram）。

用真實 4h K 線建構引擎 → 檢查 σ 帶數值是否正確 → 人工把價格推到 σ 帶附近
→ 確認候選與卡片輸出符合預期（含「限價掛單」語意）。
"""

from __future__ import annotations

import sys
import time

import requests

from binance_orderflow import (
    OrderflowEngine,
    RestGate,
    TradeEvent,
    compute_sma,
    compute_vwap_bands,
    fetch_klines,
    fetch_snapshot,
    format_orderflow_alert,
)
from config import load_config


def main() -> int:
    cfg = load_config()
    session = requests.Session()
    symbols = sys.argv[1:] or ["CLUSDT", "XAUUSDT", "BTCUSDT", "ETHUSDT", "AAPLUSDT"]
    fail = 0
    for symbol in symbols:
        engine = OrderflowEngine(cfg, symbol=symbol, gate=RestGate(), dry_run=True)
        try:
            snapshot = fetch_snapshot(session, symbol)
            rows = fetch_klines(session, symbol, cfg.orderflow_kline_interval, engine._kline_limit())
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol}: 資料取得失敗 {exc}")
            fail += 1
            continue
        engine.snapshot = snapshot
        engine.atr = 1.0
        engine._update_vwap_bands(rows)
        vwap, sigma = engine.vwap, engine.vwap_sigma
        trend = engine.vwap_trend
        if vwap <= 0 or sigma <= 0 or trend <= 0:
            print(f"{symbol}: σ 帶計算失敗（K 線 {len(rows)} 根）")
            fail += 1
            continue

        lower = vwap - engine.vwap_sigma_entry * sigma
        upper = vwap + engine.vwap_sigma_entry * sigma
        print(f"\n{'─' * 78}")
        print(f"{symbol}  K線 {len(rows)} 根")
        print(f"  VWAP ${vwap:,.4f}  σ ${sigma:,.4f} ({sigma / vwap * 100:.2f}%)")
        print(f"  下軌 ${lower:,.4f}   上軌 ${upper:,.4f}")
        print(f"  SMA{engine.vwap_trend_ma} ${trend:,.4f}  現價 ${snapshot.last_price:,.4f}")
        print(f"  位置：{(snapshot.last_price - vwap) / sigma:+.2f}σ")

        # --- 方向與觸發距離檢查 ---
        approach = engine.vwap_approach_sigma * sigma
        long_zone = (lower, lower + approach)
        short_zone = (upper - approach, upper)
        print(
            f"  多單觸發區 ${long_zone[0]:,.4f}–${long_zone[1]:,.4f}"
            f"   空單觸發區 ${short_zone[0]:,.4f}–${short_zone[1]:,.4f}"
        )
        trend_side = "多" if snapshot.last_price > trend else "空"
        print(f"  SMA 趨勢：{trend_side}（只做{trend_side}單回調）")

        # --- 模擬價格貼近 σ 帶，取得候選 ---
        probe = lower + 0.1 * sigma if snapshot.last_price > trend else upper - 0.1 * sigma
        candidate = engine._vwap_candidate(probe)
        if candidate is None:
            print("  ⚠ 候選未觸發（價格不在 σ 帶附近屬正常）")
            # 仍驗證 signal 建構路徑：直接把價格放到觸發區
            probe = lower + approach * 0.5 if snapshot.last_price > trend else upper - approach * 0.5
            candidate = engine._vwap_candidate(probe)
        if candidate is None:
            print("  ✗ 候選仍為 None")
            fail += 1
            continue

        # --- 走 _finalize_vwap 產生完整訊號 ---
        engine.context = None
        engine.min_grade = "C"  # 離線驗證不看評級
        event = TradeEvent(
            trade_time_ms=int(time.time() * 1000),
            price=probe,
            qty=1.0,
            taker_buy=True,
        )
        signal = engine._finalize_vwap(candidate, event, 0.0, "B", 75, (), probe)
        if signal is None:
            print("  ✗ _finalize_vwap 回傳 None（可能 TP 距離低於資產類別地板）")
            fail += 1
            continue
        print(f"  進場模式：{signal.entry_mode}｜掛單價 ${signal.entry_price:,.4f}")
        print(f"  止損 ${signal.stop_loss:,.4f}｜TP1 ${signal.tp1:,.4f}｜TP2 ${signal.tp2:,.4f}｜TP3 ${signal.tp3:,.4f}")
        risk = abs(signal.entry_price - signal.stop_loss)
        for label, target in (("TP1", signal.tp1), ("TP2", signal.tp2), ("TP3", signal.tp3)):
            r = abs(target - signal.entry_price) / risk if risk else 0
            print(f"    {label} = {r:.2f}R")
        card = format_orderflow_alert(signal)
        print("\n" + card)
        if "限價掛單" not in card:
            print("  ✗ 卡片未標示限價掛單")
            fail += 1
    print(f"\n{'═' * 78}\n完成，失敗 {fail} 項")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
