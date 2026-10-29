"""Binance 官方公告監控（上幣／下架／空投／維護）＋ 精簡 VIP 卡片。

資料源：`https://www.binance.com/bapi/composite/v1/public/cms/article/list/query`
只保留與 BINANCE_SYMBOLS 相關或全市場期貨維護的公告；標題翻譯沿用新聞管線，
其餘（活動／API 更新／無關幣種）一律不推播。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from news_pipeline import LOCAL_TZ, translate

log = logging.getLogger(__name__)

ANNOUNCE_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# catalogId → (中文分類, 影響級別)
CATALOGS = {
    48: ("上幣", "high"),
    161: ("下架", "high"),
    128: ("空投", "medium"),
    157: ("維護", "medium"),
    49: ("公告", "medium"),
}
WATCH_TOKENS = ("BTC", "ETH", "SOL", "XRP", "LINK", "ENA", "XAU", "XAG", "PAXG", "CL")
FIXED_ASSETS = ("CL", "XAU", "XAG", "PAXG", "XPT", "XPD", "COPPER", "BZ", "NATGAS")
SEEN_SECTION = "binance_news_seen"
MAX_SEEN = 500


@dataclass(slots=True, frozen=True)
class Announcement:
    code: str
    title: str
    catalog_id: int
    catalog_name: str
    release_ms: int

    @property
    def url(self) -> str:
        return f"https://www.binance.com/en/support/announcement/{self.code}"

    @property
    def category(self) -> str:
        return CATALOGS.get(self.catalog_id, ("公告", "medium"))[0]

    @property
    def impact(self) -> str:
        return CATALOGS.get(self.catalog_id, ("公告", "medium"))[1]


# --------------------------------------------------------------------- #
def fetch_announcements(session: requests.Session, page_size: int = 20) -> list[Announcement]:
    page_size = max(1, min(20, page_size))  # Binance 上限 20
    response = session.get(
        ANNOUNCE_URL,
        params={"type": 1, "pageNo": 1, "pageSize": page_size},
        headers=HEADERS,
        timeout=(6, 20),
    )
    if response.status_code != 200:
        raise RuntimeError(f"Binance 公告 HTTP {response.status_code}")
    payload = response.json()
    catalogs = (payload.get("data") or {}).get("catalogs") or []
    items: list[Announcement] = []
    seen: set[str] = set()
    for catalog in catalogs:
        catalog_id = int(catalog.get("catalogId") or 0)
        if catalog_id not in CATALOGS:
            continue
        for row in catalog.get("articles") or []:
            code = str(row.get("code") or "")
            title = str(row.get("title") or "").strip()
            if not code or not title or code in seen:
                continue
            seen.add(code)
            items.append(
                Announcement(
                    code=code,
                    title=title,
                    catalog_id=catalog_id,
                    catalog_name=str(catalog.get("catalogName") or ""),
                    release_ms=int(row.get("releaseDate") or 0),
                )
            )
    items.sort(key=lambda item: item.release_ms, reverse=True)
    return items


def watch_tokens(cfg) -> tuple[str, ...]:
    """由 BINANCE_SYMBOLS / EQUITY_SYMBOLS 動態產生觀察名單（含商品固定清單）。"""
    tokens = set(FIXED_ASSETS)
    for symbol in getattr(cfg, "binance_symbols", ()):
        base = str(symbol).upper().replace("USDT", "").replace("USD1", "")
        if base:
            tokens.add(base)
    for symbol in getattr(cfg, "equity_symbols", ()):
        tokens.add(str(symbol).upper())
    return tuple(sorted(tokens))


def matched_assets(title: str, tokens: tuple[str, ...]) -> list[str]:
    # 只匹配大寫代碼（避免 "coin" 這類普通單字誤判）
    words = set(re.findall(r"\b[A-Z0-9]{2,12}\b", title))
    return [asset for asset in tokens if asset in words]


def is_relevant(announcement: Announcement, tokens: tuple[str, ...]) -> tuple[bool, list[str]]:
    assets = matched_assets(announcement.title, tokens)
    if assets:
        return True, assets
    # TradFi 類（XAU/XAG/CL 等 TradFi perps；費率/槓桿/新合約變動都相關）
    if re.search(r"tradfi", announcement.title, flags=re.IGNORECASE):
        return True, ["XAU/XAG/CL"]
    # 全市場期貨維護/升級（影響所有合約）
    if announcement.catalog_id == 157 and re.search(
        r"futures|usdt-m|perpetual|derivatives", announcement.title, flags=re.IGNORECASE
    ):
        return True, ["全市場"]
    return False, []


# --------------------------------------------------------------------- #
def _age_text(release_ms: int) -> str:
    if not release_ms:
        return ""
    published = datetime.fromtimestamp(release_ms / 1000.0, tz=LOCAL_TZ)
    minutes = max(0, int((datetime.now(timezone.utc) - datetime.fromtimestamp(release_ms / 1000.0, tz=timezone.utc)).total_seconds() // 60))
    if minutes < 60:
        age = f"{minutes} 分鐘前"
    elif minutes < 1440:
        age = f"{minutes // 60} 小時前"
    else:
        age = f"{minutes // 1440} 天前"
    return f"{age} ({published.strftime('%H:%M')} 本地)"


def format_announcement_alert(announcement: Announcement, title_zh: str, assets: list[str]) -> str:
    lines = [
        f"🏛️ Binance 公告 · {announcement.category}",
        f"🎯 {'、'.join(assets)}｜{_age_text(announcement.release_ms)}",
        f"📰 {title_zh}",
        f"🔗 {announcement.url}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------- #
def run_binance_news_cycle(cfg, dispatcher, state, dry_run: bool = False) -> int:
    """扫描一次 Binance 公告；回傳成功推播数。"""
    if not getattr(cfg, "binance_news_enabled", True):
        return 0
    try:
        session = requests.Session()
        announcements = fetch_announcements(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("Binance 公告取得失敗：%s", exc)
        return 0

    seen = state.get_section(SEEN_SECTION)
    tokens = watch_tokens(cfg)
    cutoff_ms = int(time.time() * 1000) - int(cfg.binance_news_max_age_hours) * 3600 * 1000
    sent = 0
    for announcement in announcements:
        if sent >= cfg.binance_news_limit_per_cycle:
            break
        if announcement.release_ms < cutoff_ms:
            continue
        if announcement.code in seen:
            continue
        relevant, assets = is_relevant(announcement, tokens)
        if not relevant:
            continue

        title_zh = translate(announcement.title, cfg) or announcement.title
        alert = format_announcement_alert(announcement, title_zh, assets)
        log.info("Binance 公告：%s（%s）", announcement.title[:70], announcement.category)
        if dispatcher.send_alert(alert, dry_run=dry_run):
            sent += 1
            if not dry_run:
                seen[announcement.code] = datetime.now(timezone.utc).isoformat()
        time.sleep(1.0)

    if not dry_run and seen:
        # 只保留最近 MAX_SEEN 筆
        if len(seen) > MAX_SEEN:
            ordered = sorted(seen.items(), key=lambda item: item[1])[-MAX_SEEN:]
            seen = dict(ordered)
        state.set_section(SEEN_SECTION, seen)
    return sent
