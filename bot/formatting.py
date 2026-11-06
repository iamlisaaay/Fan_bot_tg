"""Builds the Telegram HTML message for a post, respecting Telegram's length limits.

Layout:
    <b>Новий пост від Сомюля в X</b>

    original text
    -----
    translation

    Дивитись (link to the source)
    @channel
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

from .models import Post
from .translate import Translation

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
PART_OVERHEAD = 8  # separator and line breaks that surround each body part


@dataclass
class Rendered:
    caption: str  # goes with the media (or is the whole message when there's no media)
    followup: str = ""  # separate text message when the text doesn't fit into a caption


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _body_parts(post: Post, tr: Translation) -> list[tuple[str, str]]:
    """(plain text, template) pairs; the template wraps the escaped text."""
    parts = []
    if post.text.strip():
        parts.append((post.text.strip(), "{}"))
    if tr.romanized:
        parts.append((tr.romanized, "<i>{}</i>"))
    if tr.text:
        parts.append((tr.text, "-----\n{}"))
    return parts


def _visible_length(parts: list[tuple[str, str]]) -> int:
    return sum(len(text) + PART_OVERHEAD for text, _ in parts)


def _render_body(parts: list[tuple[str, str]], budget: int) -> str:
    if _visible_length(parts) > budget and parts:
        # Shrink every part in proportion, keeping the separators' allowance.
        room = max(budget - PART_OVERHEAD * len(parts), 0)
        chars = sum(len(text) for text, _ in parts)
        parts = [(_shorten(text, room * len(text) // chars), tpl) for text, tpl in parts]
    return "\n".join(tpl.format(escape(text, quote=False)) for text, tpl in parts)


def render(post: Post, tr: Translation, channel: dict, has_media: bool) -> Rendered:
    handle = channel["chat_id"]
    # Hand-written posts may have neither a headline nor a source link.
    header = f"<b>{escape(post.label, quote=False)}</b>" if post.label else ""
    link = f'<a href="{escape(post.url)}">Дивитись</a>\n' if post.url else ""
    footer = f"{link}{escape(handle, quote=False)}"

    # Visible length of what surrounds the body, plus the blank lines around it.
    frame = len(post.label) + len("Дивитись\n") + len(handle) + 4

    def join(body: str) -> str:
        return "\n\n".join(part for part in (header, body, footer) if part)

    parts = _body_parts(post, tr)
    limit = CAPTION_LIMIT if has_media else MESSAGE_LIMIT
    if _visible_length(parts) <= limit - frame or not has_media:
        return Rendered(join(_render_body(parts, limit - frame)))

    # Too long for a caption: media gets the short version, the text goes after it.
    return Rendered(join(""), _render_body(parts, MESSAGE_LIMIT - 10))
