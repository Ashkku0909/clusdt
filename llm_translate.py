"""DeepSeek 翻譯（繁體中文）——新聞直譯的可靠備援。

為什麼需要這一層：
- deep-translator 的 Google 免費端點在雲端主機 IP 上經常被限流（本專案實測會整段冷卻）
- MyMemory 免費額度以「IP／每日字數」計算，Render 免費層是共用出口 IP，
  很快就被其他租戶一起吃光，回傳 ``You made too many requests``
- 專案已有 ``DEEPSEEK_API_KEY``，用同一個 key 做翻譯最穩定，
  且能一次翻譯整篇（標題＋段落），比逐句打免費端點更省請求

設計原則：
- 只做「翻譯」：不改寫、不評論、不預測，術語與數字原樣保留
- 任何失敗皆回傳 ``None``（呼叫端可再退回 MyMemory 或丟棄該則）
"""

from __future__ import annotations

import json
import logging
import re

import requests

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a professional financial-news translator. Translate every numbered line the user sends "
    "from English into Traditional Chinese (zh-Hant, Taiwan/Hong Kong style). "
    "Rules: keep tickers, company names, numbers, percentages, currencies and units unchanged; "
    "translate faithfully and concisely; never add commentary, analysis, advice or extra lines. "
    'Return ONLY JSON: {"lines": ["<translation of line 1>", "<translation of line 2>", ...]} '
    "with exactly the same number of items, in the same order."
)

MAX_LINE_CHARS = 1500
MAX_TOTAL_CHARS = 6000


def _extract_json(text: str) -> dict | None:
    """從模型輸出取出第一個 JSON 物件（容忍 reasoning 前綴與 ``` 包裹）。"""
    if not text:
        return None
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def translate_lines(lines: list[str], cfg) -> list[str] | None:
    """一次翻譯多行；成功回傳等長的中文列表，失敗回傳 None。"""
    if not getattr(cfg, "deepseek_api_key", ""):
        return None
    texts = [(line or "").strip()[:MAX_LINE_CHARS] for line in lines]
    if not any(texts):
        return None
    budget = 0
    kept: list[str] = []
    for text in texts:
        if budget + len(text) > MAX_TOTAL_CHARS:
            kept.append("")
            continue
        budget += len(text)
        kept.append(text)

    user_prompt = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(kept) if text)
    try:
        for max_tokens in (1200, 2400):
            response = requests.post(
                f"{cfg.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.deepseek_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=(6, 60),
            )
            if response.status_code != 200:
                log.warning("DeepSeek 翻譯失敗 HTTP %d：%s", response.status_code, response.text[:160])
                return None
            message = response.json()["choices"][0]["message"]
            # reasoning 模型可能把 token 全花在 reasoning_content
            content = (message.get("content") or "").strip() or (message.get("reasoning_content") or "").strip()
            data = _extract_json(content)
            items = data.get("lines") if data else None
            if isinstance(items, list) and len(items) == len(kept):
                result = [str(item).strip() if texts[i] else "" for i, item in enumerate(items[: len(kept)])]
                return result + ["" for _ in range(len(texts) - len(kept))]
            log.warning(
                "DeepSeek 翻譯回應格式不符（max_tokens=%d，%s），%s",
                max_tokens,
                f"lines={len(items) if isinstance(items, list) else 'n/a'} 預期={len(kept)}",
                "重試" if max_tokens == 1200 else "放棄",
            )
    except Exception as exc:  # noqa: BLE001 - fail-open，交由上層退回其他引擎
        log.warning("DeepSeek 翻譯呼叫失敗：%s", exc)
    return None
