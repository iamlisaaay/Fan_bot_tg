"""YouTube through the public RSS feed: no key, no account, nothing to get banned.

Regular videos are posted as a preview with a link. Shorts are downloaded when
possible and fall back to the preview, because YouTube often refuses downloads
from datacenter IPs such as GitHub Actions.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from ..models import Media, Post, headline

log = logging.getLogger(__name__)
FEED = "https://www.youtube.com/feeds/videos.xml"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "m": "http://search.yahoo.com/mrss/",
}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}


def resolve_channel_id(handle: str, meta: dict) -> str:
    if re.fullmatch(r"UC[\w-]{22}", handle):
        return handle
    cache_key = f"youtube_id:{handle}"
    if cache_key in meta:
        return meta[cache_key]
    response = requests.get(f"https://www.youtube.com/{handle}", headers=HEADERS, timeout=30)
    response.raise_for_status()
    match = re.search(r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', response.text)
    if not match:
        raise ValueError(f"Could not find the channel ID for {handle}; put the UC... ID into config.yaml")
    meta[cache_key] = match.group(1)
    return meta[cache_key]


def shorts_fetcher(url: str):
    def fetch(folder: Path) -> Optional[Path]:
        import yt_dlp  # imported lazily: it's slow to load and rarely needed

        options = {
            "format": "bv*[ext=mp4][height<=1920]+ba[ext=m4a]/b[ext=mp4]/b",
            "merge_output_format": "mp4",
            "max_filesize": 49 * 1024 * 1024,
            "outtmpl": str(folder / "short.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
        return next(iter(sorted(folder.glob("short.*"))), None)

    return fetch


def parse_feed(xml: str, source_key: str, account: dict, download_shorts: bool) -> list[Post]:
    posts = []
    for entry in ET.fromstring(xml).findall("a:entry", NS):
        video_id = entry.findtext("yt:videoId", namespaces=NS)
        url = entry.find("a:link", NS).attrib["href"]
        is_short = "/shorts/" in url
        thumb = entry.find("m:group/m:thumbnail", NS).attrib["url"]
        media = Media(kind="photo", url=f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg", thumbnail=thumb)
        if is_short and download_shorts:
            media = Media(kind="video", fetcher=shorts_fetcher(url), thumbnail=thumb)
        posts.append(
            Post(
                id=video_id,
                source_key=source_key,
                author=account["name"],
                label=headline(account, "Новий Shorts" if is_short else "Нове відео", "YouTube"),
                url=url,
                text=entry.findtext("a:title", default="", namespaces=NS),
                media=[media],
                timestamp=datetime.fromisoformat(entry.findtext("a:published", namespaces=NS)).timestamp(),
            )
        )
    return posts


class YouTubeSource:
    name = "youtube"

    def __init__(self, cfg: dict):
        self.download_shorts = cfg.get("youtube", {}).get("download_shorts", True)

    def feeds(self, account: dict) -> list[str]:
        return [f"youtube:{account['youtube']}"] if account.get("youtube") else []

    def fetch(self, key: str, account: dict, seen: list[str], meta: dict) -> list[Post]:
        channel_id = resolve_channel_id(account["youtube"], meta)
        response = requests.get(FEED, params={"channel_id": channel_id}, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return parse_feed(response.text, key, account, self.download_shorts)
