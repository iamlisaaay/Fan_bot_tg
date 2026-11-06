"""Shared data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Media:
    kind: str  # "photo" or "video"
    url: Optional[str] = None
    # Lower-quality variants to try when `url` is too large for Telegram.
    alternatives: list[str] = field(default_factory=list)
    # Picture to send instead when no video variant can be delivered.
    thumbnail: Optional[str] = None
    # Produces a local file (used for yt-dlp); returns None when it can't.
    fetcher: Optional[Callable[[Path], Optional[Path]]] = None


@dataclass
class Post:
    id: str
    source_key: str  # e.g. "instagram:posts:band_official"
    author: str  # display name from config
    label: str  # the headline, e.g. "Новий пост від Сомюля в X"
    url: str
    text: str = ""
    media: list[Media] = field(default_factory=list)
    timestamp: float = 0.0  # used to publish in chronological order


def headline(account: dict, noun: str, platform: str) -> str:
    """Accounts with `from` are people; the others are official pages."""
    if account.get("from"):
        return f"{noun} від {account['from']} в {platform}"
    return f"{noun} на акаунті {account['name']} в {platform}"


class SourceBlocked(Exception):
    """The platform refused access; stop using it for the cooldown period."""


class FatalConfigError(Exception):
    """The config or environment is wrong; a human has to fix it."""
