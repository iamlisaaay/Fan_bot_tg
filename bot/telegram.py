"""Delivers posts through the Telegram Bot API.

Files are always downloaded and uploaded, not passed as URLs: Telegram often can't
fetch Instagram/X CDN links itself, and upload limits are more generous.
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

from .formatting import Rendered
from .models import Media

log = logging.getLogger(__name__)

PHOTO_LIMIT = 10 * 1024 * 1024
VIDEO_LIMIT = 50 * 1024 * 1024
ALBUM_SIZE = 10
HEADERS = {"User-Agent": "Mozilla/5.0"}


class TelegramError(Exception):
    def __init__(self, status: int, description: str):
        super().__init__(f"{status}: {description}")
        self.status = status


def download(url: str, dest: Path, max_bytes: int) -> Optional[Path]:
    """Returns the path, or None when the file exceeds max_bytes."""
    with requests.get(url, stream=True, timeout=60, headers=HEADERS) as response:
        response.raise_for_status()
        if int(response.headers.get("Content-Length") or 0) > max_bytes:
            return None
        size = 0
        with open(dest, "wb") as out:
            for chunk in response.iter_content(1024 * 256):
                size += len(chunk)
                if size > max_bytes:
                    break
                out.write(chunk)
    if size > max_bytes:
        dest.unlink(missing_ok=True)
        return None
    return dest


def materialize(item: Media, folder: Path, index: int) -> Optional[tuple[str, Path]]:
    """Gets one media item onto disk, degrading from video to its preview if needed."""
    if item.fetcher:
        try:
            path = item.fetcher(folder)
            if path and path.stat().st_size <= VIDEO_LIMIT:
                return "video", path
        except Exception as exc:  # yt-dlp raises many kinds; the preview is an OK result
            log.warning("Video download failed, using the preview: %s", exc)
    elif item.kind == "video":
        for n, url in enumerate(filter(None, [item.url, *item.alternatives])):
            try:
                path = download(url, folder / f"{index}_{n}.mp4", VIDEO_LIMIT)
            except requests.RequestException as exc:
                log.warning("Video download failed: %s", exc)
                continue
            if path:
                return "video", path
    elif item.url:
        path = _download_photo(item.url, folder / f"{index}.jpg")
        if path:
            return "photo", path

    if item.thumbnail:
        path = _download_photo(item.thumbnail, folder / f"{index}_thumb.jpg")
        if path:
            return "photo", path
    return None


def _download_photo(url: str, dest: Path) -> Optional[Path]:
    try:
        return download(url, dest, PHOTO_LIMIT)
    except requests.RequestException as exc:
        log.warning("Photo download failed: %s", exc)
        return None


class Telegram:
    def __init__(self, token: str, chat_id: str, admin_chat_id: str = ""):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.admin_chat_id = admin_chat_id

    def _call(self, method: str, data: dict, files: Optional[dict] = None) -> dict:
        for attempt in range(4):
            # Reopen files on each attempt: requests consumes them while sending.
            handles = {k: open(p, "rb") for k, p in (files or {}).items()}
            try:
                response = requests.post(f"{self.base}/{method}", data=data, files=handles, timeout=300)
            except requests.RequestException as exc:
                # The token is part of the URL, so don't let it reach the logs.
                if attempt == 3:
                    raise TelegramError(0, f"network error: {type(exc).__name__}") from None
                time.sleep(5 * (attempt + 1))
                continue
            finally:
                for handle in handles.values():
                    handle.close()
            payload = response.json()
            if payload.get("ok"):
                return payload["result"]
            retry_after = payload.get("parameters", {}).get("retry_after")
            if response.status_code == 429 and retry_after:
                log.info("Telegram asks to wait %ss", retry_after)
                time.sleep(retry_after + 1)
                continue
            raise TelegramError(response.status_code, payload.get("description", "unknown error"))
        raise TelegramError(429, "still rate limited after retries")

    def _send_text(self, text: str, reply_to: Optional[int] = None, chat_id: Optional[str] = None) -> dict:
        data = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
        if reply_to:
            data["reply_parameters"] = json.dumps({"message_id": reply_to})
        return self._call("sendMessage", data)

    def send_by_file_id(self, chat_id: str, items: list[tuple[str, str]], rendered: Rendered) -> list[int]:
        """Sends media already stored on Telegram, so it's not compressed a second time.

        Returns the ids of all sent messages.
        """
        sent: list[dict] = []
        if len(items) == 1:
            kind, file_id = items[0]
            sent.append(self._call(
                f"send{kind.capitalize()}",
                {"chat_id": chat_id, kind: file_id, "caption": rendered.caption, "parse_mode": "HTML"},
            ))
        elif items:
            for start in range(0, len(items), ALBUM_SIZE):
                media = [{"type": kind, "media": file_id} for kind, file_id in items[start : start + ALBUM_SIZE]]
                if start == 0:
                    media[0].update(caption=rendered.caption, parse_mode="HTML")
                sent.extend(self._call("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)}))
        else:
            sent.append(self._send_text(rendered.caption, chat_id=chat_id))
        if rendered.followup:
            sent.append(self._send_text(rendered.followup, reply_to=sent[0]["message_id"], chat_id=chat_id))
        return [message["message_id"] for message in sent]

    def _send_single(self, kind: str, path: Path, caption: str) -> dict:
        method, field = ("sendVideo", "video") if kind == "video" else ("sendPhoto", "photo")
        data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        if kind == "video":
            data["supports_streaming"] = "true"
        return self._call(method, data, {field: path})

    def _send_album(self, items: list[tuple[str, Path]], caption: str) -> dict:
        media, files = [], {}
        for i, (kind, path) in enumerate(items):
            entry = {"type": kind, "media": f"attach://f{i}"}
            if i == 0 and caption:
                entry.update(caption=caption, parse_mode="HTML")
            media.append(entry)
            files[f"f{i}"] = path
        result = self._call("sendMediaGroup", {"chat_id": self.chat_id, "media": json.dumps(media)}, files)
        return result[0]

    def send_post(self, rendered: Rendered, media: list[Media]) -> None:
        """Sends the post. If the media is rejected, still publishes the text with the link."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            files = [f for f in (materialize(m, folder, i) for i, m in enumerate(media)) if f]
            first = None
            try:
                if len(files) == 1:
                    first = self._send_single(files[0][0], files[0][1], rendered.caption)
                elif files:
                    for start in range(0, len(files), ALBUM_SIZE):
                        chunk = files[start : start + ALBUM_SIZE]
                        caption = rendered.caption if start == 0 else ""
                        if len(chunk) == 1:
                            sent = self._send_single(chunk[0][0], chunk[0][1], caption)
                        else:
                            sent = self._send_album(chunk, caption)
                        first = first or sent
            except TelegramError as exc:
                if exc.status != 400 or first:
                    raise
                log.warning("Telegram rejected the media (%s), posting text only", exc)

        if first is None:
            first = self._send_text(rendered.caption)
        if rendered.followup:
            self._send_text(rendered.followup, reply_to=first["message_id"])

    def alert_admin(self, text: str) -> None:
        if not self.admin_chat_id:
            log.warning("No TELEGRAM_ADMIN_CHAT_ID set, alert not delivered: %s", text)
            return
        try:
            self._call("sendMessage", {"chat_id": self.admin_chat_id, "text": text})
        except TelegramError as exc:
            log.warning("Could not alert the admin: %s", exc)
