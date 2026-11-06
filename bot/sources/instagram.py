"""Instagram posts and stories through instaloader, using a logged-in browser session.

This is the risky source. Instagram has no API for reading other people's accounts,
and using an account this way breaks its rules, so a throwaway account can get
restricted at any time. To lower the odds we never log in with a password from code,
make few requests, and stop at the first sign of trouble instead of retrying.
"""
from __future__ import annotations

import itertools
import logging
import os
import random
import time

import instaloader
from instaloader import exceptions as ig
from instaloader.instaloadercontext import RateController

from ..models import FatalConfigError, Media, Post, SourceBlocked, headline

log = logging.getLogger(__name__)


class StopOn429(RateController):
    """By default instaloader sleeps for up to half an hour here; we want to stop."""

    def handle_429(self, query_type: str) -> None:
        raise ig.TooManyRequestsException(f"429 on {query_type}")


def post_to_model(post, key: str, account: dict) -> Post:
    if post.typename == "GraphSidecar":
        media = [
            Media(kind="video", url=n.video_url, thumbnail=n.display_url)
            if n.is_video
            else Media(kind="photo", url=n.display_url)
            for n in post.get_sidecar_nodes()
        ]
    elif post.is_video:
        media = [Media(kind="video", url=post.video_url, thumbnail=post.url)]
    else:
        media = [Media(kind="photo", url=post.url)]
    return Post(
        id=post.shortcode,
        source_key=key,
        author=account["name"],
        label=headline(account, "Новий пост", "Instagram"),
        url=f"https://www.instagram.com/p/{post.shortcode}/",
        text=post.caption or "",
        media=media,
        timestamp=post.date_utc.timestamp(),
    )


def story_to_model(item, key: str, account: dict, username: str) -> Post:
    media = (
        Media(kind="video", url=item.video_url, thumbnail=item.url)
        if item.is_video
        else Media(kind="photo", url=item.url)
    )
    return Post(
        id=str(item.mediaid),
        source_key=key,
        author=account["name"],
        label=headline(account, "Нова сторіс", "Instagram"),
        url=f"https://www.instagram.com/stories/{username}/{item.mediaid}/",
        text=item.caption or "",
        media=[media],
        timestamp=item.date_utc.timestamp(),
    )


class InstagramSource:
    name = "instagram"

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("instagram", {})
        username = os.environ.get("IG_USERNAME", "")
        sessionid = os.environ.get("IG_SESSIONID", "")
        csrftoken = os.environ.get("IG_CSRFTOKEN", "")
        if not (username and sessionid and csrftoken):
            raise FatalConfigError("IG_USERNAME, IG_SESSIONID and IG_CSRFTOKEN must all be set")

        jitter = self.cfg.get("start_jitter_seconds", 0)
        if jitter and os.environ.get("BOT_NO_JITTER") != "1":
            time.sleep(random.uniform(0, jitter))

        self.loader = instaloader.Instaloader(
            quiet=True,
            max_connection_attempts=1,
            iphone_support=False,  # fewer requests per post
            rate_controller=lambda ctx: StopOn429(ctx),
        )
        self.loader.load_session(username, {"sessionid": sessionid, "csrftoken": csrftoken})
        self.profiles: dict[str, instaloader.Profile] = {}
        self.request_made = False

    def feeds(self, account: dict) -> list[str]:
        username = account.get("instagram")
        if not username:
            return []
        keys = [f"instagram:posts:{username}"]
        if account.get("stories", True):
            keys.append(f"instagram:stories:{username}")
        return keys

    def _pause(self) -> None:
        if self.request_made:
            low, high = self.cfg.get("delay_between_accounts", [15, 45])
            time.sleep(random.uniform(low, high))
        self.request_made = True

    def _profile(self, username: str):
        if username not in self.profiles:
            self.profiles[username] = instaloader.Profile.from_username(self.loader.context, username)
        return self.profiles[username]

    def fetch(self, key: str, account: dict, seen: list[str], meta: dict) -> list[Post]:
        username = account["instagram"]
        self._pause()
        try:
            profile = self._profile(username)
            if key.startswith("instagram:stories:"):
                return [
                    story_to_model(item, key, account, username)
                    for story in self.loader.get_stories([profile.userid])
                    for item in story.get_items()
                ]
            count = self.cfg.get("posts_to_check", 6)
            return [post_to_model(p, key, account) for p in itertools.islice(profile.get_posts(), count)]
        except ig.ProfileNotExistsException:
            raise ValueError(f"Instagram account not found: {username}") from None
        except ig.QueryReturnedNotFoundException as exc:
            raise ValueError(f"Instagram returned 404 for {username}: {exc}") from None
        except (
            ig.LoginRequiredException,
            ig.LoginException,
            ig.TooManyRequestsException,
            ig.QueryReturnedForbiddenException,
            ig.QueryReturnedBadRequestException,
            ig.ConnectionException,
            ig.AbortDownloadException,
        ) as exc:
            raise SourceBlocked(f"Instagram: {exc}") from exc
