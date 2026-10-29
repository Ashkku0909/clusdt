"""幣安 USDT-M 合約即時訂單流引擎（多標的；100% 確定性、零 LLM）。

支援標的（BINANCE_SYMBOLS，逗號分隔）：
    CL 原油（CLUSDT）、BTC、ETH、SOL、XRP、LINK、ENA、XAU 黃金（TRADIFI）、XAG 白銀（TRADIFI）

每個標的獨立運行一個引擎（各自 WS 串流、滾動窗口、冷卻與自動校準門檻）；
所有標的共用一個「全頻道警報預算」（ORDERFLOW_MAX_ALERTS_PER_HOUR）。

唯一權威市場資料源：Binance 公共 REST + WebSocket
====================================================

訊號邏輯（完全規則化，無任何模型／情感／敘事判讀）：

1. 逐筆成交串流 ``<symbol>@trade``：事件含 ``m`` 欄位（買方是否為做市商）。
       ``m = False`` → 主動買（市價掃貨吃單）
       ``m = True``  → 主動賣（市價砸盤吃單）
   ※ 說明：fapi 的 ``@aggTrade`` 串流目前對所有代碼皆無推送（含 ETHUSDT 對照組），
     而 ``@trade`` 逐筆推送且自帶相同語意的 ``m`` 欄位，故採用 ``@trade`` 作為唯一來源，
     不使用任何延遲／第三方行情（已完全移除 yfinance 依賴）。

2. 觸發條件（任一成立即進入位置過濾）：
   a. 單筆吃單名義金額 >= ORDERFLOW_BLOCK_USD（預設 $500,000）
   b. ORDERFLOW_WINDOW_SEC（預設 15）秒滾動窗口內同方向累計 >= ORDERFLOW_WINDOW_USD（預設 $1,000,000）

3. 位置過濾（機構級關鍵位）：
   成交價必須距離 24h 高點或低點 <= ORDERFLOW_SR_PROXIMITY_PCT（預設 0.3%），
   否則視為「中段噪音」直接壓制。

4. 止損：ATR(14)（15m K 線，Wilder EMA 平滑）之 ORDERFLOW_ATR_MULT 倍（預設 1.5）。
      做多止損 = 進場價 − 1.5 × ATR
      做空止損 = 進場價 + 1.5 × ATR

5. 額外訊號（可用環境變數分別開關，增加訊號多樣性）：
   a. 動能突破 momentum：60 秒同向 delta >= ORDERFLOW_MOMENTUM_USD 且主動佔比 >= 70%，
      且價格創 24h 新高／新低
   b. 巨量吸收 absorption：60 秒雙向總量 >= ORDERFLOW_ABSORPTION_USD 但價格推不動
      （<= 0.08%）且貼著 24h 高／低（未突破）→ 低點賣壓被接走＝做多；高點買盤被吃掉＝做空
   c. 量價背離 divergence：5 分鐘 delta 與價格方向相反（價漲但淨主動賣 >= $3M＝派發；
      價跌但淨主動買 >= $3M＝吸籌）

工程設計：
- WebSocket 斷線自動重連（指數退避 5s → 10s → 20s → 40s → 60s 上限）
- 24h 高低點與 ATR 每 ORDERFLOW_TA_REFRESH_SEC（預設 300s）以 REST 背景刷新（to_thread）；
  失敗僅指數退避重試（418/429 直接封鎖 1 小時），**絕不逐筆重試**，
  行情基準超過 ORDERFLOW_SNAPSHOT_MAX_AGE_SEC（預設 1800s）自動停發新訊號
- 每個方向獨立冷卻（預設 60s），外加全域最小警報間隔 ORDERFLOW_MIN_GAP_SEC（預設 300s）
  與同方向最小價格推移 ORDERFLOW_MIN_MOVE_PCT（預設 0.12%）——三層防刷頻
- ``run_forever()`` 為 asyncio 任務，於 FastAPI lifespan 以 ``asyncio.create_task`` 常駐
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Iterable

import requests
import websockets

from market_context import (
    GRADE_RANK,
    MarketContext,
    context_line,
    fetch_market_context,
    grade_signal,
    load_context_cache,
    save_context_cache,
)

log = logging.getLogger(__name__)

REST_BASE = "https://fapi.binance.com"
WS_COMBINED = "wss://fstream.binance.com/stream?streams={streams}"

# 依序探測：CLUSDT 為 Binance 現行「原油 (CL)」永續合約代碼
CANDIDATE_SYMBOLS: tuple[str, ...] = ("CLUSDT", "OILUSDT")
FALLBACK_BASE_ASSETS: tuple[str, ...] = ("CL", "OIL", "WTI", "BRENT", "CRUDE")

# ATR 磁碟快取：重啟或 REST 被封鎖期間仍可用（WS K 線會持續增量更新）
BASELINE_DIR = Path(__file__).resolve().parent / "data" / "baseline"
BASELINE_MAX_AGE_SEC = 12 * 3600.0

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}


def interval_ms(interval: str) -> int:
    """K 線週期字串（15m/1h/4h/1d）→ 毫秒。"""
    return INTERVAL_MS.get(str(interval).strip(), 900_000)

DIRECTION_LABELS = {"long": "做多 🟢", "short": "做空 🔴"}

# 開頭號誌燈（紅綠燈語意）：做多 = 綠燈 🟢、做空 = 紅燈 🔴
DIRECTION_LIGHTS = {"long": "🟢", "short": "🔴"}

# 標的名稱（警報標題）與訊號類型後綴
SYMBOL_LABELS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
    "LINKUSDT": "LINK",
    "ENAUSDT": "ENA",
    "BNBUSDT": "BNB",
    "XAUUSDT": "XAU 黃金",
    "XAGUSDT": "XAG 白銀",
    "PAXGUSDT": "PAXG 黃金",
    "XPTUSDT": "XPT 鉑金",
    "XPDUSDT": "XPD 鈀金",
    "COPPERUSDT": "銅",
    "BZUSDT": "Brent",
    "NATGASUSDT": "天然氣",
    "CLUSDT": "CL 原油",
    "COINUSDT": "COIN",
    "HOODUSDT": "HOOD",
    "SPCXUSDT": "SPCX",
    "CRCLUSDT": "CRCL",
    "MUUSDT": "MU",
    "SNDKUSDT": "SNDK",
    "SKHYNIXUSDT": "SK Hynix",
    "GOOGLUSDT": "GOOGL",
    "TSLAUSDT": "TSLA",
    "NVDAUSDT": "NVDA",
    "METAUSDT": "META",
    "AAPLUSDT": "AAPL",
    "MSFTUSDT": "MSFT",
    "AMZNUSDT": "AMZN",
    "MSTRUSDT": "MSTR",
    "PLTRUSDT": "PLTR",
}
KIND_LABELS = {
    "sweep": "異動",
    "momentum": "動能突破",
    "absorption": "巨量吸收",
    "divergence": "量價背離",
}


def symbol_label(symbol: str) -> str:
    code = (symbol or "").upper()
    return SYMBOL_LABELS.get(code, code.replace("USDT", ""))

NATURE_SWEEP = "連續市價吃單 (主動做市主力)"
NATURE_DEFENSE = "關鍵位強勢防守"

LEVEL_TEXT = {
    ("high", "long"): "突破前高阻力位 ${price:,.2f}",
    ("high", "short"): "防禦前高阻力位 ${price:,.2f}",
    ("low", "long"): "測試前低支撐位 ${price:,.2f}",
    ("low", "short"): "跌破前低支撐位 ${price:,.2f}",
}


def _level_text(level_kind: str, direction: str, level_price: float, entry_price: float) -> str:
    """關鍵位置文字（依進場價相對關鍵位的實際位置選詞，避免語意矛盾）。

    - 高＋買：進場價 >= 高點 → 突破；在其下方 → 測試
    - 低＋賣：進場價 <= 低點 → 跌破；在其上方 → 測試
    """
    if level_kind == "high" and direction == "long":
        verb = "突破" if entry_price >= level_price else "測試"
        return f"{verb}前高阻力位 ${level_price:,.2f}"
    if level_kind == "low" and direction == "short":
        verb = "跌破" if entry_price <= level_price else "測試"
        return f"{verb}前低支撐位 ${level_price:,.2f}"
    return LEVEL_TEXT[(level_kind, direction)].format(price=level_price)

TRIGGER_LABELS = {
    "single_block": "單筆巨量吃單",
    "window_flow": "15 秒內同向連續吃單",
    "momentum": "60 秒動能突破",
    "absorption": "60 秒巨量吸收",
    "divergence": "5 分鐘量價背離",
}


# --------------------------------------------------------------------- #
# 資料結構
# --------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class TradeEvent:
    """單筆逐筆成交（taker 視角）。"""

    trade_time_ms: int
    price: float
    qty: float
    taker_buy: bool

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class MarketSnapshot:
    symbol: str
    last_price: float
    high_24h: float
    low_24h: float
    quote_volume_24h: float
    trade_count_24h: int
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True, frozen=True)
class OrderflowSignal:
    symbol: str
    direction: str  # "long" / "short"
    entry_price: float
    stop_loss: float
    atr: float
    atr_mult: float
    nature: str
    notional: float
    qty: float
    trigger: str  # "single_block" / "window_flow"
    level_kind: str  # "high" / "low"
    level_price: float
    level_text: str
    trade_time: datetime
    kind: str = "sweep"
    grade: str = "B"
    score: int = 0
    entry_low: float = 0.0
    entry_high: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    context_text: str = ""


@dataclass(slots=True, frozen=True)
class SignalCandidate:
    """偵測器產出的候選訊號（尚未通過冷卻／間隔過濾）。"""

    kind: str
    direction: str
    nature: str
    notional: float
    qty: float
    trigger: str
    level_kind: str
    level_price: float
    level_text: str


class FlowWindow:
    """以成交時間剪枝的滾動窗口（名義金額與數量累計）。"""

    __slots__ = ("ms", "events", "buy", "sell", "buy_qty", "sell_qty")

    def __init__(self, ms: int) -> None:
        self.ms = ms
        self.events: Deque[TradeEvent] = deque()
        self.buy = 0.0
        self.sell = 0.0
        self.buy_qty = 0.0
        self.sell_qty = 0.0

    def add(self, event: TradeEvent) -> None:
        cutoff = event.trade_time_ms - self.ms
        while self.events and self.events[0].trade_time_ms < cutoff:
            old = self.events.popleft()
            if old.taker_buy:
                self.buy -= old.notional
                self.buy_qty -= old.qty
            else:
                self.sell -= old.notional
                self.sell_qty -= old.qty
        self.events.append(event)
        if event.taker_buy:
            self.buy += event.notional
            self.buy_qty += event.qty
        else:
            self.sell += event.notional
            self.sell_qty += event.qty

    @property
    def delta(self) -> float:
        return self.buy - self.sell

    @property
    def total(self) -> float:
        return self.buy + self.sell

    def first_price(self) -> float | None:
        return self.events[0].price if self.events else None


# --------------------------------------------------------------------- #
# REST 資料層（全部為阻塞式；呼叫端請以 asyncio.to_thread 執行）
# --------------------------------------------------------------------- #
def _request_json(
    session: requests.Session,
    path: str,
    params: dict | None = None,
    timeout: tuple[float, float] = (6.0, 20.0),
):
    response = session.get(f"{REST_BASE}{path}", params=params, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"Binance HTTP {response.status_code}（{path}）：{response.text[:150]}")
    return response.json()


def fetch_snapshot(session: requests.Session, symbol: str) -> MarketSnapshot:
    """24h ticker：最新價與 24h 高低點（關鍵支撐／壓力來源）。"""
    payload = _request_json(session, "/fapi/v1/ticker/24hr", {"symbol": symbol})
    return MarketSnapshot(
        symbol=symbol,
        last_price=float(payload["lastPrice"]),
        high_24h=float(payload["highPrice"]),
        low_24h=float(payload["lowPrice"]),
        quote_volume_24h=float(payload.get("quoteVolume", 0.0)),
        trade_count_24h=int(payload.get("count", 0)),
    )


def fetch_klines(
    session: requests.Session,
    symbol: str,
    interval: str = "15m",
    limit: int = 64,
) -> list[list]:
    """K 線（新 → 舊排序反轉為舊 → 新，方便 ATR 計算）。"""
    rows = _request_json(
        session,
        "/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": max(limit, 32)},
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Binance {symbol} {interval} K 線無資料")
    return rows


def compute_atr(klines: Iterable[list], period: int = 14) -> float:
    """ATR(14)：Wilder 式 EMA(TR, period)。

    TR = max(high − low, |high − prev_close|, |low − prev_close|)
    種子 = 前 period 根 TR 的簡單平均；其後 atr += (tr − atr) / period
    """
    if period < 1:
        raise ValueError("ATR period 必須 >= 1")
    true_ranges: list[float] = []
    prev_close: float | None = None
    for row in klines:
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])
        if prev_close is None:
            true_ranges.append(high - low)
        else:
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    if len(true_ranges) < period:
        raise RuntimeError(f"K 線數量不足（{len(true_ranges)} < ATR period {period}）")
    atr = sum(true_ranges[:period]) / period
    alpha = 1.0 / period
    for tr in true_ranges[period:]:
        atr += alpha * (tr - atr)
    return atr


def fetch_atr(
    session: requests.Session,
    symbol: str,
    interval: str = "15m",
    period: int = 14,
) -> float:
    """以 15m K 線計算 ATR(14)（EMA 形式）。"""
    klines = fetch_klines(session, symbol, interval, limit=period + 48)
    return compute_atr(klines, period)


def resolve_symbol(session: requests.Session, preferred: str = "") -> str:
    """確定實際可用的 Binance 合約代碼（CLUSDT / OILUSDT / exchangeInfo 掃描）。

    已知代碼（非 CLUSDT/OILUSDT）直接採信，不再額外打 ticker——
    多標的啟動時少 23 次 REST 呼叫，避免瞬間權重超限。
    """
    code = (preferred or "").strip().upper()
    if code and code not in CANDIDATE_SYMBOLS:
        return code
    candidates: list[str] = []
    for item in (preferred, *CANDIDATE_SYMBOLS):
        code = (item or "").strip().upper()
        if code and code not in candidates:
            candidates.append(code)
    for code in candidates:
        try:
            fetch_snapshot(session, code)
            return code
        except Exception:  # noqa: BLE001 - 逐一探測
            continue
    # 最終手段：掃描 exchangeInfo 的 baseAsset
    payload = _request_json(session, "/fapi/v1/exchangeInfo")
    for item in payload.get("symbols", []):
        if item.get("status") != "TRADING":
            continue
        if str(item.get("quoteAsset", "")).upper() != "USDT":
            continue
        if str(item.get("baseAsset", "")).upper() in FALLBACK_BASE_ASSETS:
            return str(item["symbol"]).upper()
    raise RuntimeError("找不到可用的 Binance 原油合約代碼（CLUSDT/OILUSDT）")


# --------------------------------------------------------------------- #
# 訊息模板（固定格式、無 LLM）
# --------------------------------------------------------------------- #
def _fmt_price(value: float) -> str:
    """依價格量級選擇小數位（BTC 2 位、XRP 4 位、ENA 5 位）。"""
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.5f}"


def format_orderflow_alert(signal: OrderflowSignal) -> str:
    """依既定模板輸出警報（純文字，Telegram 端僅做 Markdown→HTML 轉義）。"""
    lines = [
        f"{DIRECTION_LIGHTS[signal.direction]} 【{symbol_label(signal.symbol)} {KIND_LABELS.get(signal.kind, KIND_LABELS['sweep'])}】",
        "━━━━━━━━━━━━━━━━━━",
        f"方向：{DIRECTION_LABELS[signal.direction]}",
        f"進場位：${_fmt_price(signal.entry_price)}",
        f"進場區間：${_fmt_price(signal.entry_low)} – ${_fmt_price(signal.entry_high)}（限價）",
        f"止損位：${_fmt_price(signal.stop_loss)} (ATR {signal.atr_mult:g}x)",
        f"止盈：TP1 ${_fmt_price(signal.tp1)}｜TP2 ${_fmt_price(signal.tp2)}",
        f"評級：{'—（技術面暫不可用，純量價）' if signal.grade == '—' else f'{signal.grade}（{signal.score} 分）'}",
        f"性質：{signal.nature}",
        f"大單金額：${signal.notional:,.0f}",
        f"成交量：{signal.qty:,.2f} 手",
        f"關鍵位置：{signal.level_text}",
    ]
    if signal.context_text:
        lines.append(f"依據：{signal.context_text}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# 訂單流引擎
# --------------------------------------------------------------------- #
class RestGate:
    """跨標的共享 REST 熔斷：任一標的遇到 418/429，全體暫停到封鎖結束。

    沒有這層時，23 個引擎會各自每 60 秒重試，把 Binance 的封鎖時間不斷延長。
    """

    def __init__(self, default_ban_sec: float = 600.0) -> None:
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self.default_ban_sec = default_ban_sec

    def blocked(self) -> bool:
        with self._lock:
            return time.monotonic() < self._blocked_until

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._blocked_until - time.monotonic())

    def block_for(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + max(60.0, float(seconds)))


def parse_ban_seconds(exc: Exception, default: float = 600.0) -> float | None:
    """從 418/429 錯誤訊息解析「banned until <epoch ms>」，回傳需要暫停的秒數。"""
    message = str(exc)
    lowered = message.lower()
    if "418" not in message and "429" not in message and "banned" not in lowered and "too many requests" not in lowered:
        return None
    match = re.search(r"banned until (\d{10,})", message)
    if match:
        try:
            until = int(match.group(1)) / 1000.0
            return max(60.0, min(6 * 3600.0, until - time.time() + 30.0))
        except (TypeError, ValueError):
            pass
    return default


class AlertBudget:
    """全域警報預算（所有標的共用；防止多標的同時刷頻）。"""

    def __init__(self, max_per_hour: int) -> None:
        self.max_per_hour = max(0, int(max_per_hour))
        self._sent: Deque[float] = deque()

    def allow(self, clock: float) -> bool:
        """在 1 小時滾動窗內保留一個名額；超過上限回傳 False（0 = 不限）。"""
        if self.max_per_hour <= 0:
            return True
        while self._sent and clock - self._sent[0] > 3600.0:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_hour:
            return False
        self._sent.append(clock)
        return True


class OrderflowEngine:
    """Binance 逐筆成交 → 大單／連續吃單偵測 → 固定模板警報（單一標的）。"""

    def __init__(
        self,
        cfg,
        dispatcher=None,
        *,
        symbol: str | None = None,
        dry_run: bool = False,
        budget: "AlertBudget | None" = None,
        gate: "RestGate | None" = None,
    ) -> None:
        self.symbol = (symbol or getattr(cfg, "binance_symbol", "") or CANDIDATE_SYMBOLS[0]).upper()
        self.block_usd = float(cfg.orderflow_block_usd)
        self.window_usd = float(cfg.orderflow_window_usd)
        self.window_ms = int(cfg.orderflow_window_sec) * 1000
        self.proximity_pct = float(cfg.orderflow_sr_proximity_pct)
        self.atr_period = int(cfg.orderflow_atr_period)
        self.atr_mult = float(cfg.orderflow_atr_mult)
        self.kline_interval = cfg.orderflow_kline_interval
        self.cooldown_sec = float(cfg.orderflow_cooldown_sec)
        self.ta_refresh_sec = float(cfg.orderflow_ta_refresh_sec)
        self.min_gap_sec = float(cfg.orderflow_min_gap_sec)
        self.min_move_pct = float(cfg.orderflow_min_move_pct)
        self.enable_momentum = bool(cfg.orderflow_enable_momentum)
        self.momentum_usd = float(cfg.orderflow_momentum_usd)
        self.momentum_share = float(cfg.orderflow_momentum_share)
        self.enable_absorption = bool(cfg.orderflow_enable_absorption)
        self.absorption_usd = float(cfg.orderflow_absorption_usd)
        self.absorption_range_pct = float(cfg.orderflow_absorption_range_pct)
        self.enable_divergence = bool(cfg.orderflow_enable_divergence)
        self.divergence_usd = float(cfg.orderflow_divergence_usd)
        self.divergence_price_pct = float(cfg.orderflow_divergence_price_pct)
        # 自動門檻校準（依 24h 成交額；每標的獨立）
        self.auto_calibrate = bool(cfg.orderflow_auto_calibrate)
        self.calibration_factor = float(cfg.orderflow_calibration_factor)
        self.min_window_usd = float(cfg.orderflow_min_window_usd)
        self.max_window_usd = float(cfg.orderflow_max_window_usd)
        self.budget = budget
        self.gate = gate or RestGate()
        self.context: MarketContext | None = None
        self.min_grade = str(cfg.orderflow_min_grade).upper()

        self.dispatcher = dispatcher
        self.dry_run = dry_run

        self.session = requests.Session()
        self.snapshot: MarketSnapshot | None = None
        self.atr: float | None = None
        self.snapshot_max_age_sec = float(cfg.orderflow_snapshot_max_age_sec)
        self._stopping = False
        self._refresh_lock = threading.Lock()
        self._next_refresh_at = 0.0  # 下一次一般刷新時間（單調鐘）
        self._refresh_fail_until = 0.0  # 418/429 封鎖期間（單調鐘；強制刷新也須等）
        self._refresh_failures = 0
        self._last_stale_warn = 0.0
        self._baseline_warned = False
        self._next_context_at = 0.0
        # WS K 線增量維護 ATR（REST 被封鎖也不中斷）
        self._bar_ms = interval_ms(self.kline_interval)
        self._prev_close: float | None = None
        self._prev_bar_ts = 0

        # 多尺度滾動窗口（15s 大單偵測 / 60s 動能與吸收 / 300s 背離）
        self.win15 = FlowWindow(self.window_ms)
        self.win60 = FlowWindow(60_000)
        self.win300 = FlowWindow(300_000)
        self._cooldown_until: dict[str, float] = {"long": 0.0, "short": 0.0}
        self._last_alert_at = -1.0e9  # 全域最小間隔用：最後一次「已發送」時間
        self._last_alert_price: dict[str, float] = {}  # 各方向最後一筆已發送價位

        self.stats: dict[str, float] = {
            "trades": 0,
            "single_blocks": 0,
            "window_triggers": 0,
            "signals": 0,
            "suppressed_sr": 0,
            "suppressed_cooldown": 0,
            "suppressed_gap": 0,
            "suppressed_move": 0,
            "suppressed_stale": 0,
            "signal_sweep": 0,
            "signal_momentum": 0,
            "signal_absorption": 0,
            "signal_divergence": 0,
            "suppressed_budget": 0,
            "suppressed_grade": 0,
            "suppressed_bad_tick": 0,
            "max_single_notional": 0.0,
            "max_buy_flow": 0.0,
            "max_sell_flow": 0.0,
        }

    # ------------------------------------------------------------------ #
    # 市場資料（阻塞；請以 to_thread 呼叫）
    # ------------------------------------------------------------------ #
    def _baseline_path(self) -> Path:
        return BASELINE_DIR / f"{self.symbol}.json"

    def _save_baseline(self) -> None:
        """把 ATR 與最後一根收盤 K 線寫盤，供重啟／REST 封鎖期間使用。"""
        if not self.atr or self.atr <= 0:
            return
        payload = {
            "symbol": self.symbol,
            "kline_interval": self.kline_interval,
            "atr": self.atr,
            "prev_close": self._prev_close,
            "prev_bar_ts": self._prev_bar_ts,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            BASELINE_DIR.mkdir(parents=True, exist_ok=True)
            self._baseline_path().write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - 快取失敗不影響主流程
            log.debug("%s ATR 快取寫入失敗：%s", self.symbol, exc)

    def _load_baseline(self) -> None:
        """讀回磁碟 ATR；尚未取得 ATR 時才载入（REST 恢復後會覆蓋）。"""
        if self.atr and self.atr > 0:
            return
        try:
            payload = json.loads(self._baseline_path().read_text(encoding="utf-8"))
            saved = datetime.fromisoformat(payload["saved_at"])
            atr = float(payload["atr"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - saved).total_seconds()
        if atr <= 0 or age > BASELINE_MAX_AGE_SEC:
            return
        self.atr = atr
        prev_close = payload.get("prev_close")
        self._prev_close = float(prev_close) if prev_close else None
        self._prev_bar_ts = int(payload.get("prev_bar_ts") or 0)
        log.info("%s 沿用磁碟 ATR %.4f（%.0f 分鐘前）", self.symbol, atr, age / 60.0)

    def _remember_closed_bar(self, klines: list[list]) -> None:
        """記住最後一根「已收盤」K 線，作為 WS ATR 增量的起點。"""
        if len(klines) < 2:
            return
        row = klines[-2]  # 最後一根為進行中，倒數第二根才是已收盤
        with contextlib.suppress(TypeError, ValueError, IndexError):
            self._prev_bar_ts = int(row[0])
            self._prev_close = float(row[4])

    def _apply_ws_ticker(self, data: dict) -> None:
        """24hrTicker 推播（每秒）：直接用 WS 維護 24h 高低點，不依賴 REST。"""
        try:
            last = float(data["c"])
            high = float(data["h"])
            low = float(data["l"])
            quote_volume = float(data.get("q") or 0.0)
            trades = int(data.get("n") or 0)
        except (KeyError, TypeError, ValueError):
            return
        if last <= 0 or high <= 0 or low <= 0 or high < low:
            return
        self.snapshot = MarketSnapshot(
            symbol=self.symbol,
            last_price=last,
            high_24h=high,
            low_24h=low,
            quote_volume_24h=quote_volume,
            trade_count_24h=trades,
        )
        self.apply_calibration(self.snapshot)

    def _apply_ws_kline(self, data: dict) -> None:
        """K 線收盤時用 Wilder 遞推更新 ATR（不需 REST）。"""
        bar = data.get("k") or {}
        if not bar.get("x"):
            return
        try:
            open_ts = int(bar["t"])
            high = float(bar["h"])
            low = float(bar["l"])
            close = float(bar["c"])
        except (KeyError, TypeError, ValueError):
            return
        if open_ts == self._prev_bar_ts or high <= 0 or low <= 0 or close <= 0:
            return
        prev_close = self._prev_close
        gap_bars = 0 if self._prev_bar_ts == 0 else (open_ts - self._prev_bar_ts) // max(self._bar_ms, 1)
        if self.atr and self.atr > 0 and prev_close and 0 < gap_bars <= 1:
            true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
            self.atr += (true_range - self.atr) / self.atr_period
            self._save_baseline()
        self._prev_bar_ts = open_ts
        self._prev_close = close

    def resolve(self) -> str:
        """確定並記住實際可用的合約代碼。"""
        self.symbol = resolve_symbol(self.session, self.symbol)
        return self.symbol

    def apply_calibration(self, snapshot: MarketSnapshot) -> None:
        """依 24h 成交額自動校準門檻（每 15 秒平均流量 × 係數，含地板／天花板）。

        BTC（$11.8B/24h）與 LINK（$150M/24h）流量差 ~80 倍，固定門檻不可用；
        校準後各標的的門檻都代表「同一統計強度」的異常流量。
        """
        if not self.auto_calibrate or snapshot.quote_volume_24h <= 0:
            return
        flow_15s = snapshot.quote_volume_24h / 86400.0 * 15.0
        window = self.calibration_factor * flow_15s
        window = min(self.max_window_usd, max(self.min_window_usd, window))
        changed = abs(window - self.window_usd) / max(self.window_usd, 1.0) > 0.20
        self.window_usd = window
        self.block_usd = window * 0.5
        self.momentum_usd = window * 2.0
        self.absorption_usd = window * 3.0
        self.divergence_usd = window * 3.0
        if changed:
            log.info(
                "門檻校準 %s：15s 窗口 $%s｜單筆 $%s｜動能60s $%s｜吸收60s $%s｜背離5m $%s",
                self.symbol,
                f"{self.window_usd:,.0f}",
                f"{self.block_usd:,.0f}",
                f"{self.momentum_usd:,.0f}",
                f"{self.absorption_usd:,.0f}",
                f"{self.divergence_usd:,.0f}",
            )

    def refresh_market_data(self, force: bool = False) -> None:
        """刷新 24h 高低點與 ATR（阻塞；請以 to_thread 呼叫）。

        防 418 封鎖設計（絕不在每筆成交上重試）：
        - 一般節流：距上次排程 < ORDERFLOW_TA_REFRESH_SEC 直接跳過
        - 失敗退避：指數退避（最高 3600s）；418/429 → 硬封鎖 3600s，
          連線重連的 force 刷新在此期間同樣跳過
        - 非阻塞鎖：同時間只允許一個刷新在執行
        """
        now = time.monotonic()
        if self.gate.blocked() or now < self._refresh_fail_until:
            # 418/429 封鎖期間：一般與強制刷新皆暫停；WS ticker/kline 仍持續供給基準
            self._load_baseline()
            self._restore_cached_context()
            return
        if not force and now < self._next_refresh_at:
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            try:
                snapshot = fetch_snapshot(self.session, self.symbol)
                rows = fetch_klines(
                    self.session, self.symbol, self.kline_interval, self.atr_period + 48
                )
                atr = compute_atr(rows, self.atr_period)
            except Exception as exc:  # noqa: BLE001 - 保留舊基準繼續運作
                self._refresh_failures += 1
                ban = parse_ban_seconds(exc, self.gate.default_ban_sec)
                if ban is not None:
                    self.gate.block_for(ban)
                    self._refresh_fail_until = time.monotonic() + ban
                    delay = ban
                    until_local = datetime.fromtimestamp(time.time() + ban).strftime("%H:%M")
                    log.warning(
                        "行情基準刷新失敗（REST 封鎖至約 %s；WebSocket 行情照常，不影響訊號）：%s",
                        until_local,
                        exc,
                    )
                else:
                    delay = min(self.ta_refresh_sec * (2 ** min(self._refresh_failures, 4)), 3600.0)
                    log.warning(
                        "行情基準刷新失敗（沿用舊值；%.0f 秒後再試，連續失敗 %d 次）：%s",
                        delay,
                        self._refresh_failures,
                        exc,
                    )
                self._next_refresh_at = time.monotonic() + delay
                self._load_baseline()
                return
            self.snapshot = snapshot
            self.atr = atr
            self._remember_closed_bar(rows)
            self._save_baseline()
            self.apply_calibration(snapshot)
            self._refresh_context()
            self._refresh_failures = 0
            self._baseline_warned = False
            self._next_refresh_at = time.monotonic() + self.ta_refresh_sec
            log.info(
                "行情基準已更新：%s 現價 $%.2f｜24h %.2f–%.2f｜ATR(%s,%d) $%.2f",
                self.symbol,
                snapshot.last_price,
                snapshot.low_24h,
                snapshot.high_24h,
                self.kline_interval,
                self.atr_period,
                atr,
            )
        finally:
            self._refresh_lock.release()

    def _refresh_context(self, force: bool = False) -> None:
        """抓取多周期上下文；遇 418/429 交由共享熔斷器，不再自行重試。"""
        if self.gate.blocked():
            return
        now = time.monotonic()
        if not force and now < self._next_context_at:
            return
        try:
            context = fetch_market_context(self.session, self.symbol)
        except Exception as exc:  # noqa: BLE001 - 上下文失敗不阻塞交易
            ban = parse_ban_seconds(exc, self.gate.default_ban_sec)
            if ban is not None:
                self.gate.block_for(ban)
                self._next_context_at = time.monotonic() + ban + random.uniform(0.0, 20.0)
                log.warning("%s 上下文失敗：全體 REST 熔斷 %.0f 秒（不再重試）", self.symbol, ban)
            else:
                self._next_context_at = time.monotonic() + 60.0 + random.uniform(0.0, 30.0)
                log.warning("%s 市場上下文取得失敗（60 秒後重試）：%s", self.symbol, exc)
            self._restore_cached_context()
            return
        self.context = context
        self._next_context_at = time.monotonic() + self.ta_refresh_sec
        save_context_cache(context)

    def _restore_cached_context(self) -> None:
        """限流／重啟時沿用上次成功的上下文（最多 6 小時），至少保留評級能力。"""
        if self.context is not None:
            return
        cached = load_context_cache(self.symbol)
        if cached is None:
            return
        self.context = cached
        age_min = (datetime.now(timezone.utc) - cached.fetched_at).total_seconds() / 60.0
        log.info("%s 沿用磁碟快取上下文（%.0f 分鐘前，限流期間暫代）", self.symbol, age_min)

    # ------------------------------------------------------------------ #
    # 純邏輯：逐筆成交 → 訊號判斷（可離線單元測試）
    # ------------------------------------------------------------------ #
    def process_trade(self, event: TradeEvent, now: float | None = None) -> OrderflowSignal | None:
        if event.price <= 0.0 or event.qty <= 0.0 or event.notional <= 0.0:
            self.stats["suppressed_bad_tick"] += 1
            return None
        self.stats["trades"] += 1
        self.stats["max_single_notional"] = max(self.stats["max_single_notional"], event.notional)

        # --- 多尺度滾動窗口維護 ---
        self.win15.add(event)
        self.win60.add(event)
        self.win300.add(event)
        self.stats["max_buy_flow"] = max(self.stats["max_buy_flow"], self.win15.buy)
        self.stats["max_sell_flow"] = max(self.stats["max_sell_flow"], self.win15.sell)

        # --- 15 秒大單統計（供 --ws-test 觀察）---
        single_hit = event.notional >= self.block_usd
        if single_hit:
            self.stats["single_blocks"] += 1
        prev_flow = (self.win15.buy - event.notional) if event.taker_buy else (self.win15.sell - event.notional)
        window_flow = self.win15.buy if event.taker_buy else self.win15.sell
        if window_flow >= self.window_usd and prev_flow < self.window_usd:
            self.stats["window_triggers"] += 1

        if self.snapshot is None or self.atr is None or self.atr <= 0:
            # 只提示一次，避免開盤瞬間每筆觸發都刷 WARNING
            if not self._baseline_warned:
                self._baseline_warned = True
                log.info("%s 行情基準載入中，暫停評估訊號（完成後自動恢復）", self.symbol)
            return None

        # --- 基準過期保護（避免用過期關鍵位發訊）---
        age = (datetime.now(timezone.utc) - self.snapshot.fetched_at).total_seconds()
        if age > self.snapshot_max_age_sec:
            self.stats["suppressed_stale"] += 1
            if time.time() - self._last_stale_warn >= 300:
                self._last_stale_warn = time.time()
                log.warning(
                    "行情基準已過期 %.0f 秒（> %d 秒），暫停發送新訊號",
                    age,
                    int(self.snapshot_max_age_sec),
                )
            return None

        clock = now if now is not None else time.monotonic()
        for candidate in self._build_candidates(event):
            signal = self._finalize(candidate, event, clock)
            if signal is not None:
                return signal
        return None

    # ------------------------------------------------------------------ #
    # 訊號偵測（四類確定性訊號；每筆成交最多發一種）
    # ------------------------------------------------------------------ #
    def _build_candidates(self, event: TradeEvent) -> list[SignalCandidate]:
        price = event.price
        high = self.snapshot.high_24h
        low = self.snapshot.low_24h
        dist_high = abs(high - price) / high * 100.0 if high > 0 else float("inf")
        dist_low = abs(price - low) / low * 100.0 if low > 0 else float("inf")
        near_high = dist_high <= self.proximity_pct
        near_low = dist_low <= self.proximity_pct
        candidates: list[SignalCandidate] = []

        # 1) 巨量吸收：60 秒雙向總量巨大但價格推不動，且貼著關鍵位但「未」突破
        if self.enable_absorption and (near_high or near_low):
            first_60 = self.win60.first_price()
            move_60 = abs(price - first_60) / first_60 * 100.0 if first_60 else float("inf")
            if self.win60.total >= self.absorption_usd and move_60 <= self.absorption_range_pct:
                buy_dominant = self.win60.buy >= self.win60.sell
                direction = ""
                level_kind = ""
                if near_low and price > low and not buy_dominant:
                    direction, level_kind = "long", "low"  # 低點賣壓被接走
                elif near_high and price < high and buy_dominant:
                    direction, level_kind = "short", "high"  # 高點買盤被吃掉
                if direction:
                    level_price = low if level_kind == "low" else high
                    candidates.append(
                        SignalCandidate(
                            kind="absorption",
                            direction=direction,
                            nature="關鍵位巨量吸收（掃單被接走）",
                            notional=self.win60.total,
                            qty=self.win60.buy_qty + self.win60.sell_qty,
                            trigger="absorption",
                            level_kind=level_kind,
                            level_price=level_price,
                            level_text=_level_text(level_kind, direction, level_price, price),
                        )
                    )

        # 2) 量價背離：5 分鐘累計 delta 與價格方向相反
        if self.enable_divergence:
            first_300 = self.win300.first_price()
            delta_300 = self.win300.delta
            if first_300 and abs(delta_300) >= self.divergence_usd:
                move_300 = (price - first_300) / first_300 * 100.0
                if delta_300 <= -self.divergence_usd and move_300 >= self.divergence_price_pct:
                    candidates.append(
                        SignalCandidate(
                            kind="divergence",
                            direction="short",
                            nature="量價背離（價漲量賣、疑似派發）",
                            notional=abs(delta_300),
                            qty=self.win300.sell_qty,
                            trigger="divergence",
                            level_kind="high",
                            level_price=high,
                            level_text=f"24h 高位區 ${high:,.2f}（距 {dist_high:.2f}%）",
                        )
                    )
                elif delta_300 >= self.divergence_usd and move_300 <= -self.divergence_price_pct:
                    candidates.append(
                        SignalCandidate(
                            kind="divergence",
                            direction="long",
                            nature="量價背離（價跌量買、疑似吸籌）",
                            notional=abs(delta_300),
                            qty=self.win300.buy_qty,
                            trigger="divergence",
                            level_kind="low",
                            level_price=low,
                            level_text=f"24h 低位區 ${low:,.2f}（距 {dist_low:.2f}%）",
                        )
                    )

        # 3) 動能突破：60 秒主動方向高度集中且價格創 24h 新高／新低
        if self.enable_momentum and self.win60.total > 0:
            delta_60 = self.win60.delta
            buy_share = self.win60.buy / self.win60.total
            if delta_60 >= self.momentum_usd and buy_share >= self.momentum_share and price >= high:
                candidates.append(
                    SignalCandidate(
                        kind="momentum",
                        direction="long",
                        nature="動能突破（主動買壓制）",
                        notional=self.win60.buy,
                        qty=self.win60.buy_qty,
                        trigger="momentum",
                        level_kind="high",
                        level_price=high,
                        level_text=f"突破 24h 高點 ${high:,.2f}（新高效應）",
                    )
                )
            elif -delta_60 >= self.momentum_usd and (1.0 - buy_share) >= self.momentum_share and price <= low:
                candidates.append(
                    SignalCandidate(
                        kind="momentum",
                        direction="short",
                        nature="動能突破（主動賣壓制）",
                        notional=self.win60.sell,
                        qty=self.win60.sell_qty,
                        trigger="momentum",
                        level_kind="low",
                        level_price=low,
                        level_text=f"跌破 24h 低點 ${low:,.2f}（新低效應）",
                    )
                )

        # 4) 原有：15 秒單筆／同向累計吃單 + 關鍵位過濾
        single_hit = event.notional >= self.block_usd
        window_flow = self.win15.buy if event.taker_buy else self.win15.sell
        window_hit = window_flow >= self.window_usd
        if not (single_hit or window_hit):
            return candidates
        if not (near_high or near_low):
            self.stats["suppressed_sr"] += 1
            log.debug(
                "大單 %.0f 未接近關鍵位（距高點 %.2f%%、距低點 %.2f%%），壓制",
                event.notional,
                dist_high,
                dist_low,
            )
            return candidates
        if near_high and near_low:
            level_kind = "high" if dist_high <= dist_low else "low"
        else:
            level_kind = "high" if near_high else "low"
        level_price = high if level_kind == "high" else low
        direction = "long" if event.taker_buy else "short"
        nature = (
            NATURE_DEFENSE
            if (direction == "long" and level_kind == "low") or (direction == "short" and level_kind == "high")
            else NATURE_SWEEP
        )
        if window_hit:
            notional_out = window_flow
            qty_out = sum(t.qty for t in self.win15.events if t.taker_buy == event.taker_buy)
        else:
            notional_out = event.notional
            qty_out = event.qty
        candidates.append(
            SignalCandidate(
                kind="sweep",
                direction=direction,
                nature=nature,
                notional=notional_out,
                qty=qty_out,
                trigger="window_flow" if window_hit else "single_block",
                level_kind=level_kind,
                level_price=level_price,
                level_text=_level_text(level_kind, direction, level_price, price),
            )
        )
        return candidates

    def _finalize(self, candidate: SignalCandidate, event: TradeEvent, clock: float) -> OrderflowSignal | None:
        """共用過濾（冷卻／全域間隔／同價推移）與訊號組裝。"""
        direction = candidate.direction
        price = event.price

        # --- 價格健全性（防止 0 價／離群價組出負數止盈）---
        if price <= 0.0 or not math.isfinite(price):
            self.stats["suppressed_bad_tick"] += 1
            log.debug("成交價異常（%r），丟棄", price)
            return None
        reference = self.snapshot.last_price if self.snapshot is not None else 0.0
        if reference > 0 and abs(price - reference) / reference > 0.25:
            self.stats["suppressed_bad_tick"] += 1
            log.warning("%s 成交價 $%.4f 偏離基準現價 $%.4f 超過 25%%，疑似異常行情，丟棄", self.symbol, price, reference)
            return None

        # --- 冷卻（每方向獨立）---
        if clock < self._cooldown_until[direction]:
            self.stats["suppressed_cooldown"] += 1
            log.debug("%s 方向冷卻中（剩餘 %.0f 秒），壓制", direction, self._cooldown_until[direction] - clock)
            return None

        # --- 全域最小間隔（任何方向；防連環刷頻）---
        if (clock - self._last_alert_at) < self.min_gap_sec:
            self.stats["suppressed_gap"] += 1
            log.debug(
                "距上次警報僅 %.0f 秒（< %d 秒全域間隔），壓制",
                clock - self._last_alert_at,
                int(self.min_gap_sec),
            )
            return None

        # --- 同方向需有足夠價格推移（避免同價位反覆觸發）---
        last_price = self._last_alert_price.get(direction)
        if last_price and abs(price - last_price) / last_price * 100.0 < self.min_move_pct:
            self.stats["suppressed_move"] += 1
            log.debug(
                "同方向警報價位僅推移 %.3f%%（< %.2f%%），壓制",
                abs(price - last_price) / last_price * 100.0,
                self.min_move_pct,
            )
            return None

        # --- 多周期評級（ADX / CVD / OI / 資金費率；確定性）---
        score, grade, reasons = grade_signal(self.context, direction, candidate.kind)
        if GRADE_RANK.get(grade, 1) < GRADE_RANK.get(self.min_grade, 0):
            self.stats["suppressed_grade"] += 1
            log.debug("評級 %s（%d 分）低於門檻 %s，壓制", grade, score, self.min_grade)
            return None

        # --- 全域警報預算（所有標的共用 1 小時上限）---
        if self.budget is not None and not self.budget.allow(clock):
            self.stats["suppressed_budget"] += 1
            log.debug("全域警報預算已滿（%d/小時），壓制", self.budget.max_per_hour)
            return None

        atr = self.atr
        if direction == "long":
            entry_low, entry_high = price - 0.05 * atr, price + 0.02 * atr
            tp1, tp2 = price + 1.5 * atr, price + 3.0 * atr
        else:
            entry_low, entry_high = price - 0.02 * atr, price + 0.05 * atr
            tp1, tp2 = price - 1.5 * atr, price - 3.0 * atr
        stop_loss = (
            price - self.atr_mult * self.atr if direction == "long" else price + self.atr_mult * self.atr
        )
        signal = OrderflowSignal(
            symbol=self.symbol,
            direction=direction,
            entry_price=price,
            stop_loss=stop_loss,
            atr=self.atr,
            atr_mult=self.atr_mult,
            nature=candidate.nature,
            notional=candidate.notional,
            qty=candidate.qty,
            trigger=candidate.trigger,
            level_kind=candidate.level_kind,
            level_price=candidate.level_price,
            level_text=candidate.level_text,
            trade_time=datetime.fromtimestamp(event.trade_time_ms / 1000.0, tz=timezone.utc),
            kind=candidate.kind,
            grade=grade,
            score=score,
            entry_low=entry_low,
            entry_high=entry_high,
            tp1=tp1,
            tp2=tp2,
            context_text=context_line(self.context, reasons),
        )
        self._cooldown_until[direction] = clock + self.cooldown_sec
        self._last_alert_at = clock
        self._last_alert_price[direction] = price
        self.stats["signals"] += 1
        self.stats[f"signal_{candidate.kind}"] = self.stats.get(f"signal_{candidate.kind}", 0) + 1
        return signal

    # ------------------------------------------------------------------ #
    # asyncio 常駐任務
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        """常駐監聽；斷線以指數退避重連（5s → 60s 上限）。"""
        backoff = 5.0
        while not self._stopping:
            try:
                await self._consume_stream()
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 重連語意
                log.warning("訂單流串流中斷：%s；%.0f 秒後重連", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _consume_stream(self) -> None:
        base = self.symbol.lower()
        # trade（成交）＋ ticker（24h 高低/成交額）＋ kline（ATR 增量）：後兩者讓 REST 完全非必需
        stream = f"{base}@trade/{base}@ticker/{base}@kline_{self.kline_interval}"
        uri = WS_COMBINED.format(streams=stream)
        # 先載入磁碟 ATR（若有）再載行情基準：REST 被封鎖時仍可發訊號
        self._load_baseline()
        await asyncio.to_thread(self.refresh_market_data, True)
        async with websockets.connect(
            uri,
            open_timeout=20,
            close_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_queue=8192,
        ) as socket:
            log.info("訂單流 WebSocket 已連線：%s", uri)

            async for raw in socket:
                if self._stopping:
                    return
                if time.monotonic() >= self._next_refresh_at:
                    await asyncio.to_thread(self.refresh_market_data, False)
                if (
                    self.context is None
                    and not self.gate.blocked()
                    and time.monotonic() >= self._next_context_at
                ):
                    await asyncio.to_thread(self._refresh_context, True)
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                data = message.get("data", message)
                event_type = data.get("e")
                if event_type == "24hrTicker":
                    self._apply_ws_ticker(data)
                    continue
                if event_type == "kline":
                    self._apply_ws_kline(data)
                    continue
                if event_type != "trade":
                    continue
                try:
                    event = parse_trade(data)
                except (KeyError, TypeError, ValueError):
                    continue
                signal = self.process_trade(event)
                if signal is not None:
                    await self._dispatch(signal)

    async def _dispatch(self, signal: OrderflowSignal) -> None:
        if signal.entry_price <= 0 or signal.stop_loss <= 0:
            log.warning(
                "%s 訊號價格異常（進場 $%.4f／止損 $%.4f），已攔截不發送",
                self.symbol,
                signal.entry_price,
                signal.stop_loss,
            )
            return
        text = format_orderflow_alert(signal)
        log.info(
            "⚡ 訂單流訊號 %s %s：$%.0f（%s，%.2f 手）｜進場 $%.2f → 止損 $%.2f",
            self.symbol,
            DIRECTION_LABELS[signal.direction],
            signal.notional,
            TRIGGER_LABELS[signal.trigger],
            signal.qty,
            signal.entry_price,
            signal.stop_loss,
        )
        if self.dispatcher is None:
            print("\n" + "─" * 56)
            print("[listen-only] 以下警報未發送：")
            print(text)
            print("─" * 56 + "\n")
            return
        ok = await asyncio.to_thread(self.dispatcher.send_alert, text, self.dry_run)
        if not ok:
            log.warning("訂單流警報發送失敗（%s %s）", self.symbol, signal.direction)

    # ------------------------------------------------------------------ #
    def stats_line(self) -> str:
        return (
            "trades=%d｜單筆巨量=%d｜窗口觸發=%d｜訊號=%d（sweep=%d/mom=%d/abs=%d/div=%d）"
            "｜S/R 壓制=%d｜冷卻壓制=%d｜間隔壓制=%d｜同價壓制=%d｜過期壓制=%d｜預算壓制=%d｜評級壓制=%d｜異常價=%d"
            "｜單筆最大=$%.0f｜買峰=$%.0f｜賣峰=$%.0f"
            % (
                self.stats["trades"],
                self.stats["single_blocks"],
                self.stats["window_triggers"],
                self.stats["signals"],
                self.stats["signal_sweep"],
                self.stats["signal_momentum"],
                self.stats["signal_absorption"],
                self.stats["signal_divergence"],
                self.stats["suppressed_sr"],
                self.stats["suppressed_cooldown"],
                self.stats["suppressed_gap"],
                self.stats["suppressed_move"],
                self.stats["suppressed_stale"],
                self.stats["suppressed_budget"],
                self.stats["suppressed_grade"],
                self.stats["suppressed_bad_tick"],
                self.stats["max_single_notional"],
                self.stats["max_buy_flow"],
                self.stats["max_sell_flow"],
            )
        )


def parse_trade(data: dict) -> TradeEvent:
    """Binance ``@trade`` 事件 → TradeEvent。

    ``m`` = 買方是否為做市商；``m=False`` 代表買方為 taker（主動買）。

    Binance 會偶爾推送 ``p="0" q="0" X="NA" st=1`` 的「無成交」佔位事件
    （實測 BNBUSDT 25 秒內出現 3 次）。這類事件必須丟棄，否則進場價、
    止損與止盈都會被算成 0。
    """
    price = float(data["p"])
    qty = float(data["q"])
    if price <= 0.0 or qty <= 0.0:
        raise ValueError(f"無效成交事件（p={price}, q={qty}, st={data.get('st')}）")
    return TradeEvent(
        trade_time_ms=int(data["T"]),
        price=price,
        qty=qty,
        taker_buy=(data.get("m") is False),
    )


# --------------------------------------------------------------------- #
# CLI 輔助（--orderflow / --ws-test）
# --------------------------------------------------------------------- #
def _print_snapshot(engine: OrderflowEngine) -> None:
    snapshot = engine.snapshot
    atr = engine.atr or 0.0
    price = snapshot.last_price if snapshot else float("nan")
    stop_long = price - engine.atr_mult * atr
    stop_short = price + engine.atr_mult * atr
    print(f"\n🛢  {symbol_label(engine.symbol)} 訂單流快照")
    print("─" * 64)
    print(f"合約代碼   : {engine.symbol}（Binance USDT-M 永續）")
    print(f"最新價格   : ${price:,.4f}")
    if snapshot:
        dist_high = abs(snapshot.high_24h - price) / snapshot.high_24h * 100
        dist_low = abs(price - snapshot.low_24h) / snapshot.low_24h * 100
        print(
            f"24h 高／低 : ${snapshot.high_24h:,.4f}（距 {dist_high:.2f}%）"
            f" / ${snapshot.low_24h:,.4f}（距 {dist_low:.2f}%）"
        )
        print(
            f"24h 成交額 : ${snapshot.quote_volume_24h:,.0f}"
            f"｜筆數 {snapshot.trade_count_24h:,}"
        )
    print(f"ATR({engine.kline_interval},{engine.atr_period}) : {atr:,.4f}")
    if engine.context is not None:
        print(f"多周期上下文: {context_line(engine.context)}")
    print(
        f"觸發門檻   : 單筆 >= ${engine.block_usd:,.0f}"
        f"｜{int(engine.window_ms / 1000)}s 同向累計 >= ${engine.window_usd:,.0f}"
        f"｜關鍵位 ±{engine.proximity_pct:.2f}%"
    )
    print(
        f"其他訊號   : 動能60s ${engine.momentum_usd:,.0f}"
        f"｜吸收60s ${engine.absorption_usd:,.0f}"
        f"｜背離5m ${engine.divergence_usd:,.0f}"
    )
    print(f"止損示例   : 做多 ${stop_long:,.4f} / 做空 ${stop_short:,.4f}（{engine.atr_mult:g}×ATR）")
    print("─" * 64)


def _print_snapshot_table(engines: list[OrderflowEngine]) -> None:
    print("\n🛢  多標的訂單流快照（門檻依 24h 成交額自動校準）")
    print("─" * 100)
    print(
        f"{'標的':<12}{'現價':>14}{'24h 低–高':>28}{'ATR':>10}"
        f"{'單筆門檻':>14}{'15s 窗口':>14}{'距高/低':>16}"
    )
    for engine in engines:
        snapshot = engine.snapshot
        if snapshot is None:
            print(f"{symbol_label(engine.symbol):<12}資料不可用")
            continue
        dist_high = abs(snapshot.high_24h - snapshot.last_price) / snapshot.high_24h * 100
        dist_low = abs(snapshot.last_price - snapshot.low_24h) / snapshot.low_24h * 100
        print(
            f"{symbol_label(engine.symbol):<12}{snapshot.last_price:>14,.4f}"
            f"{f'{snapshot.low_24h:,.2f}–{snapshot.high_24h:,.2f}':>28}"
            f"{engine.atr or 0:>10,.4f}"
            f"{engine.block_usd:>14,.0f}{engine.window_usd:>14,.0f}"
            f"{f'{dist_high:.2f}%/{dist_low:.2f}%':>16}"
        )
    print("─" * 100)


def run_orderflow_cli(
    cfg,
    dispatcher=None,
    *,
    dry_run: bool = False,
    force: bool = False,
    symbol: str | None = None,
) -> int:
    """``--orderflow``：多標的快照；``--symbol`` 指定單一標的詳細資料；``--force`` 發示範警報。"""
    symbols = [symbol.upper()] if symbol else list(cfg.binance_symbols)
    gate = RestGate()
    engines: list[OrderflowEngine] = []
    for code in symbols:
        engine = OrderflowEngine(cfg, dispatcher, symbol=code, dry_run=dry_run, gate=gate)
        try:
            engine.resolve()
            engine.refresh_market_data(force=True)
            engine._refresh_context(force=True)
        except Exception as exc:  # noqa: BLE001 - 單一標的失敗不影響其他
            print(f"❌ {code} 初始化失敗：{exc}")
            continue
        if engine.snapshot is None or engine.atr is None:
            print(f"❌ {code} 行情基準不可用")
            continue
        engines.append(engine)
        time.sleep(0.15)  # 錯峰查詢，降低瞬時權重
    if not engines:
        return 1
    if symbol:
        _print_snapshot(engines[0])
    else:
        _print_snapshot_table(engines)

    if force:
        engine = engines[0]
        snapshot = engine.snapshot
        price = snapshot.last_price
        dist_high = abs(snapshot.high_24h - price) / snapshot.high_24h
        dist_low = abs(price - snapshot.low_24h) / snapshot.low_24h
        level_kind = "high" if dist_high <= dist_low else "low"
        direction = "long" if level_kind == "low" else "short"
        level_price = snapshot.high_24h if level_kind == "high" else snapshot.low_24h
        stop_loss = (
            price - engine.atr_mult * engine.atr
            if direction == "long"
            else price + engine.atr_mult * engine.atr
        )
        sample = OrderflowSignal(
            symbol=engine.symbol,
            direction=direction,
            entry_price=price,
            stop_loss=stop_loss,
            atr=engine.atr,
            atr_mult=engine.atr_mult,
            nature=NATURE_DEFENSE if direction == "long" and level_kind == "low" else NATURE_SWEEP,
            notional=engine.window_usd,
            qty=engine.window_usd / price,
            trigger="window_flow",
            level_kind=level_kind,
            level_price=level_price,
            level_text=_level_text(level_kind, direction, level_price, price),
            trade_time=datetime.now(timezone.utc),
        )
        text = format_orderflow_alert(sample)
        if dispatcher is None:
            print("[dry-run] 示範警報（未發送）：\n" + text)
            return 0
        ok = dispatcher.send_alert(text, dry_run=dry_run)
        print("✅ 示範警報已發送" if ok and not dry_run else ("（dry-run）" if ok else "❌ 發送失敗"))
        return 0 if ok else 1
    return 0


def run_ws_test(cfg, seconds: float = 30.0, symbol: str | None = None) -> int:
    """``--ws-test``：實際監聽 N 秒（預設全部標的），印統計與 listen-only 警報，不發送任何訊息。"""
    symbols = [symbol.upper()] if symbol else list(cfg.binance_symbols)

    async def runner() -> list[OrderflowEngine]:
        engines: list[OrderflowEngine] = []
        gate = RestGate()
        for code in symbols:
            engine = OrderflowEngine(cfg, dispatcher=None, symbol=code, dry_run=True, gate=gate)
            try:
                await asyncio.to_thread(engine.resolve)
            except Exception as exc:  # noqa: BLE001 - 限流時沿用預設代碼
                log.warning("%s 代碼解析失敗（%s），沿用預設", code, exc)
            engines.append(engine)
            await asyncio.sleep(0.2)  # 錯峰連線，避免同時觸發 REST 刷新
        tasks = [asyncio.create_task(engine.run_forever()) for engine in engines]
        try:
            await asyncio.sleep(seconds)
        finally:
            for engine in engines:
                engine.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        return engines

    engines = asyncio.run(runner())
    print(f"\n📡 監聽 {seconds:.0f} 秒統計（{len(engines)} 個標的）：")
    total_trades = 0
    total_signals = 0
    for engine in engines:
        print(f"   {symbol_label(engine.symbol):<12} {engine.stats_line()}")
        total_trades += int(engine.stats["trades"])
        total_signals += int(engine.stats["signals"])
    print(f"   合計：trades={total_trades}｜訊號={total_signals}")
    return 0
