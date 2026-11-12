import argparse
import re
from html import unescape
from types import SimpleNamespace

import pytest

from bot import __main__ as app
from bot.formatting import CAPTION_LIMIT, MESSAGE_LIMIT, render
from bot.models import Media, Post, SourceBlocked, headline
from bot.sources import x as x_source
from bot.sources.instagram import StopOn429, post_to_model, story_to_model
from bot.sources.youtube import parse_feed
from bot.state import State
from bot.telegram import Telegram, TelegramError, materialize
from bot.translate import Translation, parse_response

CHANNEL = {"chat_id": "@c"}
FEED = """<?xml version="1.0"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <entry><yt:videoId>AAA</yt:videoId><title>Normal &amp; video</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=AAA"/>
  <published>2026-11-04T18:00:21+00:00</published>
  <media:group><media:thumbnail url="https://i4.ytimg.com/vi/AAA/hqdefault.jpg"/></media:group></entry>
 <entry><yt:videoId>BBB</yt:videoId><title>Short</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/BBB"/>
  <published>2026-11-03T18:00:21+00:00</published>
  <media:group><media:thumbnail url="https://i4.ytimg.com/vi/BBB/hqdefault.jpg"/></media:group></entry>
</feed>"""


def visible(html):
    return len(unescape(re.sub(r"<[^>]+>", "", html)))


def make_post(text="hello", media=None):
    return Post(id="1", source_key="k", author="A", label="L<b>", url="https://x.com/a?b=1&c=2",
                text=text, media=media or [])


# --- translation: responses as really returned by the endpoint

def test_parse_translation_uses_source_romanization():
    data = [[["안녕", "x"], [None, None, "annyeong-uk", "annyeong"]], None, "ko"]
    assert parse_response(data) == Translation("안녕", "annyeong", "ko")


def test_parse_translation_ignores_target_only_transliteration():
    data = [[["привіт світ", "hello world"], [None, None, "pryvit svit"]], None, "en"]
    assert parse_response(data) == Translation("привіт світ", "", "en")


def test_split_text_keeps_every_line_and_respects_the_limit():
    from bot.translate import split_text
    text = "a" * 30 + "\n\n" + ("word " * 40).strip() + "\nend\nfin"
    chunks = split_text(text, 50)
    assert all(len(chunk) <= 50 for _, chunk in chunks)
    assert "".join(sep + chunk for sep, chunk in chunks) == text
    assert split_text("short\ntext", 50) == [("", "short\ntext")]


class FakeReply:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Too Many Requests")


def mymemory_reply(text, lang="en"):
    return FakeReply({"responseStatus": 200, "responseData": {"translatedText": text, "detectedLanguage": lang}})


def test_falls_back_to_mymemory_when_google_blocks(monkeypatch):
    from bot import translate as tr
    asked = []
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: FakeReply([], 429))
    monkeypatch.setattr(tr.requests, "get", lambda url, params, timeout: asked.append(params["q"]) or mymemory_reply("т:" + params["q"]))
    result = tr.translate("hello\n\nworld", {"target": "uk", "romanize": False})
    assert result.text == "т:hello\n\nworld" and result.source_lang == "en" and asked == ["hello\n\nworld"]


def test_blank_lines_survive_mymemory_trimming_the_edges(monkeypatch):
    from bot import translate as tr
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: FakeReply([], 429))
    monkeypatch.setattr(tr.requests, "get", lambda url, params, timeout: mymemory_reply(params["q"].strip().upper()))
    text = "first!\n\n" + "long " * 120 + "\n\nlast"
    out = tr.translate(text, {"target": "uk"}).text
    assert out.startswith("FIRST!\n\n") and out.endswith("\n\nLAST") and out.count("\n") == 4


def test_long_text_goes_to_mymemory_in_pieces(monkeypatch):
    from bot import translate as tr
    asked = []
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: FakeReply([], 429))
    monkeypatch.setattr(tr.requests, "get", lambda url, params, timeout: asked.append(params["q"]) or mymemory_reply("x"))
    tr.translate("\n".join(["line of text number"] * 60), {"target": "uk"})
    assert len(asked) > 1 and all(len(q) <= tr.MYMEMORY_LIMIT for q in asked)


def test_ukrainian_text_is_not_translated_by_mymemory(monkeypatch):
    from bot import translate as tr
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: FakeReply([], 429))
    monkeypatch.setattr(tr.requests, "get", lambda *a, **k: FakeReply(
        {"responseStatus": 403, "responseDetails": "PLEASE SELECT TWO DISTINCT LANGUAGES", "responseData": {}}))
    assert tr.translate("Дякую всім", {"target": "uk", "skip_languages": ["uk"]}).text == ""


def test_post_goes_out_untranslated_when_both_fail(monkeypatch):
    from bot import translate as tr
    import requests
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: FakeReply([], 429))

    def down(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(tr.requests, "get", down)
    assert tr.translate("hello", {"target": "uk"}) == Translation()


def test_fix_names_replaces_whole_words_only():
    from bot.translate import fix_names
    names = {"Сомюл": "Сомюль"}
    assert fix_names("АРХІВ — Сомюл", names) == "АРХІВ — Сомюль"
    assert fix_names("Сомюл, привіт", names) == "Сомюль, привіт"
    assert fix_names("від Сомюля і Сомюль", names) == "від Сомюля і Сомюль"


# --- formatting

def test_render_layout_matches_the_channel_format():
    out = render(make_post("a < b"), Translation("т", "", "ko"), CHANNEL, has_media=False)
    assert out.caption == (
        "<b>L&lt;b&gt;</b>\n\n"
        "a &lt; b\n\n<b>🇺🇦 Переклад:</b>\n<blockquote>т</blockquote>\n\n"
        '<a href="https://x.com/a?b=1&amp;c=2">Дивитись</a>\n@c'
    )


def test_render_without_translation_has_no_heading():
    out = render(make_post("hi"), Translation(), CHANNEL, has_media=False)
    assert "Переклад" not in out.caption


def test_headline_person_vs_official():
    assert headline({"name": "Somyul", "from": "Сомюля"}, "Новий пост", "X") == "Новий пост від Сомюля в X"
    assert headline({"name": "Band"}, "Нова сторіс", "Instagram") == "Нова сторіс на акаунті Band в Instagram"


@pytest.mark.parametrize("size", [0, 500, 1020, 1024, 3000, 5000, 20000])
def test_render_respects_limits(size):
    tr = Translation("т" * size, "r" * (size // 4), "ko")
    with_media = render(make_post("з" * size), tr, CHANNEL, has_media=True)
    assert visible(with_media.caption) <= CAPTION_LIMIT
    assert visible(with_media.followup) <= MESSAGE_LIMIT
    without = render(make_post("з" * size), tr, CHANNEL, has_media=False)
    assert visible(without.caption) <= MESSAGE_LIMIT
    assert without.followup == ""


def test_long_text_moves_to_followup_and_keeps_links_on_media():
    out = render(make_post("з" * 2000), Translation(), CHANNEL, has_media=True)
    assert "Дивитись" in out.caption
    assert "ззз" in out.followup and "ззз" not in out.caption


# --- state

def test_state_roundtrip_and_trim(tmp_path):
    path = tmp_path / "s" / "x.json"
    state = State(path)
    assert not state.is_initialized("k")
    state.mark_seen("k", [])
    assert state.is_initialized("k")
    state.mark_seen("k", [str(i) for i in range(400)] + ["5"])
    state.save()
    loaded = State(path)
    assert len(loaded.seen("k")) == 300 and "399" in loaded.seen("k") and "0" not in loaded.seen("k")


# --- sources

def test_youtube_feed_video_gets_preview_short_gets_download():
    video, short = parse_feed(FEED, "k", {"name": "Band"}, True)
    assert video.text == "Normal & video" and video.label == "Нове відео на акаунті Band в YouTube"
    assert video.media[0].kind == "photo" and "maxresdefault" in video.media[0].url
    assert short.label == "Новий Shorts на акаунті Band в YouTube" and short.media[0].fetcher is not None
    assert short.media[0].thumbnail.endswith("hqdefault.jpg")


def test_youtube_shorts_download_can_be_disabled():
    _, short = parse_feed(FEED, "k", {"name": "Band"}, False)
    assert short.media[0].kind == "photo" and short.media[0].fetcher is None


def test_x_timeline_parsing():
    payload = {
        "data": [{
            "id": "10", "created_at": "2026-11-04T18:00:00.000Z",
            "text": "short &amp; https://t.co/m https://t.co/l",
            "note_tweet": {"text": "Juho &amp; Somyul https://t.co/m https://t.co/l", "entities": {"urls": [
                {"url": "https://t.co/m", "media_key": "3_1"},
                {"url": "https://t.co/l", "expanded_url": "https://site.com"},
            ]}},
            "attachments": {"media_keys": ["3_1", "7_1"]},
        }],
        "includes": {"media": [
            {"media_key": "3_1", "type": "photo", "url": "https://p/1.jpg"},
            {"media_key": "7_1", "type": "video", "preview_image_url": "https://p/v.jpg", "variants": [
                {"content_type": "application/x-mpegURL", "url": "https://v/pl.m3u8"},
                {"content_type": "video/mp4", "bit_rate": 100, "url": "https://v/low.mp4"},
                {"content_type": "video/mp4", "bit_rate": 900, "url": "https://v/high.mp4"},
            ]},
        ]},
    }
    post = x_source.parse_timeline(payload, "k", {"name": "Band", "x": "user"})[0]
    assert post.text == "Juho & Somyul  https://site.com"
    assert post.url == "https://x.com/user/status/10"
    assert post.media[0].url == "https://p/1.jpg?name=orig" and post.media[0].thumbnail == "https://p/1.jpg"
    assert post.media[1].url == "https://v/high.mp4" and post.media[1].alternatives == ["https://v/low.mp4"]


def test_x_token_tolerates_stray_whitespace(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", " abc\n")
    assert x_source.XSource({}).headers["Authorization"] == "Bearer abc"


def test_x_first_run_reads_nothing(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    calls = []
    source = x_source.XSource({})

    def fake_get(path, params=None):
        calls.append((path, params))
        return {"data": {"id": "1"}} if "/by/username/" in path else {}

    monkeypatch.setattr(source, "_get", fake_get)
    meta = {}
    assert source.fetch("k", {"x": "u", "name": "n"}, [], meta) == []
    assert [c[0] for c in calls] == ["/users/by/username/u"]  # only the free-of-posts lookup
    assert "x_start:u" in meta

    source.fetch("k", {"x": "u", "name": "n"}, [], meta)
    assert calls[-1][1]["start_time"] == meta["x_start:u"]
    source.fetch("k", {"x": "u", "name": "n"}, ["9", "100", "20"], meta)
    assert calls[-1][1]["since_id"] == "100"  # numeric, not alphabetical, maximum
    assert calls[-1][1]["exclude"] == "retweets,replies"

    source.backfill_from = "2026-11-02T00:00:00Z"
    source.fetch("k2", {"x": "u", "name": "n"}, [], meta)
    assert calls[-1][1]["start_time"] == "2026-11-02T00:00:00Z"


def test_instagram_models():
    from datetime import datetime, timezone
    date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    node = SimpleNamespace
    album = SimpleNamespace(
        typename="GraphSidecar", shortcode="abc", caption=None, date_utc=date,
        get_sidecar_nodes=lambda: [node(is_video=False, display_url="p"), node(is_video=True, display_url="t", video_url="v")],
    )
    post = post_to_model(album, "k", {"name": "Band"})
    assert post.url.endswith("/p/abc/") and post.text == ""
    assert [m.kind for m in post.media] == ["photo", "video"] and post.media[1].thumbnail == "t"

    item = SimpleNamespace(mediaid=5, is_video=True, video_url="v", url="t", caption=None, date_utc=date)
    story = story_to_model(item, "k", {"name": "Band"}, "user")
    assert story.id == "5" and story.url.endswith("/stories/user/5/") and story.media[0].url == "v"


def test_instagram_stops_on_429_instead_of_sleeping():
    from instaloader.exceptions import TooManyRequestsException
    with pytest.raises(TooManyRequestsException):
        StopOn429(None).handle_429("graphql")


def test_instagram_constructor_accepts_our_options(monkeypatch):
    monkeypatch.setenv("IG_USERNAME", "u")
    monkeypatch.setenv("IG_SESSIONID", "s")
    monkeypatch.setenv("IG_CSRFTOKEN", "c")
    monkeypatch.setenv("BOT_NO_JITTER", "1")
    from bot.sources.instagram import InstagramSource
    source = InstagramSource({})
    assert source.loader.context.username == "u"
    assert source.feeds({"instagram": "a"}) == ["instagram:posts:a", "instagram:stories:a"]
    assert source.feeds({"instagram": "a", "stories": False}) == ["instagram:posts:a"]
    assert source.feeds({"name": "no instagram"}) == []


# --- delivery

def test_failed_video_falls_back_to_thumbnail(tmp_path, monkeypatch):
    def fetcher(folder):
        raise RuntimeError("Sign in to confirm you're not a bot")
    monkeypatch.setattr("bot.telegram.download", lambda url, dest, limit: dest)
    kind, path = materialize(Media(kind="video", fetcher=fetcher, thumbnail="https://t"), tmp_path, 0)
    assert kind == "photo" and "thumb" in path.name


def test_oversized_video_tries_next_variant(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.telegram.download", lambda url, dest, limit: None if "high" in url else dest)
    kind, path = materialize(Media(kind="video", url="https://high", alternatives=["https://low"]), tmp_path, 0)
    assert kind == "video" and path.name == "0_1.mp4"


class RecordingTelegram(Telegram):
    def __init__(self, fail_media=False):
        super().__init__("token", "@c")
        self.calls = []
        self.fail_media = fail_media

    def _call(self, method, data, files=None):
        self.calls.append((method, data, files))
        if self.fail_media and method != "sendMessage":
            raise TelegramError(400, "bad media")
        return {"message_id": 7} if method != "sendMediaGroup" else [{"message_id": 7}]


def stub_media(monkeypatch, tmp_path):
    monkeypatch.setattr("bot.telegram.materialize", lambda m, folder, i: ("photo", tmp_path / f"{i}.jpg"))


def test_album_of_12_splits_and_captions_once(tmp_path, monkeypatch):
    stub_media(monkeypatch, tmp_path)
    tg = RecordingTelegram()
    tg.send_post(SimpleNamespace(caption="cap", followup=""), [Media("photo", "u")] * 12)
    assert [c[0] for c in tg.calls] == ["sendMediaGroup", "sendMediaGroup"]
    assert '"caption": "cap"' in tg.calls[0][1]["media"] and "caption" not in tg.calls[1][1]["media"]


def test_rejected_media_still_publishes_text(tmp_path, monkeypatch):
    stub_media(monkeypatch, tmp_path)
    tg = RecordingTelegram(fail_media=True)
    tg.send_post(SimpleNamespace(caption="cap", followup=""), [Media("photo", "u")])
    assert [c[0] for c in tg.calls] == ["sendPhoto", "sendMessage"]


def test_followup_replies_to_media(tmp_path, monkeypatch):
    stub_media(monkeypatch, tmp_path)
    tg = RecordingTelegram()
    tg.send_post(SimpleNamespace(caption="cap", followup="long"), [Media("photo", "u")])
    assert tg.calls[1][0] == "sendMessage" and '"message_id": 7' in tg.calls[1][1]["reply_parameters"]


# --- the main loop

class FakeSource:
    name = "fake"

    def __init__(self, batches):
        self.batches = batches

    def feeds(self, account):
        return ["fake:a"]

    def fetch(self, key, account, seen, meta):
        result = self.batches.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def timed(pid, ts):
    return Post(id=pid, source_key="fake:a", author="A", label="L", url="https://u", timestamp=ts)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "PAUSE_BETWEEN_POSTS", 0)
    monkeypatch.setattr(app, "translate", lambda text, cfg: Translation())
    args = argparse.Namespace(dry_run=False, send_latest=0, backfill_days=0)
    cfg = {"channel": CHANNEL, "accounts": [{"name": "A"}], "block_cooldown_hours": 6}

    class Tg:
        def __init__(self):
            self.sent, self.alerts, self.fail_on = [], [], None

        def send_post(self, rendered, media):
            if self.fail_on and self.fail_on in rendered.caption:
                raise TelegramError(500, "boom")
            self.sent.append(rendered.caption)

        def alert_admin(self, text):
            self.alerts.append(text)

    return SimpleNamespace(args=args, cfg=cfg, tg=Tg(), tmp=tmp_path, mp=monkeypatch)


def run(h, batches):
    h.mp.setattr(app, "build_source", lambda platform, cfg: FakeSource(batches))
    return app.run_platform("fake", h.cfg, h.tg, h.args)


def test_first_run_sends_nothing_then_only_new_in_order(harness):
    assert run(harness, [[timed("old", 1)]]) == 0
    assert harness.tg.sent == []
    assert run(harness, [[timed("c", 30), timed("old", 1), timed("b", 20)]]) == 0
    assert len(harness.tg.sent) == 2
    assert State(harness.tmp / "state" / "fake.json").seen("fake:a") == ["old", "b", "c"]
    assert run(harness, [[timed("c", 30), timed("b", 20)]]) == 0
    assert len(harness.tg.sent) == 2  # no duplicates


def test_send_failure_keeps_unsent_posts_for_retry(harness):
    run(harness, [[]])
    a, b = timed("a", 1), timed("b", 2)
    b.label = "FAILME"
    harness.tg.fail_on = "FAILME"
    assert run(harness, [[a, b]]) == 1
    assert State(harness.tmp / "state" / "fake.json").seen("fake:a") == ["a"]
    harness.tg.fail_on = None
    assert run(harness, [[a, b]]) == 0
    assert State(harness.tmp / "state" / "fake.json").seen("fake:a") == ["a", "b"]


def test_backfill_sends_recent_only_and_never_repeats(harness):
    import time
    now = time.time()
    harness.args.backfill_days = 3
    old, recent = timed("old", now - 5 * 86400), timed("recent", now - 86400)
    assert run(harness, [[old, recent]]) == 0
    assert len(harness.tg.sent) == 1
    assert run(harness, [[old, recent]]) == 0  # repeating the backfill is safe
    assert len(harness.tg.sent) == 1
    harness.args.backfill_days = 0
    assert run(harness, [[old, recent]]) == 0  # and normal runs don't pick up the old post
    assert len(harness.tg.sent) == 1


def test_block_alerts_once_and_cools_down(harness):
    assert run(harness, [SourceBlocked("checkpoint")]) == 0
    assert len(harness.tg.alerts) == 1
    assert State(harness.tmp / "state" / "fake.json").blocked_until > 0
    # During the cooldown the source isn't even built, so no requests are made.
    harness.mp.setattr(app, "build_source", lambda p, c: pytest.fail("must not touch the platform"))
    assert app.run_platform("fake", harness.cfg, harness.tg, harness.args) == 0
    assert len(harness.tg.alerts) == 1


def test_dry_run_writes_nothing(harness, capsys):
    harness.args.dry_run = True
    harness.mp.setattr(app, "build_source", lambda p, c: FakeSource([[timed("a", 1)]]))
    assert app.run_platform("fake", harness.cfg, None, harness.args) == 0
    assert "===== a" in capsys.readouterr().out
    assert not (harness.tmp / "state" / "fake.json").exists()
