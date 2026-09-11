"""推播流程（100% 確定性、零 LLM）：即時新聞 + 資金流 + CFTC。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import Config
from flow_monitor import run_cftc_cycle, run_flow_cycle
from news_pipeline import run_news_cycle
from state import StateStore
from telegram_dispatcher import TelegramDispatcher

log = logging.getLogger(__name__)


@dataclass
class Tools:
    """一輪推播所需的共用元件（由 main 組裝）。"""

    tg: TelegramDispatcher
    state: StateStore


def run_full_cycle(cfg: Config, tools: Tools, dry_run: bool = False, limit: int | None = None) -> int:
    """完整一輪：即時新聞（嚴格時間窗）+ 盤中資金流 + CFTC 週度籌碼。"""
    sent = run_news_cycle(cfg, tools.tg, tools.state, dry_run=dry_run, limit=limit)
    sent += run_flow_cycle(cfg, tools.tg, tools.state, dry_run=dry_run)
    sent += run_cftc_cycle(cfg, tools.tg, tools.state, dry_run=dry_run)
    return sent



