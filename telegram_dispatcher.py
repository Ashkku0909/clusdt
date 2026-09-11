"""Telegram 發送層（規格：429 退避、網路逾時靜默丟棄、傳送間隔）。

繼承 `TelegramClient`（Markdown→HTML 轉換、長訊息分段、解析失敗純文字退回），
並實作確定性發送語意：

- 429（限流）：依 `retry_after` 退避；超過重試上限即放棄該則（回傳 False，不拋出）
- 網路逾時（requests.Timeout）：記錄後「靜默丟棄」，不重試、不拋出例外
- 其他連線錯誤：最多重試一次
- 全域最小發送間隔，避免連續發送觸發限流
"""

from __future__ import annotations

import logging
import time

import requests

from telegram_client import TelegramClient

log = logging.getLogger(__name__)


class TelegramDispatcher(TelegramClient):
    def __init__(
        self,
        token: str,
        chat_id: str,
        proxy_url: str = "",
        min_gap_seconds: float = 1.0,
        max_rate_limit_retries: int = 2,
    ):
        super().__init__(token, chat_id, proxy_url)
        self.min_gap_seconds = min_gap_seconds
        self.max_rate_limit_retries = max_rate_limit_retries
        self._last_send_at = 0.0

    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        wait = self.min_gap_seconds - (time.monotonic() - self._last_send_at)
        if wait > 0:
            time.sleep(wait)

    def _send_chunk(self, chunk: str, parse_mode: str | None) -> str:
        """回傳 'ok' / 'parse' / 'error'；逾時靜默丟棄。"""
        payload = {
            "chat_id": self.chat_id,
            "text": chunk,
            "disable_web_page_preview": False,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        rate_limited = 0
        for attempt in range(2):
            self._throttle()
            try:
                response = self._post("sendMessage", payload)
            except requests.Timeout as exc:
                log.warning("Telegram 逾時，靜默丟棄本則訊息：%s", exc)
                return "error"
            except requests.RequestException as exc:
                log.warning("Telegram 連線錯誤（第 %d 次）：%s", attempt + 1, exc)
                time.sleep(1.0)
                continue

            self._last_send_at = time.monotonic()

            if response.ok:
                try:
                    if response.json().get("ok", True):
                        return "ok"
                except ValueError:
                    return "ok"

            description = ""
            try:
                description = response.json().get("description", "")
            except ValueError:
                description = response.text[:200]

            if response.status_code == 400 and "parse" in description.lower():
                return "parse"
            if response.status_code == 429:
                rate_limited += 1
                if rate_limited > self.max_rate_limit_retries:
                    log.error("Telegram 429 超過重試上限，放棄本則訊息")
                    return "error"
                retry_after = 5
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
                except (ValueError, TypeError):
                    pass
                log.warning("Telegram 429 限流，%d 秒後重試", retry_after)
                time.sleep(retry_after + 1)
                continue
            if response.status_code >= 500:
                log.warning("Telegram HTTP %d，稍後重試", response.status_code)
                time.sleep(2.0 * (attempt + 1))
                continue

            log.error("Telegram 發送失敗 HTTP %d：%s", response.status_code, description)
            return "error"

        return "error"
