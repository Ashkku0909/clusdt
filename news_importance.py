"""新聞重要性評分（DeepSeek Flash，0-10），用於過濾無關／低價值新聞。

設計原則：
- 只做「分數判定」：不產生任何敘事、預測或投資建議，輸出為嚴格 JSON
- 未設定 `DEEPSEEK_API_KEY`、或呼叫／解析失敗 → 回傳 None
  （呼叫端 fail-open：照常推播，不中斷新聞流）
- 分數 < `NEWS_MIN_IMPORTANCE`（預設 6）才視為不重要並過濾
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "supply": "供給",
    "demand": "需求",
    "geopolitics": "地緣政治",
    "inventories": "庫存",
    "macro": "宏觀",
    "other": "其他",
}

SYSTEM_PROMPT = (
    "You are a strict crude-oil news triage filter for an automated WTI/Brent alert bot. "
    "Score how likely the article is to materially move crude oil prices within 24 hours (0-10). "
    "Consider: OPEC+ decisions, supply disruptions, sanctions, war or infrastructure attacks, "
    "inventories (EIA/API), demand data, and major macro catalysts. "
    "Treat as LOW importance: price recaps, technical-analysis pieces, opinion, single-company/stock news, "
    "unrelated commodities or markets, and stale content without a new catalyst. "
    'Return ONLY JSON: {"score": <integer 0-10>, '
    '"category": "<supply|demand|geopolitics|inventories|macro|other>", '
    '"reason": "<max 15 words>"}'
)


@dataclass(slots=True, frozen=True)
class NewsVerdict:
    score: int
    category: str
    reason: str


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get((category or "").strip().lower(), "其他")


def _extract_json(text: str) -> dict | None:
    """從回應中擷取 JSON（容忍 ```json 圍欄與前後雜訊）。"""
    text = (text or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except ValueError:
            return None


def score_news(cfg, title: str, body: str) -> NewsVerdict | None:
    """呼叫 DeepSeek Flash 對一則新聞評分；任何失敗皆回傳 None（fail-open）。"""
    if not getattr(cfg, "deepseek_api_key", ""):
        return None
    user_prompt = f"Title: {title}\n\nBody: {(body or '')[:1500]}"
    try:
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
                "temperature": 0,
                "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
            timeout=(6, 40),
        )
        if response.status_code != 200:
            log.warning("DeepSeek 評分失敗 HTTP %d：%s", response.status_code, response.text[:160])
            return None
        content = response.json()["choices"][0]["message"].get("content", "")
        data = _extract_json(content)
        if not data or "score" not in data:
            log.warning("DeepSeek 評分回應無法解析：%s", str(content)[:160])
            return None
        score = max(0, min(10, int(data.get("score", 0))))
        return NewsVerdict(
            score=score,
            category=str(data.get("category", "other")),
            reason=str(data.get("reason", ""))[:120],
        )
    except Exception as exc:  # noqa: BLE001 - fail-open
        log.warning("DeepSeek 評分呼叫失敗：%s", exc)
        return None
