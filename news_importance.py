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

ASSET_LABELS = {
    "CL": "原油",
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "XRP": "XRP",
    "LINK": "LINK",
    "ENA": "ENA",
    "BNB": "BNB",
    "DOGE": "DOGE",
    "ADA": "ADA",
    "AVAX": "AVAX",
    "SUI": "SUI",
    "HYPE": "HYPE",
    "NEAR": "NEAR",
    "LTC": "LTC",
    "BCH": "BCH",
    "XAU": "黃金",
    "XAG": "白銀",
    "XPT": "鉑金",
    "XPD": "鈀金",
    "COPPER": "銅",
    "BZ": "Brent",
    "NATGAS": "天然氣",
    "COIN": "COIN",
    "HOOD": "HOOD",
    "SPCX": "SPCX",
    "CRCL": "CRCL",
    "MU": "MU",
    "SNDK": "SNDK",
    "SKHYNIX": "SK Hynix",
    "GOOGL": "GOOGL",
    "TSLA": "TSLA",
    "NVDA": "NVDA",
    "META": "META",
    "AAPL": "AAPL",
    "MSFT": "MSFT",
    "AMZN": "AMZN",
}
ASSET_ALIASES = {
    "OIL": "CL",
    "WTI": "CL",
    "CRUDE": "CL",
    "GOLD": "XAU",
    "SILVER": "XAG",
    "PLATINUM": "XPT",
    "PALLADIUM": "XPD",
    "BRENT": "BZ",
}

SYSTEM_PROMPT_TEMPLATE = (
    "You are a strict market news triage filter for an automated alert bot watching these assets: "
    "{ASSETS}. "
    "Score how likely the article is to materially move ANY of these assets within 24 hours (0-10). "
    "Consider: OPEC+ decisions, supply disruptions, sanctions, war or infrastructure attacks, "
    "inventories (EIA/API), ETF/regulatory news, exchange/security incidents, earnings/guidance, "
    "demand data, and major macro catalysts. "
    "Treat as LOW importance: price recaps, technical-analysis pieces, opinion, minor token news, "
    "unrelated markets, and stale content without a new catalyst. "
    'Return ONLY JSON: {"score": <integer 0-10>, '
    '"category": "<supply|demand|geopolitics|inventories|macro|other>", '
    '"reason": "<max 15 words>", "symbols": ["CL", "XAU"]}'
    " Where symbols lists only affected assets from the watched list (empty array if none)."
)


def allowed_assets(cfg) -> tuple[str, ...]:
    """由設定推導可標記資產（Binance 標的 base + 股票清單 + 商品固定項）。"""
    assets: set[str] = {"CL", "XAU", "XAG", "XPT", "XPD", "COPPER", "BZ", "NATGAS"}
    for symbol in getattr(cfg, "binance_symbols", ()):
        base = str(symbol).upper().replace("USDT", "").replace("USD1", "")
        if base:
            assets.add(ASSET_ALIASES.get(base, base))
    for symbol in getattr(cfg, "equity_symbols", ()):
        assets.add(str(symbol).upper())
    return tuple(sorted(assets))


def system_prompt(cfg) -> str:
    return SYSTEM_PROMPT_TEMPLATE.replace("{ASSETS}", ", ".join(allowed_assets(cfg)))



@dataclass(slots=True, frozen=True)
class NewsVerdict:
    score: int
    category: str
    reason: str
    symbols: tuple[str, ...] = ()


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get((category or "").strip().lower(), "其他")


def normalise_asset(code: str) -> str:
    code = (code or "").strip().upper()
    return ASSET_ALIASES.get(code, code)


def assets_label(symbols) -> str:
    return "、".join(ASSET_LABELS.get(code, code) for code in symbols)


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
        data = None
        for max_tokens in (800, 1600):
            response = requests.post(
                f"{cfg.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.deepseek_model,
                    "messages": [
                        {"role": "system", "content": system_prompt(cfg)},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=(6, 40),
            )
            if response.status_code != 200:
                log.warning("DeepSeek 評分失敗 HTTP %d：%s", response.status_code, response.text[:160])
                return None
            message = response.json()["choices"][0]["message"]
            # reasoning 模型可能把 token 全用在 reasoning_content；
            # content 為空時退回解析 reasoning_content，仍失敗則加大預算重試一次
            content = (message.get("content") or "").strip() or (message.get("reasoning_content") or "").strip()
            data = _extract_json(content)
            if data and "score" in data:
                break
            log.warning(
                "DeepSeek 評分回應無法解析（max_tokens=%d，長度 %d），%s",
                max_tokens,
                len(content),
                "重試" if max_tokens == 800 else "放棄",
            )
            data = None
        if not data:
            return None
        score = max(0, min(10, int(data.get("score", 0))))
        raw_symbols = data.get("symbols") or []
        if isinstance(raw_symbols, str):
            raw_symbols = [raw_symbols]
        symbols = tuple(
            dict.fromkeys(
                normalise_asset(str(item))
                for item in raw_symbols
                if normalise_asset(str(item)) in ASSET_LABELS
            )
        )[:6]
        return NewsVerdict(
            score=score,
            category=str(data.get("category", "other")),
            reason=str(data.get("reason", ""))[:120],
            symbols=symbols,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open
        log.warning("DeepSeek 評分呼叫失敗：%s", exc)
        return None
