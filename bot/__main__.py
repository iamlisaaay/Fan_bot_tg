"""Entry point: python -m bot [--platforms youtube,x] [--dry-run] [--send-latest N]"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .formatting import render
from .models import FatalConfigError, Post, SourceBlocked
from .state import State
from .telegram import Telegram, TelegramError
from .translate import translate

log = logging.getLogger("bot")
ROOT = Path(__file__).resolve().parent.parent
ALL_PLATFORMS = ["youtube", "x", "instagram"]
PAUSE_BETWEEN_POSTS = 3


def build_source(platform: str, cfg: dict):
    # Imported here so running only YouTube doesn't require the others' setup.
    if platform == "youtube":
        from .sources.youtube import YouTubeSource as cls
    elif platform == "x":
        from .sources.x import XSource as cls
    elif platform == "instagram":
        from .sources.instagram import InstagramSource as cls
    else:
        raise FatalConfigError(f"Unknown platform: {platform}")
    return cls(cfg)


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    channel = cfg.get("channel") or {}
    if not channel.get("chat_id"):
        raise FatalConfigError("config.yaml: channel.chat_id is empty")
    cfg["accounts"] = [a for a in cfg.get("accounts") or [] if a.get("enabled", True)]
    for account in cfg["accounts"]:
        if not account.get("name"):
            raise FatalConfigError(f"config.yaml: account without a name: {account}")
    return cfg


def publish(post: Post, cfg: dict, telegram: Telegram | None) -> None:
    translation = translate(post.text, cfg.get("translation", {}))
    rendered = render(post, translation, cfg["channel"], has_media=bool(post.media))
    if telegram is None:
        media = ", ".join(m.kind for m in post.media) or "no media"
        extra = f"\n--- followup ---\n{rendered.followup}" if rendered.followup else ""
        print(f"\n===== {post.id} [{media}]\n{rendered.caption}{extra}")
        return
    telegram.send_post(rendered, post.media)
    time.sleep(PAUSE_BETWEEN_POSTS)


def process_feed(key, account, source, state, cfg, telegram, args) -> None:
    posts = source.fetch(key, account, state.seen(key), state.meta)
    posts.sort(key=lambda p: p.timestamp)

    if args.send_latest or args.dry_run:
        for post in posts[-(args.send_latest or 2):]:
            publish(post, cfg, telegram)
        return

    cutoff = time.time() - args.backfill_days * 86400
    if args.backfill_days:
        # Older posts count as seen, so they never come up on later runs either.
        state.mark_seen(key, [p.id for p in posts if p.timestamp < cutoff])
        state.save()
    elif not state.is_initialized(key):
        # First sight of this feed: remember the backlog instead of flooding the channel.
        state.mark_seen(key, [p.id for p in posts])
        state.save()
        log.info("%s: first run, %d existing posts remembered, nothing sent", key, len(posts))
        return

    new = [p for p in posts if p.id not in state.seen(key)]
    log.info("%s: %d new", key, len(new))
    for post in new:
        publish(post, cfg, telegram)
        # Saved after every post: a crash mid-run can't cause duplicates.
        state.mark_seen(key, [post.id])
        state.save()


def run_platform(platform: str, cfg: dict, telegram: Telegram | None, args) -> int:
    """Returns the number of errors."""
    state = State(ROOT / "state" / f"{platform}.json")
    if state.blocked_until > time.time():
        hours = (state.blocked_until - time.time()) / 3600
        log.info("%s: cooling down after a block, %.1f h left", platform, hours)
        return 0

    try:
        source = build_source(platform, cfg)
    except FatalConfigError as exc:
        log.error("%s skipped: %s", platform, exc)
        return 1

    if args.backfill_days:
        start = datetime.now(timezone.utc) - timedelta(days=args.backfill_days)
        source.backfill_from = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    errors = 0
    for account in cfg["accounts"]:
        for key in source.feeds(account):
            try:
                process_feed(key, account, source, state, cfg, telegram, args)
            except SourceBlocked as exc:
                hours = cfg.get("block_cooldown_hours", 6)
                log.error("%s blocked for %s h: %s", platform, hours, exc)
                if not args.dry_run:
                    state.blocked_until = time.time() + hours * 3600
                    state.save()
                if telegram:
                    telegram.alert_admin(f"⚠️ {platform}: доступ обмежено, пауза {hours} год.\n{exc}")
                return errors
            except TelegramError as exc:
                # The feed's order is preserved: unsent posts are retried on the next run.
                log.error("%s: Telegram refused: %s", key, exc)
                errors += 1
            except Exception as exc:  # one bad feed must not stop the others
                log.exception("%s failed: %s", key, exc)
                errors += 1
    if not args.dry_run:
        state.save()
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitors social networks and posts to a Telegram channel")
    parser.add_argument("--platforms", default=",".join(ALL_PLATFORMS), help="comma-separated list")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="print the latest posts, send and save nothing")
    parser.add_argument("--send-latest", type=int, default=0, metavar="N",
                        help="really send the N newest posts per feed, to test the formatting")
    parser.add_argument("--backfill-days", type=int, default=0, metavar="N",
                        help="one-off: also send unseen posts from the last N days")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # requests logs full URLs at this level, and Telegram's contain the bot token.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(ROOT / ".env")

    try:
        cfg = load_config(Path(args.config))
        telegram = None
        if not args.dry_run:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                raise FatalConfigError("TELEGRAM_BOT_TOKEN is not set")
            if cfg["channel"]["chat_id"] == "@your_channel":
                raise FatalConfigError("config.yaml: set channel.chat_id to your channel")
            admin_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
            telegram = Telegram(token, cfg["channel"]["chat_id"], admin_id)
    except FatalConfigError as exc:
        log.error("%s", exc)
        return 2

    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    errors = sum(run_platform(p, cfg, telegram, args) for p in platforms)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
