"""Remembers what was already published, in one JSON file per platform.

This replaces a database. One file per platform means the home PC (Instagram)
and GitHub Actions (YouTube, X) never touch the same file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SEEN_LIMIT = 300


class State:
    # Called with the file path after every save; the server uses it to copy the
    # file to GitHub, because Render's free disk is wiped on every restart.
    on_save = None

    def __init__(self, path: Path):
        self.path = path
        self.data = {"seen": {}, "meta": {}, "blocked_until": 0.0}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def is_initialized(self, key: str) -> bool:
        return key in self.data["seen"]

    def seen(self, key: str) -> list[str]:
        return self.data["seen"].get(key, [])

    def mark_seen(self, key: str, ids: list[str]) -> None:
        current = self.data["seen"].setdefault(key, [])
        current.extend(i for i in ids if i not in current)
        del current[:-SEEN_LIMIT]

    @property
    def meta(self) -> dict:
        return self.data["meta"]

    @property
    def blocked_until(self) -> float:
        return self.data["blocked_until"]

    @blocked_until.setter
    def blocked_until(self, value: float) -> None:
        self.data["blocked_until"] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic, so a crash can't leave a broken file
        if State.on_save:
            State.on_save(self.path)
