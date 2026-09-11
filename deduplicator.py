"""主題冷卻與語意去重（Topic Cooldown）。

避免同主題新聞在短期內連續推播（例如一小時內三則「中東推升 Brent 破 108」）。
以規則關鍵詞把標題聚類為 topic cluster，維護每個 cluster 的冷卻狀態：

- 首次出現：放行（全新事件）
- 冷卻期內第 2 次出現：放行，但標記為「進度更新」（Delta Follow-up）
- 冷卻期內第 3 次（含）以上：壓制（不再推播）

狀態透過 StateStore 持久化（state.json 的 topic_clusters 區段），
確保 `--once` 排程模式跨程序仍能維持冷卻語意。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from state import StateStore

log = logging.getLogger(__name__)

STATE_KEY = "topic_clusters"
PRUNE_AFTER_SECONDS = 48 * 3600  # 超過 48 小時未出現的 cluster 直接清除


class TopicCooldownManager:
    def __init__(self, cooldown_seconds: int = 3600, store: StateStore | None = None):
        self.cooldown_seconds = cooldown_seconds
        self.store = store
        # 格式: {cluster_key: {"last_seen": ts, "count": n, "last_title": str}}
        self.active_clusters: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self.store:
            return
        now = time.time()
        for key, value in self.store.get_section(STATE_KEY).items():
            if not isinstance(value, dict):
                continue
            last_seen = float(value.get("last_seen", 0) or 0)
            if now - last_seen >= PRUNE_AFTER_SECONDS:
                continue
            self.active_clusters[key] = {
                "last_seen": last_seen,
                "count": int(value.get("count", 1) or 1),
                "last_title": str(value.get("last_title", "")),
            }

    def _save(self) -> None:
        if not self.store:
            return
        now = time.time()
        self.active_clusters = {
            key: value
            for key, value in self.active_clusters.items()
            if now - float(value.get("last_seen", 0) or 0) < PRUNE_AFTER_SECONDS
        }
        self.store.set_section(STATE_KEY, self.active_clusters)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_cluster_key(title: str) -> str:
        """提取核心主題標籤（規則式關鍵詞聚合）。"""
        t = title.lower()
        if "hormuz" in t or "strait" in t:
            return "geopolitics_hormuz"
        if any(k in t for k in ("houthi", "red sea", "aden", "tanker")):
            return "geopolitics_red_sea_tanker"
        if any(k in t for k in ("brent", "108", "109", "rally", "surges")):
            return "price_surge_brent"
        if "opec" in t:
            return "opec_policy"
        if any(k in t for k in ("inventory", "eia", "api")):
            return "inventories"
        # 未命中預設群集：以去除標點後的前 4 個單詞 hash 做模糊主題
        words = "".join(ch for ch in t if ch.isalnum() or ch.isspace()).split()[:4]
        return "topic_" + hashlib.md5(" ".join(words).encode()).hexdigest()[:8]

    def check_and_update(self, title: str, persist: bool = True) -> tuple[bool, bool]:
        """回傳 (should_send, is_update)。

        - should_send: 是否准許推播
        - is_update: 是否為已有事件的後續增量更新
        """
        now = time.time()
        cluster_key = self._extract_cluster_key(title)

        if cluster_key in self.active_clusters:
            cluster = self.active_clusters[cluster_key]
            time_diff = now - cluster["last_seen"]

            if time_diff < self.cooldown_seconds:
                cluster["count"] += 1
                cluster["last_seen"] = now
                cluster["last_title"] = title
                if persist:
                    self._save()

                if cluster["count"] > 2:
                    # 短期內第 3 次以上：壓制（避免洗版）
                    log.info("cluster=%s 冷卻期內第 %d 次，壓制", cluster_key, cluster["count"])
                    return False, True

                # 第 2 次出現：標記為進度增量更新並放行
                log.info("cluster=%s 冷卻期內第 2 次，標記為進度更新", cluster_key)
                return True, True

        # 全新主題，或已超過冷卻期
        self.active_clusters[cluster_key] = {
            "last_seen": now,
            "count": 1,
            "last_title": title,
        }
        if persist:
            self._save()
        return True, False
