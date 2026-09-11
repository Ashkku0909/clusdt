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
from state import StateStore

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Asia/Hong_Kong")  # 顯示用本地時區（UTC+8）

SEEN_SECTION = "news_seen"
SEEN_MAX_AGE_HOURS = 48
MARKETAUX_THROTTLE_FLAG = "news:marketaux:last_fetch"
PARAGRAPH_MIN_CHARS = 60
TRANSLATE_MAX_CHARS = 1500  # 單次翻譯長度上限（免費端點保護）

# deep-translator 偶發會把 Google 錯誤頁當成翻譯結果回傳，需攔截
GARBAGE_MARKERS = (
    "error 500", "server error", "that’s an error", "that's an error",
    "please try again later", "<!doctype", "<html", "404 not found", "502 bad gateway",
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
    """備援：MyMemory 免費 API（單次 500 字元上限，長文自動分段）。"""
    parts: list[str] = []
    for chunk in _split_for_mymemory(payload):
        try:
            result = MyMemoryTranslator(source="en-US", target=cfg.translate_target).translate(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("MyMemory 翻譯失敗：%s", exc)
            break
        result = (result or "").strip()
        if not result or _looks_like_garbage(result):
            break
        parts.append(result)
    return "".join(parts) if parts else None


def translate(text: str, cfg: Config) -> str | None:
    """直譯（Google → MyMemory 引擎鏈）；全數失敗時回傳 None。"""
    text = (text or "").strip()
    if not text:
        return None
    payload = text[:TRANSLATE_MAX_CHARS]
    for _ in range(2):
        result = _translate_google(payload, cfg)
        if result:
            return result
        result = _translate_mymemory(payload, cfg)
        if result:
            return result
        time.sleep(1.0)
    return None


# --------------------------------------------------------------------- #
# 格式化與推播
# --------------------------------------------------------------------- #
def format_news_alert(
    item: NewsItem,
    published: datetime,
    age_minutes: float,
    title_zh: str,
    paragraphs_zh: list[str],
) -> str:
    local_time = published.astimezone(LOCAL_TZ).strftime("%H:%M")
    stamp = f"{int(age_minutes)} 分鐘前 ({local_time} 本地時間)"
    body = "\n\n".join(paragraphs_zh) if paragraphs_zh else "[無法擷取內文]"
    return "\n".join(
        [
            "⚡ **【原油即時快訊】**",
            "━━━━━━━━━━━━━━━━━━",
            f"📰 **標題**: {title_zh}",
            f"🌐 **來源**: {item.source} ｜ 🕒 **發布**: {stamp}",
            "",
            "📝 **內文快讀 (中譯)**:",
            body,
            "",
            f"🔗 **原文網址**: {item.url}",
            "━━━━━━━━━━━━━━━━━━",
        ]
    )


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
    sent = 0
    for entry in fresh:
        if sent >= limit:
            break
        item = entry.item
        keys = _seen_keys(item)
        if any(key in seen for key in keys):
            continue

        log.info("處理新聞：%s（%.1f 分鐘前）", item.title[:70], entry.age_minutes)
        title_zh = translate(item.title, cfg)
        if not title_zh:
            log.warning("標題翻譯失敗，丟棄：%s", item.url)
            continue

        paragraphs = scrape_article(item.url, cfg, cfg.article_paragraph_limit, preloaded_html=entry.html)
        if not paragraphs and item.summary.strip():
            log.info("內文擷取失敗，改用來源摘要（feed summary）")
            paragraphs = [item.summary.strip()]
        paragraphs_zh: list[str] = []
        for paragraph in paragraphs:
            translated = translate(paragraph, cfg)
            if translated:
                paragraphs_zh.append(translated)

        alert = format_news_alert(item, entry.published, entry.age_minutes, title_zh, paragraphs_zh)
        if dispatcher.send_alert(alert, dry_run=dry_run):
            sent += 1
            if not dry_run:
                stamp = datetime.now(timezone.utc).isoformat()
                for key in keys:
                    seen[key] = stamp
        time.sleep(1.0)

    if not dry_run and seen:
        state.set_section(SEEN_SECTION, _prune_seen(seen))
    return sent
