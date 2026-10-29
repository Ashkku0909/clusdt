"""股票基本面與新聞分析（Finnhub + DeepSeek Flash），結果快取於 data/equities/。

流程：
1. Finnhub：profile2（公司）、quote（價格）、metric=all（估值/成長）、company-news（近 7 天）
2. 估值適用性：PE/PEG 不合理（如 TSLA PE 500、PEG 28）時，改用 PS、EV/FCF 與營收成長
3. DeepSeek Flash：**只整理已抓到的數字與標題**（禁止捏造），輸出結構化 JSON
4. 快取：data/equities/{symbol}.json（TTL 由 EQUITY_CACHE_TTL_SEC 控制，預設 6 小時）
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"

METRIC_KEYS = (
    "peTTM",
    "peAnnual",
    "pegTTM",
    "psTTM",
    "pbAnnual",
    "currentEv/freeCashFlowTTM",
    "revenuePerShareTTM",
    "revenueGrowthTTMYoy",
    "revenueGrowthQuarterlyYoy",
    "epsGrowthTTMYoy",
    "grossMarginTTM",
    "beta",
    "52WeekHigh",
    "52WeekLow",
)

SYSTEM_PROMPT = (
    "You are an equity research assistant for a Chinese-language alert channel. "
    "Use ONLY the numbers and headlines provided by the user. Never invent figures or events. "
    "The user will mark whether PE/PEG are appropriate; if they are not, value the company with "
    "PS, EV/FCF and growth instead. Be concise and factual. "
    "Return ONLY JSON with keys: "
    '{"summary_zh": "<=80字公司與現況", '
    '"valuation_zh": "<=80字估值判斷（引用提供的倍數）", '
    '"catalysts": ["<=20字", "..."], "risks": ["<=20字", "..."], '
    '"rating": "bullish|neutral|bearish", "confidence": "low|medium|high"}'
)


# --------------------------------------------------------------------- #
def _get(session: requests.Session, path: str, params: dict, token: str):
    response = session.get(f"{FINNHUB_BASE}{path}", params={**params, "token": token}, timeout=(6, 20))
    if response.status_code != 200:
        raise RuntimeError(f"Finnhub HTTP {response.status_code}（{path}）：{response.text[:120]}")
    return response.json()


def _num(value) -> float | None:
    try:
        number = float(value)
        return number if number == number else None  # 過濾 NaN
    except (TypeError, ValueError):
        return None


def fetch_equity(session: requests.Session, cfg, symbol: str) -> dict:
    """抓取單一股票的快照（公司、價格、估值、近期新聞標題）。"""
    token = cfg.finnhub_api_key
    if not token:
        raise RuntimeError("未設定 FINNHUB_API_KEY")
    symbol = symbol.upper()

    profile = _get(session, "/stock/profile2", {"symbol": symbol}, token)
    quote = _get(session, "/quote", {"symbol": symbol}, token)
    metric_payload = _get(session, "/stock/metric", {"symbol": symbol, "metric": "all"}, token)
    metrics = metric_payload.get("metric", {}) or {}

    today = datetime.now(timezone.utc).date()
    news = _get(
        session,
        "/company-news",
        {"symbol": symbol, "from": (today - timedelta(days=7)).isoformat(), "to": today.isoformat()},
        token,
    )
    headlines = [
        {
            "headline": str(item.get("headline", ""))[:140],
            "source": item.get("source", ""),
            "datetime": item.get("datetime", 0),
        }
        for item in (news or [])[:8]
        if item.get("headline")
    ]

    price = _num(quote.get("c"))
    prev_close = _num(quote.get("pc"))
    change_pct = ((price - prev_close) / prev_close * 100.0) if price and prev_close else None

    return {
        "symbol": symbol,
        "name": profile.get("name") or symbol,
        "industry": profile.get("finnhubIndustry") or "",
        "market_cap_musd": _num(profile.get("marketCapitalization")),
        "price": price,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "high": _num(quote.get("h")),
        "low": _num(quote.get("l")),
        "metrics": {key: _num(metrics.get(key)) for key in METRIC_KEYS},
        "headlines": headlines,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def valuation_assessment(metrics: dict) -> tuple[bool, bool, list[str]]:
    """回傳 (PE 是否適用, PEG 是否適用, 提示文字)。"""
    pe = metrics.get("peTTM")
    peg = metrics.get("pegTTM")
    notes: list[str] = []
    pe_ok = bool(pe and 0 < pe < 150)
    peg_ok = bool(peg and 0 < peg < 5)
    if pe is not None and not pe_ok:
        notes.append(f"PE {'為負' if pe <= 0 else f'{pe:.0f} 倍偏高'}，不適用")
    if peg is not None and not peg_ok:
        notes.append(f"PEG {peg:.0f} 失真，不適用")
    if not pe_ok or not peg_ok:
        notes.append("改以 PS／EV-FCF／營收成長評估")
    return pe_ok, peg_ok, notes


# --------------------------------------------------------------------- #
def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None


def analyse_equity(cfg, snapshot: dict) -> dict | None:
    """DeepSeek Flash 產生結構化分析（只使用提供的數字）；失敗回傳 None。"""
    if not cfg.deepseek_api_key:
        return None
    metrics = snapshot.get("metrics", {})
    pe_ok, peg_ok, notes = valuation_assessment(metrics)
    payload = {
        "symbol": snapshot["symbol"],
        "name": snapshot["name"],
        "industry": snapshot["industry"],
        "price": snapshot["price"],
        "market_cap_musd": snapshot["market_cap_musd"],
        "metrics": {k: v for k, v in metrics.items() if v is not None},
        "pe_applicable": pe_ok,
        "peg_applicable": peg_ok,
        "valuation_notes": notes,
        "headlines": [item["headline"] for item in snapshot.get("headlines", [])],
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    try:
        data = None
        for max_tokens in (900, 1800):
            response = requests.post(
                f"{cfg.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.deepseek_api_key}", "Content-Type": "application/json"},
                json={
                    "model": cfg.deepseek_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=(6, 60),
            )
            if response.status_code != 200:
                log.warning("DeepSeek 股票分析失敗 HTTP %d：%s", response.status_code, response.text[:160])
                return None
            message = response.json()["choices"][0]["message"]
            content = (message.get("content") or "").strip() or (message.get("reasoning_content") or "").strip()
            data = _extract_json(content)
            if data:
                break
            log.warning("DeepSeek 股票分析解析失敗（max_tokens=%d），重試", max_tokens)
        return data
    except Exception as exc:  # noqa: BLE001 - 分析失敗不影響數據
        log.warning("DeepSeek 股票分析呼叫失敗：%s", exc)
        return None


# --------------------------------------------------------------------- #
# 快取
# --------------------------------------------------------------------- #
def _cache_path(cfg, symbol: str) -> Path:
    return Path(cfg.data_dir) / "equities" / f"{symbol.upper()}.json"


def load_cached(cfg, symbol: str) -> dict | None:
    path = _cache_path(cfg, symbol)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("cached_at", 0))
        if age <= cfg.equity_cache_ttl_sec:
            return data
    except Exception as exc:  # noqa: BLE001
        log.warning("股票快取讀取失敗 %s：%s", symbol, exc)
    return None


def save_cache(cfg, symbol: str, snapshot: dict, analysis: dict | None) -> None:
    path = _cache_path(cfg, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cached_at": time.time(), "snapshot": snapshot, "analysis": analysis}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------- #
# 輸出
# --------------------------------------------------------------------- #
def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1e12:
        return f"${value / 1e12:.2f}T"
    if abs(value) >= 1e9:
        return f"${value / 1e9:.2f}B"
    if abs(value) >= 1e6:
        return f"${value / 1e6:.2f}M"
    return f"${value:,.2f}"


def format_equity_report(snapshot: dict, analysis: dict | None) -> str:
    metrics = snapshot.get("metrics", {})
    pe_ok, peg_ok, notes = valuation_assessment(metrics)
    price = snapshot.get("price") or 0.0
    change_pct = snapshot.get("change_pct") or 0.0
    lines = [
        f"📊 【{snapshot['symbol']}｜{snapshot['name']}】",
        f"產業：{snapshot.get('industry') or '—'}｜市值：{_fmt_money((snapshot.get('market_cap_musd') or 0) * 1e6)}",
        f"價格：${price:,.2f}（日變 {change_pct:+.2f}%）",
        f"估值：PE {metrics.get('peTTM') or '—'}｜PEG {metrics.get('pegTTM') or '—'}"
        f"｜PS {metrics.get('psTTM') or '—'}｜EV/FCF {metrics.get('currentEv/freeCashFlowTTM') or '—'}",
        f"成長：營收 YoY {metrics.get('revenueGrowthTTMYoy') or '—'}%"
        f"｜EPS YoY {metrics.get('epsGrowthTTMYoy') or '—'}%｜毛利 {metrics.get('grossMarginTTM') or '—'}%",
    ]
    if notes:
        lines.append("估值提示：" + "；".join(notes))
    if analysis:
        lines.append(f"摘要：{analysis.get('summary_zh', '').strip()}")
        lines.append(f"估值判斷：{analysis.get('valuation_zh', '').strip()}")
        catalysts = "／".join(str(x) for x in (analysis.get("catalysts") or [])[:3])
        risks = "／".join(str(x) for x in (analysis.get("risks") or [])[:3])
        if catalysts:
            lines.append(f"催化：{catalysts}")
        if risks:
            lines.append(f"風險：{risks}")
        lines.append(
            f"評級：{analysis.get('rating', '—')}（信心 {analysis.get('confidence', '—')}）"
        )
    else:
        lines.append("分析：DeepSeek 暫不可用（僅顯示原始數據）")
    return "\n".join(lines)


def run_equity_cli(cfg, symbols: list[str], dispatcher=None, dry_run: bool = False) -> int:
    """``--equity``：抓取／快取並輸出股票分析；dispatcher 有值時同時推播。"""
    session = requests.Session()
    reports: list[str] = []
    for symbol in symbols:
        cached = load_cached(cfg, symbol)
        if cached is not None:
            snapshot, analysis = cached["snapshot"], cached.get("analysis")
            log.info("%s 使用快取（%s）", symbol, snapshot.get("fetched_at", "")[:19])
        else:
            snapshot = fetch_equity(session, cfg, symbol)
            analysis = analyse_equity(cfg, snapshot)
            save_cache(cfg, symbol, snapshot, analysis)
            log.info("%s 已更新並寫入快取", symbol)
        report = format_equity_report(snapshot, analysis)
        reports.append(report)
        print("\n" + "─" * 64)
        print(report)
        print("─" * 64)
    if dispatcher is not None:
        for report in reports:
            dispatcher.send_alert(report, dry_run=dry_run)
    return 0
