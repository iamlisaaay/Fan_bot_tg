"""Hand-written posts: send a post to the bot, check the preview, approve it with a button.

Runs as one short pass on each GitHub Actions run, so replies take up to ~15 minutes.
Nothing is stored between runs: Telegram keeps unprocessed updates for 24 hours, and
approving copies the preview messages themselves, whose ids travel in the button.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

from dotenv import load_dotenv

from .__main__ import ROOT, load_config
from .formatting import render
from .models import Post
from .telegram import Telegram, TelegramError
from .translate import translate

log = logging.getLogger("bot.compose")
HELP = (
    "Надішли мені пост: текст, фото, альбом, відео або файл.\n"
    "Я перевіряю повідомлення раз на 15 хвилин, тому показую чернетку й публікую не одразу.\n"
    "У канал пост іде лише після кнопки «Опублікувати». Якщо терміново, публікуй сама.\n\n"
    "Якщо останній рядок — це лише посилання, воно стане посиланням «Дивитись».\n"
    "Щоб фото не стискались, надсилай їх файлами."
)
MEDIA_KINDS = ("video", "animation", "document", "audio")


def extract_media(message: dict) -> list[tuple[str, str]]:
    if message.get("photo"):
        return [("photo", message["photo"][-1]["file_id"])]  # the last size is the largest
    return [(kind, message[kind]["file_id"]) for kind in MEDIA_KINDS if message.get(kind)]


def split_source_link(text: str) -> tuple[str, str]:
    """A last line that's only a URL becomes the source link."""
    lines = text.rstrip().split("\n")
    if re.fullmatch(r"https?://\S+", lines[-1].strip()):
        return "\n".join(lines[:-1]).rstrip(), lines[-1].strip()
    return text.strip(), ""


def encode_ids(ids: list[int]) -> str:
    # Button data is limited to 64 bytes, so ids are stored as offsets from the first.
    return f"{ids[0]}:" + ",".join(str(i - ids[0]) for i in ids)


def decode_ids(data: str) -> list[int]:
    first, offsets = data.split(":")
    return [int(first) + int(offset) for offset in offsets.split(",")]


class Composer:
    def __init__(self, telegram: Telegram, cfg: dict, admin_id: str):
        self.telegram = telegram
        self.cfg = cfg
        self.admin_id = str(admin_id)
        self.albums: dict[str, list[dict]] = {}
        self.handled: set[str] = set()  # several clicks can arrive within one pass

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_button(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def _on_message(self, message: dict) -> None:
        # Anyone can find the bot; only the owner may post through it.
        if str(message.get("from", {}).get("id")) != self.admin_id:
            return
        if message.get("text", "").startswith("/"):
            self._say(HELP)
        elif message.get("media_group_id"):
            # Telegram delivers an album as separate messages; collect them first.
            self.albums.setdefault(message["media_group_id"], []).append(message)
        else:
            self._make_draft([message])

    def flush_albums(self) -> None:
        albums, self.albums = self.albums, {}
        for messages in albums.values():
            self._make_draft(sorted(messages, key=lambda m: m["message_id"]))

    def _make_draft(self, messages: list[dict]) -> None:
        media = [item for m in messages for item in extract_media(m)]
        raw = next((m.get("caption") or m.get("text") for m in messages if m.get("caption") or m.get("text")), "")
        if not media and not raw:
            self._say("Такий тип повідомлення я поки не вмію публікувати.")
            return

        text, link = split_source_link(raw) if raw else ("", "")
        post = Post(id=str(messages[0]["message_id"]), source_key="manual", author="", label="", url=link, text=text)
        translation = translate(text, self.cfg.get("translation", {}))
        rendered = render(post, translation, self.cfg["channel"], has_media=bool(media))
        try:
            ids = self.telegram.send_by_file_id(self.admin_id, media, rendered)
        except TelegramError as exc:
            self._say(f"Не вдалось показати чернетку: {exc}")
            return
        encoded = encode_ids(ids)
        self.telegram._call("sendMessage", {
            "chat_id": self.admin_id,
            "text": "Ось так це виглядатиме в каналі. Публікуємо?\nПісля натискання зачекай до 15 хвилин.",
            "reply_markup": json.dumps({"inline_keyboard": [[
                {"text": "✅ Опублікувати", "callback_data": f"pub:{encoded}"},
                {"text": "❌ Скасувати", "callback_data": f"del:{encoded}"},
            ]]}),
        })

    def _on_button(self, query: dict) -> None:
        if str(query["from"]["id"]) != self.admin_id:
            return
        action, _, encoded = query.get("data", "").partition(":")
        try:
            # Clicks older than a few minutes can't be answered; that's expected here.
            self.telegram._call("answerCallbackQuery", {"callback_query_id": query["id"]})
        except TelegramError:
            pass
        if encoded in self.handled:
            return
        self.handled.add(encoded)

        if action == "pub":
            try:
                self.telegram._call("copyMessages", {
                    "chat_id": self.telegram.chat_id,
                    "from_chat_id": self.admin_id,
                    "message_ids": json.dumps(decode_ids(encoded)),
                })
            except TelegramError as exc:
                log.error("Publishing failed: %s", exc)
                self.handled.discard(encoded)
                self._say(f"Не вдалось опублікувати: {exc}\nМожна натиснути кнопку ще раз.")
                return
        self.telegram._call("editMessageText", {
            "chat_id": self.admin_id,
            "message_id": query["message"]["message_id"],
            "text": "✅ Опубліковано" if action == "pub" else "❌ Скасовано",
        })

    def _say(self, text: str) -> None:
        self.telegram._call("sendMessage", {"chat_id": self.admin_id, "text": text})


def run_once(telegram: Telegram, composer: Composer) -> int:
    """Handles everything waiting right now. Returns the number of failed updates."""
    updates = telegram._call("getUpdates", {
        "timeout": 0,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    })
    errors = 0
    for update in updates:
        try:
            composer.handle(update)
        except Exception:  # one bad message must not block the ones after it
            log.exception("Could not handle an update")
            errors += 1
    try:
        composer.flush_albums()
    except Exception:
        log.exception("Could not handle an album")
        errors += 1
    if updates:
        # This is what tells Telegram not to deliver these updates again.
        telegram._call("getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0})
    log.info("Handled %d updates", len(updates))
    return errors


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(ROOT / ".env")

    # Pasting into GitHub secrets easily brings a stray space or line break along.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    admin_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if not (token and admin_id):
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID must both be set")
        return 2

    cfg = load_config(ROOT / "config.yaml")
    telegram = Telegram(token, cfg["channel"]["chat_id"], admin_id)
    try:
        return 1 if run_once(telegram, Composer(telegram, cfg, admin_id)) else 0
    except TelegramError as exc:
        log.error("Telegram error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
