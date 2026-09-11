"""本機狀態：已推播去重清單、各監測項目進度與主題冷卻狀態（data/state.json）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

MAX_SENT_URLS = 3000


class StateStore:
    def __init__(self, path: Path, max_sent: int = MAX_SENT_URLS):
        self.path = Path(path)
        self.max_sent = max_sent
        self._data: dict = {"sent_urls": [], "flags": {}, "topic_clusters": {}}
        self._sent: set[str] = set()
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("sent_urls"), list):
                    self._data["sent_urls"] = data["sent_urls"]
                if isinstance(data.get("flags"), dict):
                    self._data["flags"] = data["flags"]
                if isinstance(data.get("topic_clusters"), dict):
                    self._data["topic_clusters"] = data["topic_clusters"]
        except Exception as exc:  # noqa: BLE001 - 壞檔就重建
            log.warning("state.json 讀取失敗，將以空狀態重建：%s", exc)
        self._sent = {url for url in self._data["sent_urls"] if isinstance(url, str)}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ #
    def is_sent(self, url: str) -> bool:
        return url in self._sent

    def mark_sent(self, url: str) -> None:
        if not url or url in self._sent:
            return
        self._sent.add(url)
        sent = self._data["sent_urls"]
        sent.append(url)
        if len(sent) > self.max_sent:
            self._data["sent_urls"] = sent[-self.max_sent :]
        self._save()

    def get_flags(self) -> dict[str, str]:
        return dict(self._data.get("flags", {}))

    def update_flags(self, flags: dict[str, str]) -> None:
        self._data.setdefault("flags", {}).update(flags)
        self._save()

    def get_section(self, key: str) -> dict:
        """讀取自訂區段（如 topic_clusters）；不存在時回傳空 dict。"""
        value = self._data.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def set_section(self, key: str, value: dict) -> None:
        """覆寫自訂區段並立即存檔。"""
        self._data[key] = dict(value)
        self._save()
