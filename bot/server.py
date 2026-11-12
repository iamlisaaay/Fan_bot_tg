"""Always-on version of the bot for Render: python -m bot.server

- answers hand-written posts right away (long polling),
- checks YouTube and X every few minutes,
- serves a tiny web page, so a pinger can keep Render's free service awake.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

from .__main__ import ROOT, load_config, run_platform
from .compose import Composer
from .github_state import GitHubState
from .state import State
from .telegram import Telegram, TelegramError

log = logging.getLogger("bot.server")
STATE_FILES = ["youtube.json", "x.json", "instagram.json"]


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # a ping every few minutes would flood the log
        pass


def serve_pings(port: int) -> None:
    ThreadingHTTPServer(("0.0.0.0", port), Ping).serve_forever()


def monitor_forever(cfg: dict, telegram: Telegram, platforms: list[str], minutes: float) -> None:
    args = argparse.Namespace(dry_run=False, send_latest=0, backfill_days=0)
    while True:
        started = time.time()
        for platform in platforms:
            try:
                errors = run_platform(platform, cfg, telegram, args)
                if errors:
                    log.warning("%s: %d errors this round", platform, errors)
            except Exception:  # the loop must survive anything one round throws
                log.exception("%s round failed", platform)
        time.sleep(max(60.0, minutes * 60 - (time.time() - started)))


def compose_forever(telegram: Telegram, composer: Composer) -> None:
    allowed = json.dumps(["message", "callback_query"])
    offset = None
    while True:
        try:
            # With an album in progress, a short wait tells us when all its parts have arrived.
            params = {"timeout": 2 if composer.albums else 30, "allowed_updates": allowed}
            if offset is not None:
                params["offset"] = offset
            updates = telegram._call("getUpdates", params)
            for update in updates:
                offset = update["update_id"] + 1  # first, so one bad update can't block the queue
                try:
                    composer.handle(update)
                except Exception:
                    log.exception("Could not handle an update")
            if not updates:
                composer.flush_albums()
                composer.handled.clear()  # buttons only need guarding within a burst of clicks
        except TelegramError as exc:
            # 409 means another copy is polling, e.g. during a redeploy; it sorts itself out.
            log.warning("Telegram: %s", exc)
            time.sleep(10)
        except Exception:
            log.exception("Polling failed")
            time.sleep(10)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(ROOT / ".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    admin_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if not (token and admin_id):
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID must both be set")
        return 2
    cfg = load_config(ROOT / "config.yaml")

    repo = os.environ.get("GITHUB_STATE_REPO", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if repo and github_token:
        store = GitHubState(repo, github_token, ROOT / "state")
        store.download(STATE_FILES)

        def upload(path):
            try:
                store.upload(path)
            except Exception as exc:  # GitHub being down must not stop posting
                log.error("Could not save %s to GitHub: %s", path.name, exc)

        State.on_save = upload
    else:
        log.warning("GITHUB_STATE_REPO/GITHUB_TOKEN not set: state lives only on this disk")

    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=serve_pings, args=(port,), daemon=True).start()

    platforms = [p.strip() for p in os.environ.get("PLATFORMS", "youtube,x").split(",") if p.strip()]
    minutes = float(os.environ.get("CHECK_EVERY_MINUTES", "10"))
    if platforms:
        monitor_bot = Telegram(token, cfg["channel"]["chat_id"], admin_id)
        threading.Thread(
            target=monitor_forever, args=(cfg, monitor_bot, platforms, minutes), daemon=True
        ).start()
    log.info("Running: port %s, checking %s every %s min", port, ",".join(platforms) or "nothing", minutes)

    compose_bot = Telegram(token, cfg["channel"]["chat_id"], admin_id)
    compose_forever(compose_bot, Composer(compose_bot, cfg, admin_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
