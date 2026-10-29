"""即時新聞管線（100% 確定性、零 LLM）。

流程：
    抓取（Finnhub / Al Jazeera RSS；Marketaux 另以節流輪詢）
    → 嚴格時間窗過濾（僅保留 `NEWS_MAX_AGE_MINUTES` 內、且時間戳可驗證為 UTC 者）
    → URL／標題雜湊去重（持久化於 state.json）
    → trafilatura 內文擷取（前 2-3 段實質段落）
    → deep-translator 免費直譯（繁體中文）
    → Telegram 推播（固定格式）

無生成式模型、無市場偏見判斷、無供需論述、無投資建議。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from deep_translator import GoogleTranslator, MyMemoryTranslator
import trafilatura

from config import Config
from news_sources import (
    NewsItem,
    extract_meta_published,
    fetch_aljazeera,
    fetch_finnhub,
    fetch_marketaux,
    fetch_url_html,
    parse_iso_utc,
)
from news_importance import assets_label, category_label, score_news
from llm_translate import translate_lines
from state import StateStore

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Asia/Hong_Kong")  # 顯示用本地時區（UTC+8）

SEEN_SECTION = "news_seen"
FILTERED_SECTION = "news_filtered"
TRANSLATIONS_SECTION = "news_translations"
TRANSLATIONS_MAX = 800
SEEN_MAX_AGE_HOURS = 48
MARKETAUX_THROTTLE_FLAG = "news:marketaux:last_fetch"
PARAGRAPH_MIN_CHARS = 60
TRANSLATE_MAX_CHARS = 1500  # 單次翻譯長度上限（免費端點保護）

# deep-translator 偶發會把 Google 錯誤頁當成翻譯結果回傳，需攔截
GARBAGE_MARKERS = (
    "error 500", "server error", "that’s an error", "that's an error",
    "please try again later", "<!doctype", "<html", "404 not found", "502 bad gateway",
    "mymemory warning", "you used all available free translations", "too many requests",
)


def _looks_like_garbage(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in GARBAGE_MARKERS)


# 股票指標類雜訊（GuruFocus 等頁面的 GF Score／P/S 內容與能源情報無關）
STOCK_NOISE_MARKERS = (
    "gf score", "gf value", "p/s ratio", "p/e ratio", "price-to-sales",
    "price-to-earnings", "market cap", "dividend yield", "price target",
    "overvalued", "undervalued", "shares outstanding", "eps of", "每股盈餘",
    "市銷率", "本益比", "市盈率", "殖利率", "市值",
)


def _is_stock_noise(paragraph: str) -> bool:
    lowered = paragraph.lower()
    return any(marker in lowered for marker in STOCK_NOISE_MARKERS)


# --------------------------------------------------------------------- #
# 抓取（含 Marketaux 配額節流）
# --------------------------------------------------------------------- #
def collect_fresh(cfg: Config, state: StateStore, persist: bool = True) -> list[NewsItem]:
    """抓取所有來源；Marketaux 以最小間隔節流（免費 100 次/日）。"""
    items: list[NewsItem] = []
    for name, fetcher in (("Finnhub", fetch_finnhub), ("Al Jazeera", fetch_aljazeera)):
        try:
            items.extend(fetcher(cfg) or [])
        except Exception as exc:  # noqa: BLE001 - 單一來源失敗不影響其他
            log.warning("%s 抓取失敗：%s", name, exc)

    flags = state.get_flags()
    last_fetch = _float_or_zero(flags.get(MARKETAUX_THROTTLE_FLAG))
    elapsed = time.time() - last_fetch
    if elapsed >= cfg.marketaux_min_interval_sec:
        try:
            items.extend(fetch_marketaux(cfg) or [])
            if persist:
                state.update_flags({MARKETAUX_THROTTLE_FLAG: str(time.time())})
        except Exception as exc:  # noqa: BLE001
            log.warning("Marketaux 抓取失敗：%s", exc)
    else:
        log.info("Marketaux 節流中（距上次 %.0f 秒 < %d 秒），本輪略過", elapsed, cfg.marketaux_min_interval_sec)
    return items


def _float_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- #
# 時間戳驗證與時間窗過濾
# --------------------------------------------------------------------- #
@dataclass
class FreshNews:
    item: NewsItem
    published: datetime
    age_minutes: float
    html: str | None = None


def resolve_published(item: NewsItem, cfg: Config) -> tuple[datetime | None, str | None]:
    """解析發布時間：先信賴來源欄位（需帶時區）；無效時爬取文章 meta 標籤。

    回傳 (UTC 時間, 已下載的 HTML 或 None)；無法驗證時時間為 None。
    """
    published = parse_iso_utc(item.sort_key or "")
    if published:
        return published, None
    html_text = fetch_url_html(item.url, cfg)
    if not html_text:
        return None, None
    return extract_meta_published(html_text), html_text


def select_fresh(items: list[NewsItem], cfg: Config) -> list[FreshNews]:
    """僅保留時間戳可驗證且發布時間在窗口內的新聞（新 → 舊）。"""
    now = datetime.now(timezone.utc)
    fresh: list[FreshNews] = []
    for item in items:
        published, html_text = resolve_published(item, cfg)
        if published is None:
            log.info("時間戳無法驗證（來源缺漏且 meta 不可用），丟棄：%s", item.title[:60])
            continue
        age = (now - published).total_seconds() / 60.0
        if age < -5:  # 時鐘偏差保護
            continue
        if age > cfg.news_max_age_minutes:
            continue
        fresh.append(FreshNews(item=item, published=published, age_minutes=age, html=html_text))
    fresh.sort(key=lambda row: row.published, reverse=True)
    return fresh


# --------------------------------------------------------------------- #
# 去重（URL 與標題雜湊，持久化）
# --------------------------------------------------------------------- #
def _digest(value: str) -> str:
    return hashlib.sha1(value.strip().lower().encode("utf-8")).hexdigest()


def _seen_keys(item: NewsItem) -> tuple[str, str]:
    return f"u:{_digest(item.url)}", f"t:{_digest(item.title)}"


def _prune_seen(seen: dict) -> dict:
    """清除超過保留期的紀錄後回傳。"""
    cutoff = datetime.now(timezone.utc).timestamp() - SEEN_MAX_AGE_HOURS * 3600
    kept: dict[str, str] = {}
    for key, stamp in seen.items():
        try:
            if datetime.fromisoformat(str(stamp)).timestamp() >= cutoff:
                kept[key] = stamp
        except ValueError:
            continue
    return kept


# --------------------------------------------------------------------- #
# 內文擷取與翻譯
# --------------------------------------------------------------------- #
def extract_paragraphs(html: str, url: str, limit: int) -> list[str]:
    """以 trafilatura 擷取正文，取前 N 段實質段落。"""
    if not html:
        return []
    try:
        text = trafilatura.extract(
            html,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("正文擷取失敗：%s", exc)
        return []
    if not text or _looks_like_garbage(text[:200]):
        return []
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if len(p.strip()) >= PARAGRAPH_MIN_CHARS]
    paragraphs = [p for p in paragraphs if not _looks_like_garbage(p) and not _is_stock_noise(p)]
    if not paragraphs:
        paragraphs = [text.strip()[:800]]
    return paragraphs[:limit]


GOOGLE_NEWS_HOST = "news.google.com"
_GOOGLE_ASSET_HOSTS = ("googleusercontent", "gstatic", "googleapis", "googlevideo", "google.com", "googleusercontent.com")


def _is_google_host(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in _GOOGLE_ASSET_HOSTS)


def _decode_google_news_url(url: str) -> str | None:
    """解碼 Google News RSS 連結（base64 內容可能内含原始新聞網址）。"""
    marker = "/rss/articles/"
    if marker not in url:
        return None
    token = url.split(marker, 1)[1].split("?", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:  # noqa: BLE001
        return None
    for match in re.findall(rb"https?://[^\x00-\x20\"'<>]+", raw):
        candidate = match.decode("utf-8", errors="ignore")
        if not _is_google_host(candidate):
            return candidate
    return None


def _first_external_url(html: str) -> str | None:
    """從 Google News 轉址頁取出第一個非 Google 的外部連結。"""
    for match in re.findall(r'https?://[^"\'<>\s\\]+', html or ""):
        if not _is_google_host(match):
            return match
    return None


def scrape_article(url: str, cfg: Config, limit: int, preloaded_html: str | None = None) -> list[str]:
    """下載並擷取文章正文；遇 Google News 轉址時自動解析原站。"""
    html_text = preloaded_html or fetch_url_html(url, cfg) or ""
    paragraphs = extract_paragraphs(html_text, url, limit)
    if paragraphs:
        return paragraphs
    if GOOGLE_NEWS_HOST in url:
        resolved = _decode_google_news_url(url) or _first_external_url(html_text)
        if resolved:
            log.info("解析 Google News 轉址：%s", resolved[:90])
            return extract_paragraphs(fetch_url_html(resolved, cfg) or "", resolved, limit)
    return []


GOOGLE_COOLDOWN_SEC = 600
_google_down_until = 0.0


def _translate_google(payload: str, cfg: Config) -> str | None:
    """Google 免費端點（品質優先）；連續失敗時進入冷卻，不再嘗試。"""
    global _google_down_until
    if time.time() < _google_down_until:
        return None
    try:
        result = GoogleTranslator(source="auto", target=cfg.translate_target).translate(payload)
        result = (result or "").strip()
        if result and not _looks_like_garbage(result):
            return result
        _google_down_until = time.time() + GOOGLE_COOLDOWN_SEC
        log.warning("Google 翻譯回應無效，冷卻 %d 秒", GOOGLE_COOLDOWN_SEC)
    except Exception as exc:  # noqa: BLE001
        _google_down_until = time.time() + GOOGLE_COOLDOWN_SEC
        log.warning("Google 翻譯失敗，冷卻 %d 秒：%s", GOOGLE_COOLDOWN_SEC, exc)
    return None


MYMEMORY_CHUNK_LIMIT = 480  # MyMemory 單次上限 500 字元
MYMEMORY_COOLDOWN_SEC = 3600  # 配額耗盡後停止嘗試，避免每次推播都撞牆
_mymemory_down_until = 0.0


def _split_for_mymemory(text: str) -> list[str]:
    """依句子切分並貪婪合併至單次上限內。"""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > MYMEMORY_CHUNK_LIMIT:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:MYMEMORY_CHUNK_LIMIT])
            sentence = sentence[MYMEMORY_CHUNK_LIMIT:]
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > MYMEMORY_CHUNK_LIMIT:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or ([text[:MYMEMORY_CHUNK_LIMIT]] if text else [])


def _translate_mymemory(payload: str, cfg: Config) -> str | None:
    """最後手段：MyMemory 免費 API（匿名 5,000 字/日；填 MYMEMORY_EMAIL 提升至 50,000）。"""
    global _mymemory_down_until
    if time.time() < _mymemory_down_until:
        return None
    parts: list[str] = []
    email = (getattr(cfg, "mymemory_email", "") or "").strip() or None
    for chunk in _split_for_mymemory(payload):
        try:
            translator = MyMemoryTranslator(
                source="en-US", target=cfg.translate_target, email=email
            )
            result = translator.translate(chunk)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "too many requests" in message.lower() or "quota" in message.lower():
                _mymemory_down_until = time.time() + MYMEMORY_COOLDOWN_SEC
                log.warning(
                    "MyMemory 免費額度耗盡（匿名 5,000 字/日、填 MYMEMORY_EMAIL 可到 50,000），"
                    "冷卻 %d 秒：%s",
                    MYMEMORY_COOLDOWN_SEC,
                    message[:120],
                )
            else:
                log.warning("MyMemory 翻譯失敗：%s", message[:120])
            break
        result = (result or "").strip()
        if not result or _looks_like_garbage(result):
            break
        parts.append(result)
    return "".join(parts) if parts else None


def translate(text: str, cfg: Config) -> str | None:
    """單句直譯（供 Binance 公告等單行呼叫）。"""
    return translate_batch([text], cfg)[0]


def _tr_key(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]


_translation_cache: dict[str, str] = {}


def load_translation_cache(state: StateStore) -> None:
    """載入歷史譯文，避免同一句話重複打第三方端點。"""
    _translation_cache.clear()
    section = state.get_section(TRANSLATIONS_SECTION)
    if isinstance(section, dict):
        _translation_cache.update({str(k): str(v) for k, v in section.items() if v})


def save_translation_cache(state: StateStore, dry_run: bool = False) -> None:
    if dry_run or not _translation_cache:
        return
    # 只保留最近 TRANSLATIONS_MAX 筆（dict 依插入順序）
    items = list(_translation_cache.items())[-TRANSLATIONS_MAX:]
    state.set_section(TRANSLATIONS_SECTION, dict(items))


def _remember(payload: str, zh: str) -> str:
    _translation_cache[_tr_key(payload)] = zh
    return zh


def translate_batch(texts: list[str], cfg: Config) -> list[str | None]:
    """批量直譯：快取 → Google 免費 → DeepSeek → MyMemory。

    - 快取命中直接回傳，不再產外部請求
    - DeepSeek 整批一次呼叫（比逐句打免費端點穩定得多，且可讀性更好）
    - MyMemory 僅作最後手段；它以 IP 計每日額度，共用雲端 IP 很容易耗盡
    """
    results: list[str | None] = [None] * len(texts)
    pending: list[tuple[int, str]] = []
    for index, raw in enumerate(texts):
        text = (raw or "").strip()
        if not text:
            results[index] = ""
            continue
        payload = text[:TRANSLATE_MAX_CHARS]
        cached = _translation_cache.get(_tr_key(payload))
        if cached and not _looks_like_garbage(cached):
            results[index] = cached
        else:
            pending.append((index, payload))

    # 1) Google 免費端點（品質優先；失敗會自動冷卻 600 秒）
    still: list[tuple[int, str]] = []
    for index, payload in pending:
        result = _translate_google(payload, cfg)
        if result:
            results[index] = _remember(payload, result)
        else:
            still.append((index, payload))

    # 2) DeepSeek（一次翻完整批，最可靠的備援）
    if still:
        translated = translate_lines([payload for _, payload in still], cfg)
        if translated:
            remaining: list[tuple[int, str]] = []
            for (index, payload), zh in zip(still, translated):
                zh = (zh or "").strip()
                if zh and not _looks_like_garbage(zh):
                    results[index] = _remember(payload, zh)
                else:
                    remaining.append((index, payload))
            still = remaining
            if still:
                log.warning("DeepSeek 翻譯有 %d 句未完成，改用 MyMemory", len(still))
        else:
            log.warning("DeepSeek 翻譯不可用（%d 句），改用 MyMemory", len(still))

    # 3) MyMemory（最後手段）
    for index, payload in still:
        result = _translate_mymemory(payload, cfg)
        if result:
            results[index] = _remember(payload, result)
    return results


# --------------------------------------------------------------------- #
# 格式化與推播
# --------------------------------------------------------------------- #
def format_news_alert(
    item: NewsItem,
    published: datetime,
    age_minutes: float,
    title_zh: str,
    paragraphs_zh: list[str],
    verdict=None,
) -> str:
    local_time = published.astimezone(LOCAL_TZ).strftime("%H:%M")
    stamp = f"{int(age_minutes)} 分鐘前 ({local_time} 本地時間)"
    body = "\n\n".join(paragraphs_zh) if paragraphs_zh else "[無法擷取內文]"
    lines = [
        "⚡ **【原油即時快訊】**",
        "━━━━━━━━━━━━━━━━━━",
        f"📰 **標題**: {title_zh}",
        f"🌐 **來源**: {item.source} ｜ 🕒 **發布**: {stamp}",
    ]
    if verdict is not None:
        line = f"🎯 **重要性**: {verdict.score}/10（{category_label(verdict.category)}）"
        if getattr(verdict, "symbols", ()):
            line += f" ｜ 影響: {assets_label(verdict.symbols)}"
        lines.append(line)
    lines += [
        "",
        "📝 **內文快讀 (中譯)**:",
        body,
        "",
        f"🔗 **原文網址**: {item.url}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def run_news_cycle(
    cfg: Config,
    dispatcher,
    state: StateStore,
    dry_run: bool = False,
    limit: int | None = None,
) -> int:
    """一輪即時新聞掃描；回傳成功推播數。"""
    limit = limit or cfg.max_alerts_per_run
    items = collect_fresh(cfg, state, persist=not dry_run)
    fresh = select_fresh(items, cfg)
    log.info(
        "即時新聞：抓到 %d 則，時間戳可驗證且在 %d 分鐘窗內 %d 則",
        len(items),
        cfg.news_max_age_minutes,
        len(fresh),
    )
    if not fresh:
        return 0

    seen = state.get_section(SEEN_SECTION)
    filtered = state.get_section(FILTERED_SECTION)
    load_translation_cache(state)
    sent = 0
    for entry in fresh:
        if sent >= limit:
            break
        item = entry.item
        keys = _seen_keys(item)
        if any(key in seen for key in keys) or any(key in filtered for key in keys):
            continue

        log.info("處理新聞：%s（%.1f 分鐘前）", item.title[:70], entry.age_minutes)
        paragraphs = scrape_article(item.url, cfg, cfg.article_paragraph_limit, preloaded_html=entry.html)
        if not paragraphs and item.summary.strip():
            log.info("內文擷取失敗，改用來源摘要（feed summary）")
            paragraphs = [item.summary.strip()]

        verdict = None
        if cfg.news_importance_filter and cfg.deepseek_api_key:
            verdict = score_news(cfg, item.title, " ".join(paragraphs))
            if verdict is None:
                log.info("重要性評分不可用（fail-open，照常推播）")
            elif verdict.score < cfg.news_min_importance:
                log.info(
                    "重要性 %d/10 < %d（%s｜%s），過濾：%s",
                    verdict.score,
                    cfg.news_min_importance,
                    category_label(verdict.category),
                    verdict.reason,
                    item.title[:60],
                )
                if not dry_run:
                    stamp = datetime.now(timezone.utc).isoformat()
                    for key in keys:
                        filtered[key] = stamp
                continue
            else:
                log.info(
                    "重要性 %d/10（%s）：%s",
                    verdict.score,
                    category_label(verdict.category),
                    verdict.reason,
                )

        translated = translate_batch([item.title, *paragraphs], cfg)
        title_zh = translated[0]
        if not title_zh:
            log.warning("標題翻譯失敗（三層引擎皆不可用），丟棄：%s", item.url)
            continue
        paragraphs_zh = [zh for zh in translated[1:] if zh]

        alert = format_news_alert(
            item,
            entry.published,
            entry.age_minutes,
            title_zh,
            paragraphs_zh,
            verdict=verdict,
        )
        if dispatcher.send_alert(alert, dry_run=dry_run):
            sent += 1
            if not dry_run:
                stamp = datetime.now(timezone.utc).isoformat()
                for key in keys:
                    seen[key] = stamp
        time.sleep(1.0)

    if not dry_run:
        if seen:
            state.set_section(SEEN_SECTION, _prune_seen(seen))
        if filtered:
            state.set_section(FILTERED_SECTION, _prune_seen(filtered))
        save_translation_cache(state)
    return sent
