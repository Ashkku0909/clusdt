"""繁体 → 简体统一转换（含台湾用语 → 大陆用语）。

为什么集中在一处：
    卡片文字散落在 6 个模组里，逐个字串改简体既容易漏改，也会让后续维护
    两面不讨好（改一处忘一处）。因此在 Telegram 发送边界统一转换一次：
    任何新增卡片都自动变成简体，无需在每个 format_* 里记得改。

字形用 OpenCC 的 t2s（台湾正体 → 简体），
词汇则另外映射，因为 t2s 只换字形不换用词（訊號 → 讯号，但大陆说「信号」）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:  # 缺少套件时降级为「原样输出」，绝不让推播中断
    from opencc import OpenCC

    _CC = OpenCC("t2s")
except Exception:  # noqa: BLE001
    _CC = None
    log.warning("opencc 未安裝：卡片文字不會轉為簡體（pip install opencc-python-reimplemented）")

# t2s 之後仍不符合大陆习惯的用词（左列为 t2s 输出，右列为大陆用法）。
# 只收录本专案卡片实际会用到的词，避免误伤。
_TERM_MAP = (
    ("讯号", "信号"),
    ("资讯", "信息"),
    ("资料", "数据"),
    ("品质", "质量"),
    ("预设", "默认"),
    ("快取", "缓存"),
    ("伺服器", "服务器"),
    ("磁碟", "磁盘"),
    ("记忆体", "内存"),
    ("最佳化", "优化"),
    ("演算法", "算法"),
    ("机率", "概率"),
    ("档案", "文件"),
    ("萤幕", "屏幕"),
    ("支援", "支持"),
    ("营收", "营收"),
)

# 只对纯文本做转换；这些标记不可被改动
_NO_CONVERT_TAGS = ("http://", "https://")


def to_simplified(text: str) -> str:
    """把整段卡片文字转成简体；已是简体时原样返回（幂等）。"""
    if not text or _CC is None:
        return text
    try:
        converted = _CC.convert(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("簡繁轉換失敗，原文輸出：%s", exc)
        return text
    for source, target in _TERM_MAP:
        if source != target and source in converted:
            converted = converted.replace(source, target)
    return converted


def to_simplified_lines(text: str) -> str:
    """逐行转换（保留原始换行与空白结构）。"""
    if not text or _CC is None:
        return text
    return "\n".join(to_simplified(line) for line in text.split("\n"))
