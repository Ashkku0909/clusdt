"""CLI 入口：原油即時新聞 + 資金流監控 Telegram 機器人（100% 確定性、零 LLM）。

用法範例：
    python main.py --health                 # 資料源連線健檢
    python main.py --ping                   # 發送連線測試訊息
    python main.py --news                   # 即時新聞掃描（20 分鐘時間窗 + 內文直譯）
    python main.py --flow                   # 盤中量價資金流掃描（CL=F 5 分鐘 K 線）
    python main.py --cftc                   # CFTC 週度 Managed Money 籌碼
    python main.py --once                   # 一輪完整掃描（新聞 + 資金流 + CFTC）
    python main.py --loop --interval 300    # 常駐：每 5 分鐘一輪（建議值）
    python main.py --serve                  # Render 模式：FastAPI /health + 背景輪詢

Render／UptimeRobot：`uvicorn main:app --host 0.0.0.0 --port $PORT`（見 Procfile / render.yaml），
`GET /health` 回傳 {"status": "ok"} 供外部 keep-alive 探測。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import BASE_DIR, Config, REQUIRED_FIELDS, load_config
from flow_monitor import run_cftc_cycle, run_flow_cycle
from health import run_health
from news_pipeline import run_news_cycle
from pipeline import Tools, run_full_cycle
from state import StateStore
from telegram_dispatcher import TelegramDispatcher

log = logging.getLogger(__name__)

PING_MESSAGE = (
    "⚡ **【原油與宏觀能源即時情報】**\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "✅ **系統連線測試成功**\n"
    "• 本則為 clusdt 情報機器人的連線驗證訊息。\n"
    "• 後續能源相關情報將依格式自動推播。\n"
    "━━━━━━━━━━━━━━━━━━"
)


# --------------------------------------------------------------------- #
# Render / UptimeRobot：FastAPI keep-alive 端點 + 背景輪詢迴圈
# --------------------------------------------------------------------- #
def _background_worker(interval: int) -> None:
    """在 daemon 執行緒中持續執行完整週期（新聞 + 資金流 + CFTC）。"""
    try:
        cfg = load_config()
        _setup_logging(cfg.log_level)
        tools = _build_tools(cfg)
    except Exception:
        logging.getLogger(__name__).exception("背景迴圈初始化失敗")
        return
    log.info("背景輪詢啟動：每 %d 秒一輪", interval)
    while True:
        try:
            sent = run_full_cycle(cfg, tools)
            log.info("本輪推播 %d 則", sent)
        except Exception:
            log.exception("本輪執行失敗，將於下一輪重試")
        time.sleep(interval)


_worker_lock = threading.Lock()
_worker_started = False


def _start_worker_once() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    interval = int(os.getenv("LOOP_INTERVAL_SECONDS", "300"))
    threading.Thread(target=_background_worker, args=(interval,), daemon=True).start()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _start_worker_once()
    yield


app = FastAPI(title="clusdt crude oil bot", lifespan=_lifespan)


@app.get("/")
def root() -> dict:
    return {"status": "ok"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="原油與宏觀能源即時情報 Telegram 推播機器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="執行一輪完整掃描（預設行為）：新聞 + 資金流 + CFTC")
    mode.add_argument("--loop", action="store_true", help="持續循環執行")
    mode.add_argument("--news", action="store_true", help="只執行即時新聞掃描（嚴格 20 分鐘時間窗）")
    mode.add_argument("--flow", action="store_true", help="只執行盤中量價資金流掃描（純數據）")
    mode.add_argument("--cftc", action="store_true", help="只檢查 CFTC 週度 Managed Money 籌碼")
    mode.add_argument("--health", action="store_true", help="檢查各資料源連線狀態")
    mode.add_argument("--ping", action="store_true", help="發送一則連線測試訊息到 Telegram")
    mode.add_argument("--serve", action="store_true", help="啟動 FastAPI（Render keep-alive）+ 背景輪詢")

    parser.add_argument("--interval", type=int, default=300, help="--loop 的間隔秒數（預設 300）")
    parser.add_argument("--limit", type=int, default=None, help="每輪最多推播則數（預設讀 MAX_ALERTS_PER_RUN）")
    parser.add_argument("--dry-run", action="store_true", help="只顯示訊息，不實際發送、不更新狀態")
    parser.add_argument("--force", action="store_true", help="忽略門檻與去重（搭配 --flow / --cftc）")
    return parser.parse_args(argv)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _fix_console_encoding() -> None:
    """Windows 主控台輸出中文/emoji 時避免編碼錯誤。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 部分環境無 reconfigure
            pass


def _require_env(cfg: Config) -> None:
    missing = [env_name for attr, env_name in REQUIRED_FIELDS if not getattr(cfg, attr)]
    if missing:
        raise SystemExit(f"❌ .env 缺少必要欄位：{', '.join(missing)}")


def _build_tools(cfg: Config) -> Tools:
    """組裝推播所需元件：Telegram 發送層與狀態儲存。"""
    return Tools(
        tg=TelegramDispatcher(
            cfg.telegram_bot_token,
            cfg.telegram_chat_id,
            cfg.telegram_proxy_url,
        ),
        state=StateStore(cfg.data_dir / "state.json"),
    )


def main(argv=None) -> int:
    _fix_console_encoding()
    args = _parse_args(argv)

    if not (BASE_DIR / ".env").exists():
        raise SystemExit("❌ 找不到 .env；請先複製 .env.example 為 .env 並填入金鑰。")

    cfg = load_config()
    _setup_logging(cfg.log_level)

    if args.health:
        return run_health(cfg)

    _require_env(cfg)

    if args.serve:
        import uvicorn

        port = int(os.getenv("PORT", "8000"))
        log.info("啟動 Uvicorn 服務（host=0.0.0.0, port=%d）", port)
        uvicorn.run("main:app", host="0.0.0.0", port=port)
        return 0

    tools = _build_tools(cfg)

    if args.ping:
        ok = tools.tg.send_alert(PING_MESSAGE, dry_run=args.dry_run)
        log.info("--ping %s", "完成" if ok else "失敗")
        return 0 if ok else 1

    if args.news:
        sent = run_news_cycle(cfg, tools.tg, tools.state, dry_run=args.dry_run, limit=args.limit)
        log.info("即時新聞掃描完成：推播 %d 則", sent)
        return 0

    if args.flow:
        sent = run_flow_cycle(cfg, tools.tg, tools.state, dry_run=args.dry_run, force=args.force)
        log.info("資金流掃描完成：推播 %d 則", sent)
        return 0

    if args.cftc:
        sent = run_cftc_cycle(cfg, tools.tg, tools.state, dry_run=args.dry_run, force=args.force)
        log.info("CFTC 籌碼檢查完成：推播 %d 則", sent)
        return 0

    if args.loop:
        interval = max(60, args.interval)
        log.info("進入循環模式（每 %d 秒一輪，Ctrl+C 停止）", interval)
        try:
            while True:
                sent = run_full_cycle(cfg, tools, dry_run=args.dry_run, limit=args.limit)
                log.info("本輪推播 %d 則；%d 秒後再掃描", sent, interval)
                time.sleep(interval)
        except KeyboardInterrupt:
            log.info("已手動停止")
            return 0

    sent = run_full_cycle(cfg, tools, dry_run=args.dry_run, limit=args.limit)
    log.info("完成：本輪推播 %d 則", sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
