"""新聞來源：Marketaux、Finnhub 與 Al Jazeera RSS，含能源關鍵字過濾與跨源去重。"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from dateutil import parser as dateutil_parser

from config import Config

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) clusdt-bot/1.0"}

# HTML meta 標籤中的發布時間（依序嘗試）
META_TIME_PATTERNS = (
    re.compile(
        r"<meta[^>]+(?:property|name|itemprop)=[\"'](?:article:published_time|pubdate|publish-date|datePublished)[\"'][^>]*content=[\"']([^\"']+)[\"']",
        re.I,
    ),
    re.compile(
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name|itemprop)=[\"'](?:article:published_time|pubdate|publish-date|datePublished)[\"']",
        re.I,
    ),
    re.compile(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", re.I),
)


def parse_iso_utc(value: str) -> datetime | None:
    """嚴格 UTC 正規化（python-dateutil）。

    僅接受帶明確時區的 ISO-8601／RFC 格式；像 "15:11" 這種無時區的裸時間
    字串一律拒收（避免 CEST/EDT 等區域時區換算造成的 ±1 小時誤差）。
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = dateutil_parser.parse(raw)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        return None  # 無時區資訊 → 不可驗證
    return parsed.astimezone(timezone.utc)


def extract_meta_published(html_text: str, limit: int = 200_000) -> datetime | None:
    """從文章 HTML 的 meta 標籤抽取發布時間（article:published_time / pubdate 等）。"""
    if not html_text:
        return None
    for pattern in META_TIME_PATTERNS:
        match = pattern.search(html_text[:limit])
        if match:
            parsed = parse_iso_utc(match.group(1))
            if parsed:
                return parsed
    return None

MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
FINNHUB_URL = "https://finnhub.io/api/v1/news"

# Marketaux 免費方案：每次請求最多取回 3 篇（免費額度 100 次/日）
MARKETAUX_PAGE_SIZE = 3
# 查詢詞可由 .env 的 MARKETAUX_QUERIES 覆寫（逗號分隔）；每組詞 = 1 次請求
# 預設 2 組詞 × 每小時 1 輪 = 48 次/日，保留免費額度餘裕
DEFAULT_MARKETAUX_QUERIES = ("crude oil", "OPEC")
# 只處理近 N 小時內的新聞，避免推播過期情報
LOOKBACK_HOURS = 12
FINNHUB_MAX_ITEMS = 10

# 能源相關關鍵字（\b 詞邊界比對，避免 "spread" 誤中 "spr" 之類的假訊號）
ENERGY_KEYWORDS = [
    "crude", "crude oil", "oil price", "oil prices", "brent", "wti", "opec",
    "barrel", "barrels", "refinery", "refineries", "refining", "distillate",
    "gasoline", "diesel", "jet fuel", "lng", "liquefied natural gas",
    "natural gas", "pipeline", "hormuz", "bab el-mandeb", "red sea", "houthi",
    "tanker", "vlcc", "supertanker", "strategic petroleum reserve", "spr",
    "eia", "inventories", "petroleum", "shale", "drilling", "rig count",
    "oilfield", "oil output", "oil production", "oil supply", "oil demand",
    "production cut", "production cuts", "output cut", "output cuts",
    "production quota", "output quota", "energy sector", "upstream",
    "downstream", "crack spread", "oil embargo", "sanctions", "aramco",
    "adnoc", "rosneft", "petrobras", "exxon", "chevron", "conocophillips",
    "halliburton", "schlumberger", "transocean", "frontline",
]

# 明顯非能源市場的假訊號（食用油、精油、塗料等）
EXCLUDE_KEYWORDS = [
    "palm oil", "olive oil", "coconut oil", "sunflower oil", "sesame oil",
    "essential oil", "hair oil", "baby oil", "cooking oil", "fish oil",
    "castor oil", "oil painting", "oil paintings", "motor oil",
]

_ENERGY_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in ENERGY_KEYWORDS) + r")\b", re.I)
_EXCLUDE_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in EXCLUDE_KEYWORDS) + r")\b", re.I)


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    url: str
    published_utc: str = ""
    sort_key: str = ""
    provider: str = ""


def is_energy_news(title: str, summary: str) -> bool:
    blob = f"{title} {summary}"
    return bool(_ENERGY_RE.search(blob)) and not _EXCLUDE_RE.search(blob)


# --------------------------------------------------------------------- #
# 共用工具
# --------------------------------------------------------------------- #
def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:90]


def _same_story(a: str, b: str) -> bool:
    if a[:50] and a[:50] == b[:50]:
        return True
    if len(a) >= 30 and len(b) >= 30:
        return a in b or b in a
    return False


def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
    seen_urls: set[str] = set()
    signatures: list[str] = []
    result: list[NewsItem] = []
    for item in items:
        if item.url in seen_urls:
            continue
        signature = _normalize_title(item.title)
        if signature and any(_same_story(signature, other) for other in signatures):
            log.debug("略過重複新聞：%s", item.title[:60])
            continue
        seen_urls.add(item.url)
        if signature:
            signatures.append(signature)
        result.append(item)
    return result


def _request_with_retry(
    url: str,
    params: dict,
    cfg: Config,
    attempts: int = 2,
) -> requests.Response | None:
    """GET 請求；連線或逾時失敗時重試，最終仍失敗則回傳 None。"""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return requests.get(
                url,
                params=params,
                timeout=(8, 45),
                proxies=cfg.data_proxies,
                headers=DEFAULT_HEADERS,
            )
        except requests.RequestException as exc:
            last_error = exc
            log.warning("請求失敗（第 %d/%d 次）：%s — %s", attempt + 1, attempts, url, exc)
            time.sleep(2 * (attempt + 1))
    log.error("請求最終失敗：%s（%s）", url, last_error)
    return None


def fetch_url_html(url: str, cfg: Config) -> str | None:
    """下載文章 HTML（供內文擷取）；逾時／失敗回傳 None（靜默丟棄）。"""
    response = _request_with_retry(url, {}, cfg)
    if response is None:
        return None
    if response.status_code != 200:
        log.warning("文章下載 HTTP %s：%s", response.status_code, url[:90])
        return None
    return response.text


# --------------------------------------------------------------------- #
# 來源一：Marketaux
# --------------------------------------------------------------------- #
def fetch_marketaux(cfg: Config) -> list[NewsItem]:
    if not cfg.marketaux_api_key:
        return []
    published_after = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for query in cfg.marketaux_queries or DEFAULT_MARKETAUX_QUERIES:
        params = {
            "api_token": cfg.marketaux_api_key,
            "language": "en",
            "limit": MARKETAUX_PAGE_SIZE,
            "filter_entities": "false",
            "sort": "published_at",
            "published_after": published_after,
            "search": query,
        }
        response = _request_with_retry(MARKETAUX_URL, params, cfg)
        if response is None:
            continue
        if response.status_code != 200:
            log.warning("Marketaux「%s」HTTP %s：%s", query, response.status_code, response.text[:150])
            continue

        for article in response.json().get("data") or []:
            title = (article.get("title") or "").strip()
            summary = (article.get("description") or article.get("snippet") or "").strip()
            url = (article.get("url") or "").strip()
            if not title or not url or url in seen_urls or not is_energy_news(title, summary):
                continue
            seen_urls.add(url)
            published = _parse_iso(article.get("published_at", ""))
            items.append(
                NewsItem(
                    title=title,
                    summary=summary,
                    source=(article.get("source") or "Marketaux").strip(),
                    url=url,
                    published_utc=_format_dt(published) or str(article.get("published_at") or ""),
                    sort_key=published.isoformat() if published else "",
                    provider="marketaux",
                )
            )
        time.sleep(0.5)  # 多查詢之間保持溫和間隔
    return items


# --------------------------------------------------------------------- #
# 來源二：Finnhub（一般財經新聞，以關鍵字過濾出能源情報）
# --------------------------------------------------------------------- #
def fetch_finnhub(cfg: Config) -> list[NewsItem]:
    if not cfg.finnhub_api_key:
        return []
    params = {"category": "general", "token": cfg.finnhub_api_key}
    response = _request_with_retry(FINNHUB_URL, params, cfg)
    if response is None:
        return []
    if response.status_code != 200:
        log.warning("Finnhub HTTP %s：%s", response.status_code, response.text[:180])
        return []
    payload = response.json()
    if not isinstance(payload, list):
        log.warning("Finnhub 回應格式異常：%s", str(payload)[:180])
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items: list[NewsItem] = []
    for article in payload:
        title = (article.get("headline") or "").strip()
        summary = (article.get("summary") or "").strip()
        url = (article.get("url") or "").strip()
        if not title or not url or not is_energy_news(title, summary):
            continue
        timestamp = article.get("datetime")
        published = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if isinstance(timestamp, (int, float))
            else None
        )
        if published is not None and published < cutoff:
            continue
        items.append(
            NewsItem(
                title=title,
                summary=summary,
                source=(article.get("source") or "Finnhub").strip(),
                url=url,
                published_utc=_format_dt(published),
                sort_key=published.isoformat() if published else "",
                provider="finnhub",
            )
        )
    items.sort(key=lambda item: item.sort_key, reverse=True)
    return items[:FINNHUB_MAX_ITEMS]


# --------------------------------------------------------------------- #
# 來源三：Al Jazeera English（RSS，聚焦中東地緣與能源供給風險）
# --------------------------------------------------------------------- #
ALJAZEERA_RSS_URLS = (
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.aljazeera.com/xml/rss/all.xml?category=middle-east",
)
ALJAZEERA_MAX_ITEMS = 10
ALJAZEERA_KEYWORDS = (
    "oil", "crude", "brent", "wti", "opec", "petroleum", "tanker", "hormuz",
    "pipeline", "refinery", "fuel", "saudi", "aramco", "iraq", "yemen",
    "houthi", "red sea",
)
_AJ_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in ALJAZEERA_KEYWORDS) + r")\b", re.I)


def is_aljazeera_relevant(text: str) -> bool:
    """半島電視台專用過濾：命中能源或關鍵中東地緣詞彙，且非食用油類假訊號。"""
    return bool(_AJ_RE.search(text)) and not _EXCLUDE_RE.search(text)


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def _entry_datetime(entry) -> datetime | None:
    """feedparser entry → timezone-aware UTC datetime。"""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return _parse_iso(entry.get("published", "") or "")


def fetch_aljazeera(cfg: Config) -> list[NewsItem]:
    """抓取半島電視台英語版 RSS 並過濾出能源／關鍵中東動態。"""
    items: list[NewsItem] = []
    seen_urls: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    for rss_url in ALJAZEERA_RSS_URLS:
        response = _request_with_retry(rss_url, {}, cfg)
        if response is None:
            continue
        if response.status_code != 200:
            log.warning("Al Jazeera RSS HTTP %s：%s", response.status_code, rss_url)
            continue

        feed = feedparser.parse(response.content)
        for entry in feed.entries[:ALJAZEERA_MAX_ITEMS]:
            title = (entry.get("title") or "").strip()
            summary = _strip_html(entry.get("summary", ""))
            link = (entry.get("link") or "").strip()
            if not title or not link or link in seen_urls:
                continue
            if not is_aljazeera_relevant(f"{title} {summary}"):
                continue
            published = _entry_datetime(entry)
            if published is not None and published < cutoff:
                continue
            seen_urls.add(link)
            items.append(
                NewsItem(
                    title=title,
                    summary=summary,
                    source="Al Jazeera English",
                    url=link,
                    published_utc=_format_dt(published),
                    sort_key=published.isoformat() if published else "",
                    provider="aljazeera",
                )
            )
        time.sleep(0.5)
    return items


# --------------------------------------------------------------------- #
def collect_news(cfg: Config) -> list[NewsItem]:
    """彙整所有來源、過濾能源相關、去重並依時間新→舊排序。"""
    items: list[NewsItem] = []
    for name, fetcher in (
        ("Marketaux", fetch_marketaux),
        ("Finnhub", fetch_finnhub),
        ("Al Jazeera", fetch_aljazeera),
    ):
        try:
            found = fetcher(cfg)
        except Exception as exc:  # noqa: BLE001 - 單一來源失敗不影響整體
            log.warning("%s 抓取失敗：%s", name, exc)
            continue
        log.info("%s：取得 %d 則能源相關新聞", name, len(found))
        items.extend(found)

    deduped = _dedupe(items)
    deduped.sort(key=lambda item: item.sort_key or "", reverse=True)
    return deduped
