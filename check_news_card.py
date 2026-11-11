"""渲染新聞卡範例，確認精簡格式符合訂閱者要求（不發送、不抓網路）。"""

from datetime import datetime, timedelta, timezone

from news_pipeline import format_news_alert
from news_sources import NewsItem, source_label
from zh_text import to_simplified_lines

NOW = datetime.now(timezone.utc)

CASES = [
    # 使用者貼的那一則：標題與內文重複，舊版會顯示兩次
    NewsItem(
        title="Attacks escalate between Saudi-led forces and Yemen's Iran-backed Houthis",
        summary="",
        source="Al Jazeera English",
        url="https://www.aljazeera.com/video/newsfeed/2026/11/10/attacks-escalate",
    ),
    NewsItem(
        title="OPEC+ agrees to extend output cuts through Q1",
        summary="",
        source="Reuters",
        url="https://www.reuters.com/business/energy/opec-agrees-2026-11-11/",
    ),
    NewsItem(
        title="Saudi Aramco reports lower third-quarter profit",
        summary="",
        source="Bloomberg",
        url="https://www.bloomberg.com/news/articles/2026-11-11/aramco",
    ),
    NewsItem(
        title="Yahoo Finance roundup",
        summary="",
        source="Yahoo Finance",
        url="https://finance.yahoo.com/news/roundup",
    ),
]

TITLE_ZH = {
    "Attacks escalate between Saudi-led forces and Yemen's Iran-backed Houthis":
        "沙特阿拉伯领导的部队与也门获伊朗支持的胡塞武装之间攻击升级",
    "OPEC+ agrees to extend output cuts through Q1": "OPEC+ 同意将减产延长至第一季度",
    "Saudi Aramco reports lower third-quarter profit": "沙特阿美第三季利润下滑",
    "Yahoo Finance roundup": "雅虎财经综述",
}

BODY_ZH = {
    "Attacks escalate between Saudi-led forces and Yemen's Iran-backed Houthis": [
        "沙特阿拉伯领导的部队与也门获伊朗支持的胡塞武装之间攻击升级",
        "沙特阿拉伯表示，随着双方攻击升级，已拦截也门获伊朗支持的胡塞武装所发射的导弹。萨那的一栋石油公司建筑于周一凌晨遭袭。",
    ],
    "OPEC+ agrees to extend output cuts through Q1": [
        "三名代表表示，该组织将把现有减产延长至明年第一季度末，以支撑油价。",
    ],
    "Saudi Aramco reports lower third-quarter profit": [
        "受油价下跌影响，净利润同比下降 12%，但仍维持全年派息指引不变。",
    ],
}

for item in CASES:
    label = source_label(item.source, item.url)
    if label is None:
        print(f"\n【來源不在白名單，會被直接丟棄】{item.source}")
        continue
    item.source = label
    published = NOW - timedelta(minutes=16)
    card = format_news_alert(
        item,
        published,
        16.0,
        TITLE_ZH[item.title],
        BODY_ZH.get(item.title, []),
    )
    print("\n" + "═" * 60)
    print("原始（轉換前）：")
    print(card)
    print("─" * 60)
    print("實際送出（簡體）：")
    print(to_simplified_lines(card))
