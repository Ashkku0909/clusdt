"""訊號帳本與結果驗證。

為什麼要有這個檔：
    在此之前，每一張發出的卡片都只存在於 Telegram，系統本身不記得發過什麼。
    於是「上一張 SOL 訊號到底對不對」這種問題永遠答不出來，只能靠人回翻聊天記錄。
    對一個賣訂閱的頻道，這是最大的信任缺口：沒有紀錄就沒有勝率，沒有勝率就沒有定價權。

設計：
    1. 每張成功發出的卡片寫入 `signals_ledger.json`（提交進 git，Render 重啟不掉）。
    2. `verify_ledger()` 逐筆抓發送後的 4h K 線，判定 TP1/TP2/TP3/止損/未成交失效。
    3. 判定規則刻意保守：
       - 未成交 = 價格從未觸及掛單區（掛單型）→ 不計入勝率，標記 expired
       - 同一根 K 棒同時觸及止損與目標 → 一律判定止損（最壞情況）
       - 分批出場按卡片比例計算（TP1 50% / TP2 30% / TP3 20%），止損後其餘按保本
    4. 也支援手動核對任意一張卡片：`--check` 傳入參數即可，不需先有帳本紀錄。

用法：
    python signal_ledger.py --verify                 # 驗證帳本內所有已到期訊號
    python signal_ledger.py --report                 # 只印統計（不重新驗證）
    python signal_ledger.py --check SOLUSDT short 2026-11-01T08:00 95.4 96.4 92.8 91.5 90.2
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from config import BASE_DIR, load_config

log = logging.getLogger(__name__)

LEDGER_PATH = BASE_DIR / "signals_ledger.json"
REST_BASE = "https://fapi.binance.com"
KLINE_INTERVAL = "4h"
MAX_VERIFY_BARS = 60  # 最多往前追 60 根 4H（10 天）
# 分批出場比例（與卡片文字一致）
TP1_SHARE, TP2_SHARE, TP3_SHARE = 0.50, 0.30, 0.20


# --------------------------------------------------------------------- #
# 資料模型
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class LedgerEntry:
    symbol: str
    direction: str  # long / short
    kind: str
    grade: str
    score: int
    entry_low: float
    entry_high: float
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    entry_mode: str = "market"  # market / limit
    sent_at: str = ""  # ISO UTC
    status: str = "open"  # open / expired / sl / tp1 / tp2 / tp3 / partial
    filled_at: str | None = None
    closed_at: str | None = None
    fill_price: float | None = None
    exit_price: float | None = None
    realized_r: float | None = None
    realized_pct: float | None = None
    note: str = ""
    verified_at: str | None = None
    bars_held: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def sent_dt(self) -> datetime:
        return datetime.fromisoformat(self.sent_at)


# --------------------------------------------------------------------- #
# 讀寫
# --------------------------------------------------------------------- #
def load_ledger(path: Path = LEDGER_PATH) -> list[LedgerEntry]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries: list[LedgerEntry] = []
    for row in raw if isinstance(raw, list) else []:
        try:
            entries.append(LedgerEntry(**row))
        except TypeError:
            continue
    return entries


def save_ledger(entries: list[LedgerEntry], path: Path = LEDGER_PATH) -> None:
    payload = [asdict(entry) for entry in entries]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def record_signal(signal, path: Path = LEDGER_PATH) -> None:
    """由引擎在「發送成功後」呼叫；重複（同標的、同方向、同進場價）不會重記。"""
    entries = load_ledger(path)
    stamp = datetime.now(timezone.utc).isoformat()
    for existing in entries:
        if (
            existing.symbol == signal.symbol
            and existing.direction == signal.direction
            and abs(existing.entry_price - signal.entry_price) < 1e-9
        ):
            return
    entries.append(
        LedgerEntry(
            symbol=signal.symbol,
            direction=signal.direction,
            kind=getattr(signal, "kind", "sweep"),
            grade=getattr(signal, "grade", ""),
            score=int(getattr(signal, "score", 0)),
            entry_low=float(signal.entry_low),
            entry_high=float(signal.entry_high),
            entry_price=float(signal.entry_price),
            stop_loss=float(signal.stop_loss),
            tp1=float(signal.tp1),
            tp2=float(signal.tp2),
            tp3=float(signal.tp3),
            entry_mode=getattr(signal, "entry_mode", "market"),
            sent_at=stamp,
        )
    )
    save_ledger(entries, path)
    log.info("訊號已寫入帳本：%s %s @ $%.4f（共 %d 筆）", signal.symbol, signal.direction, signal.entry_price, len(entries))


# --------------------------------------------------------------------- #
# 驗證
# --------------------------------------------------------------------- #
def _klines_after(session: requests.Session, symbol: str, start: datetime, limit: int) -> list[list]:
    rows = session.get(
        f"{REST_BASE}/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": KLINE_INTERVAL,
            "startTime": int(start.timestamp() * 1000),
            "limit": min(limit, 500),
        },
        timeout=(8, 30),
    )
    if rows.status_code != 200:
        raise RuntimeError(f"klines HTTP {rows.status_code}")
    return rows.json()


def evaluate(entry: LedgerEntry, bars: list[list]) -> LedgerEntry:
    """用發送後的 K 線判定結果（保守：同根 K 棒先判止損）。"""
    if not bars:
        entry.status = "open"
        entry.note = "尚無發送後的 K 線資料"
        return entry

    long_side = entry.direction == "long"
    risk = abs(entry.entry_price - entry.stop_loss)
    if risk <= 0:
        entry.status = "open"
        entry.note = "止損等於進場價，無法計算 R"
        return entry

    # 掛單型：必須先成交（價格進入掛單區）才開始算結果
    filled = entry.entry_mode != "limit"
    fill_price = entry.entry_price
    tp1_done = tp2_done = tp3_done = False
    remaining = 1.0
    realized_pct = 0.0

    for index, row in enumerate(bars):
        high, low = float(row[2]), float(row[3])
        close_time = datetime.fromtimestamp(int(row[6]) / 1000, tz=timezone.utc)

        if not filled:
            touched = (low <= entry.entry_high) if long_side else (high >= entry.entry_low)
            if not touched:
                continue
            filled = True
            fill_price = entry.entry_high if long_side else entry.entry_low
            entry.filled_at = close_time.isoformat()
            entry.fill_price = fill_price
            # 同一根 K 棒成交後立刻打穿止損 → 直接結案
            if (long_side and low <= entry.stop_loss) or (
                not long_side and high >= entry.stop_loss
            ):
                entry.status = "sl"
                entry.exit_price = entry.stop_loss
                entry.closed_at = close_time.isoformat()
                entry.bars_held = index
                entry.realized_pct = _pct(entry.stop_loss, fill_price, long_side)
                entry.realized_r = entry.realized_pct / (risk / fill_price * 100.0)
                entry.note = "成交當根即觸及止損"
                return entry

        # 止損優先（最壞情況假設）
        if (long_side and low <= entry.stop_loss) or (
            not long_side and high >= entry.stop_loss
        ):
            entry.exit_price = entry.stop_loss
            entry.closed_at = close_time.isoformat()
            entry.bars_held = index
            # TP 已分批出場的部分不變，剩餘以止損價結算
            realized_pct += remaining * _pct(entry.stop_loss, fill_price, long_side)
            entry.status = "partial" if (tp1_done or tp2_done or tp3_done) else "sl"
            entry.realized_pct = realized_pct
            entry.realized_r = realized_pct / (risk / fill_price * 100.0)
            entry.note = _tp_note(tp1_done, tp2_done, tp3_done) + "後止損"
            return entry

        # 目標（自高到低依序判定）
        if not tp1_done and (
            (long_side and high >= entry.tp1) or (not long_side and low <= entry.tp1)
        ):
            tp1_done = True
            realized_pct += TP1_SHARE * _pct(entry.tp1, fill_price, long_side)
            remaining -= TP1_SHARE
        if tp1_done and not tp2_done and (
            (long_side and high >= entry.tp2) or (not long_side and low <= entry.tp2)
        ):
            tp2_done = True
            realized_pct += TP2_SHARE * _pct(entry.tp2, fill_price, long_side)
            remaining -= TP2_SHARE
        if tp2_done and not tp3_done and (
            (long_side and high >= entry.tp3) or (not long_side and low <= entry.tp3)
        ):
            tp3_done = True
            realized_pct += TP3_SHARE * _pct(entry.tp3, fill_price, long_side)
            remaining -= TP3_SHARE
            entry.status = "tp3"
            entry.exit_price = entry.tp3
            entry.closed_at = close_time.isoformat()
            entry.bars_held = index
            entry.realized_pct = realized_pct
            entry.realized_r = realized_pct / (risk / fill_price * 100.0)
            entry.note = "三段目標全數達成"
            return entry

    # 走完資料仍未結案
    last_close = float(bars[-1][4])
    if not filled:
        entry.status = "expired"
        entry.note = "掛單期限內未成交（σ 帶未被觸及）"
        entry.closed_at = datetime.fromtimestamp(int(bars[-1][6]) / 1000, tz=timezone.utc).isoformat()
        return entry
    entry.status = "partial" if tp1_done else "open"
    entry.exit_price = last_close
    entry.realized_pct = realized_pct + remaining * _pct(last_close, fill_price, long_side)
    entry.realized_r = entry.realized_pct / (risk / fill_price * 100.0)
    entry.note = (_tp_note(tp1_done, tp2_done, tp3_done) + "尚未結案，以現價估算")
    return entry


def _pct(target: float, base: float, long_side: bool) -> float:
    if base <= 0:
        return 0.0
    return (target - base) / base * 100.0 if long_side else (base - target) / base * 100.0


def _tp_note(tp1: bool, tp2: bool, tp3: bool) -> str:
    hits = [name for name, hit in (("TP1", tp1), ("TP2", tp2), ("TP3", tp3)) if hit]
    return ("已達 " + "/".join(hits) + "，") if hits else ""


def verify_ledger(cfg, path: Path = LEDGER_PATH, force: bool = False) -> list[LedgerEntry]:
    """重新驗證帳本內未結案的訊號；已結案的不再重算。"""
    entries = load_ledger(path)
    if not entries:
        return entries
    session = requests.Session()
    changed = False
    for entry in entries:
        if entry.status in ("sl", "tp3", "expired") and not force:
            continue
        # 至少等一根 4H 收盤才值得驗證
        if datetime.now(timezone.utc) - entry.sent_dt < timedelta(hours=4):
            continue
        try:
            bars = _klines_after(session, entry.symbol, entry.sent_dt, MAX_VERIFY_BARS)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 驗證時 K 線取得失敗：%s", entry.symbol, exc)
            time.sleep(1.0)
            continue
        before = (entry.status, entry.realized_r)
        evaluate(entry, bars)
        entry.verified_at = datetime.now(timezone.utc).isoformat()
        changed = changed or before != (entry.status, entry.realized_r)
        time.sleep(0.3)
    if changed:
        save_ledger(entries, path)
    return entries


def report(entries: list[LedgerEntry]) -> str:
    if not entries:
        return "帳本為空：系統尚未記錄任何訊號（下次發送後才會開始累積）。"
    settled = [e for e in entries if e.status in ("sl", "partial", "tp3")]
    wins = [e for e in settled if (e.realized_r or 0) > 0]
    lines = ["訊號帳本統計", "─" * 62]
    lines.append(f"總紀錄 {len(entries)} 筆｜已結案 {len(settled)} 筆｜未結案/未成交 {len(entries) - len(settled)} 筆")
    if settled:
        avg_r = sum(e.realized_r or 0 for e in settled) / len(settled)
        gross_win = sum(e.realized_r or 0 for e in settled if (e.realized_r or 0) > 0)
        gross_loss = -sum(e.realized_r or 0 for e in settled if (e.realized_r or 0) <= 0)
        lines.append(
            f"勝率 {100.0 * len(wins) / len(settled):.1f}%｜平均 R {avg_r:+.2f}"
            f"｜獲利因子 {(gross_win / gross_loss) if gross_loss > 0 else float('inf'):.2f}"
        )
    lines.append("")
    for entry in entries:
        r = f"{entry.realized_r:+.2f}R" if entry.realized_r is not None else "—"
        lines.append(
            f"{entry.sent_at[:16]} {entry.symbol:<10} {entry.direction:<5} {entry.kind:<10} "
            f"{entry.status:<8} {r:>8}  {entry.note}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #
def _parse_check(argv: list[str]) -> LedgerEntry:
    """--check SYMBOL direction ISO_TIME entry stop tp1 tp2 tp3 [limit|market]"""
    if len(argv) < 8:
        raise SystemExit(
            "用法：--check SYMBOL direction 2026-11-01T08:00 entry stop tp1 tp2 tp3 [limit|market]"
        )
    symbol, direction, stamp, entry, stop, tp1, tp2, tp3 = argv[:8]
    mode = argv[8] if len(argv) > 8 else "market"
    sent = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return LedgerEntry(
        symbol=symbol.upper(),
        direction=direction.lower(),
        kind="manual",
        grade="",
        score=0,
        entry_low=float(entry),
        entry_high=float(entry),
        entry_price=float(entry),
        stop_loss=float(stop),
        tp1=float(tp1),
        tp2=float(tp2),
        tp3=float(tp3),
        entry_mode=mode,
        sent_at=sent.isoformat(),
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    parser = argparse.ArgumentParser(description="訊號帳本與結果驗證")
    parser.add_argument("--verify", action="store_true", help="重新驗證帳本內未結案的訊號")
    parser.add_argument("--report", action="store_true", help="印出統計")
    parser.add_argument("--force", action="store_true", help="連已結案的也重算")
    parser.add_argument("--check", nargs="+", help="手動核對單一訊號")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.check:
        entry = _parse_check(args.check)
        session = requests.Session()
        bars = _klines_after(session, entry.symbol, entry.sent_dt, MAX_VERIFY_BARS)
        evaluate(entry, bars)
        print(report([entry]))
        return 0
    if args.verify:
        entries = verify_ledger(cfg, force=args.force)
    else:
        entries = load_ledger()
    print(report(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
