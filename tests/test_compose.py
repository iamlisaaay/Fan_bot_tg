import json

import pytest

from bot import compose
from bot.compose import Composer, decode_ids, encode_ids, run_once, split_source_link
from bot.telegram import Telegram, TelegramError
from bot.translate import Translation

ADMIN = 111
CFG = {"channel": {"chat_id": "@c"}}


class FakeTelegram(Telegram):
    def __init__(self):
        super().__init__("token", "@c", str(ADMIN))
        self.calls = []
        self.fail = None
        self.next_id = 100
        self.pending_updates = []

    def _new_id(self):
        self.next_id += 1
        return {"message_id": self.next_id}

    def _call(self, method, data, files=None):
        if self.fail == method:
            raise TelegramError(400, "nope")
        self.calls.append((method, data))
        if method == "getUpdates":
            if "offset" in data:
                self.pending_updates = [u for u in self.pending_updates if u["update_id"] >= data["offset"]]
            return self.pending_updates
        if method == "sendMediaGroup":
            return [self._new_id() for _ in json.loads(data["media"])]
        return self._new_id()

    def named(self, method):
        return [d for m, d in self.calls if m == method]


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(compose, "translate", lambda text, cfg: Translation())
    return Composer(FakeTelegram(), CFG, str(ADMIN))


def msg(mid=1, sender=ADMIN, **fields):
    return {"message": {"message_id": mid, "from": {"id": sender}, **fields}}


def buttons(bot):
    """The button data of the last preview."""
    markup = json.loads(bot.telegram.named("sendMessage")[-1]["reply_markup"])
    return [b["callback_data"] for b in markup["inline_keyboard"][0]]


def press(bot, data, sender=ADMIN):
    bot.handle({"callback_query": {
        "id": "q", "from": {"id": sender}, "data": data, "message": {"message_id": 9},
    }})


def test_preview_goes_to_admin_and_nothing_reaches_the_channel(bot):
    bot.handle(msg(text="Привіт"))
    assert bot.telegram.named("copyMessages") == []
    assert all(d["chat_id"] == str(ADMIN) for d in bot.telegram.named("sendMessage"))
    assert bot.telegram.named("sendMessage")[0]["text"] == "Привіт\n\n@c"


def test_approve_copies_the_preview_to_the_channel_once(bot):
    bot.handle(msg(text="Привіт"))
    publish, _ = buttons(bot)
    press(bot, publish)
    press(bot, publish)  # a double click in the same pass
    copies = bot.telegram.named("copyMessages")
    assert len(copies) == 1
    assert copies[0]["chat_id"] == "@c" and copies[0]["from_chat_id"] == str(ADMIN)
    assert json.loads(copies[0]["message_ids"]) == [101]
    assert bot.telegram.named("editMessageText")[0]["text"] == "✅ Опубліковано"


def test_cancel_never_publishes(bot):
    bot.handle(msg(text="Привіт"))
    publish, cancel = buttons(bot)
    press(bot, cancel)
    press(bot, publish)
    assert bot.telegram.named("copyMessages") == []
    assert bot.telegram.named("editMessageText")[0]["text"] == "❌ Скасовано"


def test_strangers_are_ignored(bot):
    bot.handle(msg(sender=999, text="spam"))
    assert bot.telegram.calls == []
    bot.handle(msg(text="Привіт"))
    press(bot, buttons(bot)[0], sender=999)
    assert bot.telegram.named("copyMessages") == []


def test_failed_publish_can_be_retried(bot):
    bot.handle(msg(text="Привіт"))
    publish = buttons(bot)[0]
    bot.telegram.fail = "copyMessages"
    press(bot, publish)
    assert bot.telegram.named("editMessageText") == []
    bot.telegram.fail = None
    press(bot, publish)
    assert len(bot.telegram.named("copyMessages")) == 1


def test_old_click_that_cannot_be_answered_still_publishes(bot):
    bot.handle(msg(text="Привіт"))
    publish = buttons(bot)[0]
    bot.telegram.fail = "answerCallbackQuery"
    press(bot, publish)
    assert len(bot.telegram.named("copyMessages")) == 1


def test_album_is_previewed_as_one_post_and_all_parts_are_published(bot):
    bot.handle(msg(2, media_group_id="g", photo=[{"file_id": "s2"}, {"file_id": "big2"}]))
    bot.handle(msg(1, media_group_id="g", caption="Текст", photo=[{"file_id": "big1"}]))
    assert bot.telegram.calls == []
    bot.flush_albums()
    media = json.loads(bot.telegram.named("sendMediaGroup")[0]["media"])
    assert [m["media"] for m in media] == ["big1", "big2"]
    assert media[0]["caption"] == "Текст\n\n@c" and "caption" not in media[1]
    press(bot, buttons(bot)[0])
    assert json.loads(bot.telegram.named("copyMessages")[0]["message_ids"]) == [101, 102]


def test_long_caption_followup_is_published_too(bot):
    bot.handle(msg(photo=[{"file_id": "p"}], caption="з" * 1020))
    assert "ззз" not in bot.telegram.named("sendPhoto")[0]["caption"]
    bot.handle(msg(2, document={"file_id": "d"}, caption="т" * 1020))
    press(bot, buttons(bot)[0])
    assert len(json.loads(bot.telegram.named("copyMessages")[0]["message_ids"])) == 2


def test_file_stays_a_file_so_quality_is_kept(bot):
    bot.handle(msg(document={"file_id": "doc"}, caption="Оригінал"))
    sent = bot.telegram.named("sendDocument")[0]
    assert sent["document"] == "doc" and sent["caption"] == "Оригінал\n\n@c"


def test_last_line_link_becomes_the_source_link(bot):
    bot.handle(msg(text="Новина\nhttps://x.com/a/1"))
    text = bot.telegram.named("sendMessage")[0]["text"]
    assert text == 'Новина\n\n<a href="https://x.com/a/1">Дивитись</a>\n@c'


def test_button_data_fits_telegram_limit_for_the_largest_post():
    ids = list(range(2_000_000_000, 2_000_000_011))  # a full album plus a follow-up, with huge ids
    data = "pub:" + encode_ids(ids)
    assert len(data.encode()) <= 64
    assert decode_ids(encode_ids(ids)) == ids


@pytest.mark.parametrize("raw,expected", [
    ("текст", ("текст", "")),
    ("текст https://a.b у рядку", ("текст https://a.b у рядку", "")),
    ("https://a.b", ("", "https://a.b")),
])
def test_split_source_link(raw, expected):
    assert split_source_link(raw) == expected


def test_commands_show_help_instead_of_a_draft(bot):
    bot.handle(msg(text="/start"))
    assert "Опублікувати" in bot.telegram.named("sendMessage")[0]["text"]
    assert "reply_markup" not in bot.telegram.named("sendMessage")[0]


def test_unsupported_message_is_explained(bot):
    bot.handle(msg(sticker={"file_id": "x"}))
    assert "не вмію" in bot.telegram.named("sendMessage")[0]["text"]


def test_pass_confirms_updates_so_the_next_run_does_not_repeat_them(bot):
    bot.telegram.pending_updates = [{"update_id": 7, **msg(text="Привіт")}]
    assert run_once(bot.telegram, bot) == 0
    assert bot.telegram.pending_updates == []
    assert len(bot.telegram.named("getUpdates")) == 2


def test_bad_update_does_not_block_the_others(bot):
    bot.telegram.pending_updates = [
        {"update_id": 7, **msg(1, photo=[])},
        {"update_id": 8, "callback_query": {"from": {"id": ADMIN}, "data": "pub:bad", "id": "q", "message": {"message_id": 1}}},
        {"update_id": 9, **msg(3, text="Привіт")},
    ]
    assert run_once(bot.telegram, bot) == 1
    assert any(d.get("text") == "Привіт\n\n@c" for d in bot.telegram.named("sendMessage"))
    assert bot.telegram.pending_updates == []
