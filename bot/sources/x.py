"""X (Twitter) through the official pay-per-use API, so there's no account to get banned.

Billing is per post returned, so we always ask only for posts newer than the last
one we've seen. The very first run reads nothing: it just notes the current time.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone

import requests

from ..models import FatalConfigError, Media, Post, SourceBlocked, headline

log = logging.getLogger(__name__)
API = "https://api.x.com/2"


def clean_text(tweet: dict) -> str:
    text = tweet.get("note_tweet", {}).get("text") or tweet.get("text", "")
    entities = tweet.get("note_tweet", {}).get("entities") or tweet.get("entities", {})
    for link in entities.get("urls", []):
        # Links to the post's own media are noise; other links get their real address.
        replacement = "" if link.get("media_key") else link.get("expanded_url", link["url"])
        text = text.replace(link["url"], replacement)
    # X sends "&", "<" and ">" as HTML codes; we escape them ourselves later.
    return html.unescape(text).strip()


def parse_media(item: dict) -> Media:
    if item["type"] == "photo":
        url = item.get("url")
        # The plain URL serves a reduced size; keep it as the fallback for huge originals.
        return Media(kind="photo", url=f"{url}?name=orig", thumbnail=url)
    variants = [v for v in item.get("variants", []) if v.get("content_type") == "video/mp4"]
    urls = [v["url"] for v in sorted(variants, key=lambda v: v.get("bit_rate", 0), reverse=True)]
    return Media(
        kind="video",
        url=urls[0] if urls else None,
        alternatives=urls[1:],
        thumbnail=item.get("preview_image_url"),
    )


def parse_timeline(payload: dict, key: str, account: dict) -> list[Post]:
    username = account["x"]
    media_by_key = {m["media_key"]: m for m in payload.get("includes", {}).get("media", [])}
    posts = []
    for tweet in payload.get("data", []):
        keys = tweet.get("attachments", {}).get("media_keys", [])
        posts.append(
            Post(
                id=tweet["id"],
                source_key=key,
                author=account["name"],
                label=headline(account, "Новий пост", "X"),
                url=f"https://x.com/{username}/status/{tweet['id']}",
                text=clean_text(tweet),
                media=[parse_media(media_by_key[k]) for k in keys if k in media_by_key],
                timestamp=datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00")).timestamp(),
            )
        )
    return posts


class XSource:
    name = "x"

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("x", {})
        # Pasting a token into GitHub secrets easily brings a stray space or line break along.
        token = os.environ.get("X_BEARER_TOKEN", "").strip()
        if not token:
            raise FatalConfigError("X_BEARER_TOKEN is not set")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.backfill_from = None  # ISO time; set for a one-off "send the last N days" run

    def feeds(self, account: dict) -> list[str]:
        return [f"x:{account['x']}"] if account.get("x") else []

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = requests.get(f"{API}{path}", headers=self.headers, params=params, timeout=30)
        if response.status_code in (401, 402, 403, 429):
            raise SourceBlocked(f"X API {response.status_code}: {response.text[:200]}")
        response.raise_for_status()
        return response.json()

    def fetch(self, key: str, account: dict, seen: list[str], meta: dict) -> list[Post]:
        username = account["x"]
        id_key = f"x_id:{username}"
        if id_key not in meta:
            found = self._get(f"/users/by/username/{username}")
            if "data" not in found:
                raise ValueError(f"X account not found: {username}")
            meta[id_key] = found["data"]["id"]

        params = {
            "max_results": 20,
            "tweet.fields": "created_at,note_tweet,entities,attachments",
            "expansions": "attachments.media_keys",
            "media.fields": "type,url,preview_image_url,variants",
        }
        exclude = []
        if not self.cfg.get("include_retweets", False):
            exclude.append("retweets")
        if not self.cfg.get("include_replies", False):
            exclude.append("replies")
        if exclude:
            params["exclude"] = ",".join(exclude)

        start_key = f"x_start:{username}"
        if seen:
            params["since_id"] = max(seen, key=int)
        elif self.backfill_from:
            meta[start_key] = params["start_time"] = self.backfill_from
        elif start_key in meta:
            params["start_time"] = meta[start_key]
        else:
            # First run for this account: remember "now" and spend nothing.
            meta[start_key] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return []

        payload = self._get(f"/users/{meta[id_key]}/tweets", params)
        return parse_timeline(payload, key, account)
