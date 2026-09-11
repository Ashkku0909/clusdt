"""Telegram 推播：Markdown→HTML 轉換、長訊息分段、代理與重試。

選擇 HTML 解析模式的原因：Master Prompt 產出使用 `**粗體**`，
Telegram 舊版 Markdown 對 `**` 支援不佳、且未跳脫的 `_`／`*` 容易
觸發 "can't parse entities" 錯誤；HTML 模式最穩定。
若仍解析失敗，會自動退回收純文字重送，確保情報一定送達。
"""

from __future__ import annotations

import html
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4000  # Telegram 上限 4096，留緩衝

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_TAG_RE = re.compile(r"<[^>]+>")


def markdown_to_html(text: str) -> str:
    """把快報的輕量 Markdown 轉成 Telegram 可解析的 HTML。"""
    out = html.escape(text, quote=True)
    out = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', out)
    out = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", out)
    out = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    return out


def _plain_text(html_text: str) -> str:
    return html.unescape(_TAG_RE.sub("", html_text))


def _chunks(text: str, size: int = MAX_MESSAGE_LEN):
    """依行切分長訊息，避免超過 Telegram 單則上限。"""
    if len(text) <= size:
        yield text
        return
    buffer = ""
    for line in text.split("\n"):
        while len(line) > size:  # 單行極端過長
            if buffer:
                yield buffer
                buffer = ""
            yield line[:size]
            line = line[size:]
        candidate = f"{buffer}\n{line}" if buffer else line
        if len(candidate) > size:
            yield buffer
            buffer = line
        else:
            buffer = candidate
    if buffer:
        yield buffer


class TelegramClient:
    """封裝 Telegram Bot API 的發送端。"""

    def __init__(self, token: str, chat_id: str, proxy_url: str = ""):
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未設定")
        self.token = token
        self.chat_id = chat_id
        self.proxy_url = proxy_url
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.session = requests.Session()

    # ------------------------------------------------------------------ #
    def _post(self, method: str, payload: dict, timeout: int = 15) -> requests.Response:
        url = f"{API_BASE}/bot{self.token}/{method}"
        return self.session.post(url, json=payload, proxies=self.proxies, timeout=timeout)

    def get_me(self) -> tuple[int, dict]:
        try:
            response = self._post("getMe", {}, timeout=10)
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    # ------------------------------------------------------------------ #
    def send_alert(self, markdown_text: str, dry_run: bool = False) -> bool:
        """發送一則快報；回傳是否成功。dry_run 只印出內容。"""
        if not markdown_text.strip():
            log.error("空白訊息，略過推播")
            return False
        if dry_run:
            print("\n" + "─" * 56)
            print("[dry-run] 以下內容不會實際發送：")
            print(markdown_text)
            print("─" * 56 + "\n")
            return True

        html_text = markdown_to_html(markdown_text)
        for chunk in _chunks(html_text):
            status = self._send_chunk(chunk, "HTML")
            if status == "parse":
                log.warning("HTML 解析失敗，改用純文字重送")
                status = self._send_chunk(_plain_text(chunk), None)
            if status != "ok":
                return False
        return True

    def _send_chunk(self, chunk: str, parse_mode: str | None) -> str:
        """回傳 'ok' / 'parse' / 'error'。"""
        payload = {
            "chat_id": self.chat_id,
            "text": chunk,
            "disable_web_page_preview": False,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        for attempt in range(3):
            try:
                response = self._post("sendMessage", payload)
            except requests.RequestException as exc:
                log.warning("Telegram 連線失敗（第 %d 次）：%s", attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
                continue

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
                time.sleep(2 * (attempt + 1))
                continue

            log.error("Telegram 發送失敗 HTTP %d：%s", response.status_code, description)
            return "error"

        log.error("Telegram 重試多次仍失敗")
        return "error"
