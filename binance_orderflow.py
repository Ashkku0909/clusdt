"""幣安 USDT-M 合約即時訂單流引擎（CL 原油；100% 確定性、零 LLM）。

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
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Iterable

import requests
import websockets

log = logging.getLogger(__name__)

REST_BASE = "https://fapi.binance.com"
WS_COMBINED = "wss://fstream.binance.com/stream?streams={streams}"

# 依序探測：CLUSDT 為 Binance 現行「原油 (CL)」永續合約代碼
CANDIDATE_SYMBOLS: tuple[str, ...] = ("CLUSDT", "OILUSDT")
FALLBACK_BASE_ASSETS: tuple[str, ...] = ("CL", "OIL", "WTI", "BRENT", "CRUDE")

DIRECTION_LABELS = {"long": "做多 🟢", "short": "做空 🔴"}

# 開頭號誌燈（紅綠燈語意）：做多 = 綠燈 🟢、做空 = 紅燈 🔴
DIRECTION_LIGHTS = {"long": "🟢", "short": "🔴"}

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

TRIGGER_LABELS = {"single_block": "單筆巨量吃單", "window_flow": "15 秒內同向連續吃單"}


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
    """確定實際可用的 Binance 合約代碼（CLUSDT / OILUSDT / exchangeInfo 掃描）。"""
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
def format_orderflow_alert(signal: OrderflowSignal) -> str:
    """依既定模板輸出警報（純文字，Telegram 端僅做 Markdown→HTML 轉義）。"""
    return "\n".join(
        [
            f"{DIRECTION_LIGHTS[signal.direction]} 【幣安 CL原油 異動】",
            "━━━━━━━━━━━━━━━━━━",
            f"方向：{DIRECTION_LABELS[signal.direction]}",
            f"進場位：${signal.entry_price:,.2f}",
            f"止損位：${signal.stop_loss:,.2f} (ATR {signal.atr_mult:g}x)",
            f"性質：{signal.nature}",
            f"大單金額：${signal.notional:,.0f}",
            f"成交量：{signal.qty:,.2f} 手",
            f"關鍵位置：{signal.level_text}",
            "━━━━━━━━━━━━━━━━━━",
        ]
    )


# --------------------------------------------------------------------- #
# 訂單流引擎
# --------------------------------------------------------------------- #
class OrderflowEngine:
    """Binance 逐筆成交 → 大單／連續吃單偵測 → 固定模板警報。"""

    def __init__(
        self,
        cfg,
        dispatcher=None,
        *,
        symbol: str | None = None,
        dry_run: bool = False,
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

        # 15 秒滾動窗口（依 trade_time 剪枝，維持同方向名義金額累計）
        self._window: Deque[TradeEvent] = deque()
        self._buy_flow = 0.0
        self._sell_flow = 0.0
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
            "max_single_notional": 0.0,
            "max_buy_flow": 0.0,
            "max_sell_flow": 0.0,
        }

    # ------------------------------------------------------------------ #
    # 市場資料（阻塞；請以 to_thread 呼叫）
    # ------------------------------------------------------------------ #
    def resolve(self) -> str:
        """確定並記住實際可用的合約代碼。"""
        self.symbol = resolve_symbol(self.session, self.symbol)
        return self.symbol

    def refresh_market_data(self, force: bool = False) -> None:
        """刷新 24h 高低點與 ATR（阻塞；請以 to_thread 呼叫）。

        防 418 封鎖設計（絕不在每筆成交上重試）：
        - 一般節流：距上次排程 < ORDERFLOW_TA_REFRESH_SEC 直接跳過
        - 失敗退避：指數退避（最高 3600s）；418/429 → 硬封鎖 3600s，
          連線重連的 force 刷新在此期間同樣跳過
        - 非阻塞鎖：同時間只允許一個刷新在執行
        """
        now = time.monotonic()
        if now < self._refresh_fail_until:
            return  # 418/429 封鎖期間：一般與強制刷新皆暂停
        if not force and now < self._next_refresh_at:
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            try:
                snapshot = fetch_snapshot(self.session, self.symbol)
                atr = fetch_atr(self.session, self.symbol, self.kline_interval, self.atr_period)
            except Exception as exc:  # noqa: BLE001 - 保留舊基準繼續運作
                self._refresh_failures += 1
                message = str(exc)
                banned = any(token in message for token in ("418", "429", "banned"))
                if banned:
                    self._refresh_fail_until = time.monotonic() + 3600.0
                    delay = 3600.0
                else:
                    delay = min(self.ta_refresh_sec * (2 ** min(self._refresh_failures, 4)), 3600.0)
                self._next_refresh_at = time.monotonic() + delay
                log.warning(
                    "行情基準刷新失敗（沿用舊值；%.0f 秒後再試，連續失敗 %d 次）：%s",
                    delay,
                    self._refresh_failures,
                    exc,
                )
                return
            self.snapshot = snapshot
            self.atr = atr
            self._refresh_failures = 0
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

    # ------------------------------------------------------------------ #
    # 純邏輯：逐筆成交 → 訊號判斷（可離線單元測試）
    # ------------------------------------------------------------------ #
    def process_trade(self, event: TradeEvent, now: float | None = None) -> OrderflowSignal | None:
        self.stats["trades"] += 1
        self.stats["max_single_notional"] = max(self.stats["max_single_notional"], event.notional)

        # --- 15 秒滾動窗口維護 ---
        cutoff = event.trade_time_ms - self.window_ms
        while self._window and self._window[0].trade_time_ms < cutoff:
            expired = self._window.popleft()
            if expired.taker_buy:
                self._buy_flow -= expired.notional
            else:
                self._sell_flow -= expired.notional
        self._window.append(event)
        # --- 觸發條件（窗口僅在「跨越門檻的那一筆」計一次，避免同一波重複累計）---
        prev_flow = self._buy_flow if event.taker_buy else self._sell_flow
        if event.taker_buy:
            self._buy_flow += event.notional
        else:
            self._sell_flow += event.notional
        window_flow = self._buy_flow if event.taker_buy else self._sell_flow
        self.stats["max_buy_flow"] = max(self.stats["max_buy_flow"], self._buy_flow)
        self.stats["max_sell_flow"] = max(self.stats["max_sell_flow"], self._sell_flow)

        single_hit = event.notional >= self.block_usd
        if single_hit:
            self.stats["single_blocks"] += 1
        window_hit = window_flow >= self.window_usd
        if window_hit and prev_flow < self.window_usd:
            self.stats["window_triggers"] += 1
        if not (single_hit or window_hit):
            return None

        if self.snapshot is None or self.atr is None or self.atr <= 0:
            log.warning("觸發大單但行情基準尚未就緒，略過本筆")
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

        # --- 24h 高低點位置過濾（僅在關鍵位附近才發訊）---
        price = event.price
        high = self.snapshot.high_24h
        low = self.snapshot.low_24h
        dist_high = abs(high - price) / high * 100.0 if high > 0 else float("inf")
        dist_low = abs(price - low) / low * 100.0 if low > 0 else float("inf")
        near_high = dist_high <= self.proximity_pct
        near_low = dist_low <= self.proximity_pct
        if not (near_high or near_low):
            self.stats["suppressed_sr"] += 1
            log.debug(
                "大單 %.0f 未接近關鍵位（距高點 %.2f%%、距低點 %.2f%%），壓制",
                event.notional,
                dist_high,
                dist_low,
            )
            return None
        if near_high and near_low:
            level_kind = "high" if dist_high <= dist_low else "low"
        else:
            level_kind = "high" if near_high else "low"
        level_price = high if level_kind == "high" else low

        # --- 冷卻（每方向獨立）---
        direction = "long" if event.taker_buy else "short"
        clock = now if now is not None else time.monotonic()
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

        # --- 組裝訊號 ---
        trigger = "window_flow" if window_hit else "single_block"
        if window_hit:
            notional_out = window_flow
            qty_out = sum(t.qty for t in self._window if t.taker_buy == event.taker_buy)
        else:
            notional_out = event.notional
            qty_out = event.qty

        nature = (
            NATURE_DEFENSE
            if (direction == "long" and level_kind == "low") or (direction == "short" and level_kind == "high")
            else NATURE_SWEEP
        )
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
            nature=nature,
            notional=notional_out,
            qty=qty_out,
            trigger=trigger,
            level_kind=level_kind,
            level_price=level_price,
            level_text=_level_text(level_kind, direction, level_price, price),
            trade_time=datetime.fromtimestamp(event.trade_time_ms / 1000.0, tz=timezone.utc),
        )
        self._cooldown_until[direction] = clock + self.cooldown_sec
        self._last_alert_at = clock
        self._last_alert_price[direction] = price
        self.stats["signals"] += 1
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
        stream = f"{self.symbol.lower()}@trade"
        uri = WS_COMBINED.format(streams=stream)
        async with websockets.connect(
            uri,
            open_timeout=20,
            close_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_queue=8192,
        ) as socket:
            log.info("訂單流 WebSocket 已連線：%s", uri)
            await asyncio.to_thread(self.refresh_market_data, True)

            async for raw in socket:
                if self._stopping:
                    return
                if time.monotonic() >= self._next_refresh_at:
                    await asyncio.to_thread(self.refresh_market_data, False)
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                data = message.get("data", message)
                if data.get("e") != "trade":
                    continue
                try:
                    event = parse_trade(data)
                except (KeyError, TypeError, ValueError):
                    continue
                signal = self.process_trade(event)
                if signal is not None:
                    await self._dispatch(signal)

    async def _dispatch(self, signal: OrderflowSignal) -> None:
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
            "trades=%d｜單筆巨量=%d｜窗口觸發=%d｜訊號=%d"
            "｜S/R 壓制=%d｜冷卻壓制=%d｜間隔壓制=%d｜同價壓制=%d｜過期壓制=%d"
            "｜單筆最大=$%.0f｜買峰=$%.0f｜賣峰=$%.0f"
            % (
                self.stats["trades"],
                self.stats["single_blocks"],
                self.stats["window_triggers"],
                self.stats["signals"],
                self.stats["suppressed_sr"],
                self.stats["suppressed_cooldown"],
                self.stats["suppressed_gap"],
                self.stats["suppressed_move"],
                self.stats["suppressed_stale"],
                self.stats["max_single_notional"],
                self.stats["max_buy_flow"],
                self.stats["max_sell_flow"],
            )
        )


def parse_trade(data: dict) -> TradeEvent:
    """Binance ``@trade`` 事件 → TradeEvent。

    ``m`` = 買方是否為做市商；``m=False`` 代表買方為 taker（主動買）。
    """
    return TradeEvent(
        trade_time_ms=int(data["T"]),
        price=float(data["p"]),
        qty=float(data["q"]),
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
    print("\n🛢  幣安訂單流快照")
    print("─" * 64)
    print(f"合約代碼   : {engine.symbol}（Binance USDT-M 永續）")
    print(f"最新價格   : ${price:,.2f}")
    if snapshot:
        dist_high = abs(snapshot.high_24h - price) / snapshot.high_24h * 100
        dist_low = abs(price - snapshot.low_24h) / snapshot.low_24h * 100
        print(
            f"24h 高／低 : ${snapshot.high_24h:,.2f}（距 {dist_high:.2f}%）"
            f" / ${snapshot.low_24h:,.2f}（距 {dist_low:.2f}%）"
        )
        print(
            f"24h 成交額 : ${snapshot.quote_volume_24h:,.0f}"
            f"｜筆數 {snapshot.trade_count_24h:,}"
        )
    print(f"ATR({engine.kline_interval},{engine.atr_period}) : ${atr:,.2f}")
    print(
        f"觸發門檻   : 單筆 >= ${engine.block_usd:,.0f}"
        f"｜{int(engine.window_ms / 1000)}s 同向累計 >= ${engine.window_usd:,.0f}"
        f"｜關鍵位 ±{engine.proximity_pct:.2f}%"
    )
    print(f"止損示例   : 做多 ${stop_long:,.2f} / 做空 ${stop_short:,.2f}（{engine.atr_mult:g}×ATR）")
    print("─" * 64)


def run_orderflow_cli(
    cfg,
    dispatcher=None,
    *,
    dry_run: bool = False,
    force: bool = False,
    symbol: str | None = None,
) -> int:
    """``--orderflow``：一次性快照（不連 WebSocket）；--force 額外發送示範警報。"""
    engine = OrderflowEngine(cfg, dispatcher, symbol=symbol, dry_run=dry_run)
    engine.resolve()
    engine.refresh_market_data(force=True)
    _print_snapshot(engine)

    if engine.snapshot is None or engine.atr is None:
        print("❌ 無法取得行情基準（REST 失敗），已中止。")
        return 1

    if force:
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
    """``--ws-test``：實際監聽 N 秒，印出統計與（listen-only）警報，不發送任何訊息。"""

    async def runner() -> OrderflowEngine:
        engine = OrderflowEngine(cfg, dispatcher=None, symbol=symbol, dry_run=True)
        try:
            await asyncio.to_thread(engine.resolve)
        except Exception as exc:  # noqa: BLE001 - 限流時沿用預設代碼
            log.warning("合約代碼解析失敗（%s），沿用預設 %s", exc, engine.symbol)
        task = asyncio.create_task(engine.run_forever())
        try:
            await asyncio.sleep(seconds)
        finally:
            engine.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return engine

    engine = asyncio.run(runner())
    print(f"\n📡 {engine.symbol} 監聽 {seconds:.0f} 秒統計：")
    print("   " + engine.stats_line())
    return 0
