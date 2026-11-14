"""Translation: DeepL when a key is set, then Google's public web endpoint, then MyMemory.

The keyless services refuse busy servers: Google answers Render with "429 Too Many
Requests", and MyMemory's daily allowance is shared by everyone on the same address.
DeepL's free plan uses your own key, so it isn't affected. Failures must never cost us
a post: if every translator fails, the post goes out without a translation.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)
GOOGLE = "https://translate.googleapis.com/translate_a/single"
MYMEMORY = "https://api.mymemory.translated.net/get"
MYMEMORY_LIMIT = 450  # it rejects queries over 500 characters; keep a margin


@dataclass
class Translation:
    text: str = ""
    romanized: str = ""
    source_lang: str = ""


class TranslationError(Exception):
    pass


def parse_response(data: list) -> Translation:
    translated, romanized = [], []
    for segment in data[0] or []:
        if segment[0]:
            translated.append(segment[0])
        # The final segment holds transliterations: [_, _, of_target, of_source].
        elif len(segment) > 3 and segment[3]:
            romanized.append(segment[3])
    # The endpoint sprinkles in zero-width spaces, which break hashtags and mentions.
    text = "".join(translated).replace("​", "").strip()
    return Translation(text, " ".join(romanized).strip(), data[2] or "")


def fix_names(text: str, names: dict) -> str:
    """Replaces whole words only, so fixing "Сомюл" leaves "Сомюля" alone."""
    for wrong, right in names.items():
        text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text)
    return text


def split_text(text: str, limit: int) -> list[tuple[str, str]]:
    """Splits into (separator, chunk) pairs; joining them back gives the text again.

    Cuts at line breaks, and at spaces inside lines longer than the limit, so a long
    line stays one line after translation.
    """
    pieces: list[tuple[str, str]] = []
    for line in text.split("\n"):
        separator = "\n"
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append((separator, line[:cut]))
            line, separator = line[cut:].lstrip(), " "
        pieces.append((separator, line))

    chunks: list[tuple[str, str]] = []
    for separator, piece in pieces:
        if chunks and chunks[-1][1] and len(chunks[-1][1]) + 1 + len(piece) <= limit and separator == "\n":
            chunks[-1] = (chunks[-1][0], chunks[-1][1] + "\n" + piece)
        else:
            chunks.append((separator, piece))
    chunks[0] = ("", chunks[0][1])
    return chunks


def deepl(text: str, target: str) -> Translation:
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not key:
        raise TranslationError("DeepL: no DEEPL_API_KEY")
    # Keys of the free plan end with ":fx" and use their own address.
    host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    try:
        response = requests.post(
            f"{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            json={"text": [text], "target_lang": target.upper(), "preserve_formatting": True},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise TranslationError(f"DeepL: {exc}") from None
    if response.status_code == 456:
        raise TranslationError("DeepL: the monthly free allowance is used up")
    if response.status_code != 200:
        raise TranslationError(f"DeepL: {response.status_code} {response.text[:200]}")
    item = response.json()["translations"][0]
    return Translation(item["text"].strip(), "", item.get("detected_source_language", "").lower())


def google(text: str, target: str) -> Translation:
    try:
        response = requests.post(
            GOOGLE,
            params={"client": "gtx", "sl": "auto", "tl": target, "dt": ["t", "rm"]},
            data={"q": text},
            timeout=20,
        )
        response.raise_for_status()
        return parse_response(response.json())
    except (requests.RequestException, ValueError, IndexError, TypeError) as exc:
        raise TranslationError(f"Google: {exc}") from None


def mymemory(text: str, target: str) -> Translation:
    translated, source = [], ""
    for separator, chunk in split_text(text, MYMEMORY_LIMIT):
        translated.append(separator)
        if not chunk.strip():
            continue  # a blank line between paragraphs
        try:
            response = requests.get(MYMEMORY, params={"q": chunk, "langpair": f"Autodetect|{target}"}, timeout=30)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TranslationError(f"MyMemory: {exc}") from None
        if "DISTINCT LANGUAGES" in str(data.get("responseDetails", "")).upper():
            return Translation(source_lang=target)  # already in the target language
        if data.get("responseStatus") != 200 or data.get("quotaFinished"):
            raise TranslationError(f"MyMemory: {data.get('responseStatus')} {data.get('responseDetails')}")
        source = source or data["responseData"].get("detectedLanguage") or ""
        # MyMemory drops line breaks at the edges of a piece; put them back.
        lead = chunk[: len(chunk) - len(chunk.lstrip("\n"))]
        trail = chunk[len(chunk.rstrip("\n")):]
        translated.append(lead + data["responseData"]["translatedText"].strip("\n") + trail)
    return Translation("".join(translated).strip(), "", source)


def translate(text: str, cfg: dict) -> Translation:
    if not text.strip() or not cfg.get("enabled", True):
        return Translation()
    target = cfg.get("target", "uk")
    result = None
    for provider in (deepl, google, mymemory):
        try:
            result = provider(text, target)
            break
        except TranslationError as exc:
            log.info("%s; trying the next translator", exc)
    if result is None:
        log.warning("Translation failed, posting without it")
        return Translation()

    if not cfg.get("romanize", True):
        result.romanized = ""
    result.text = fix_names(result.text, cfg.get("names") or {})
    skip = {target, *cfg.get("skip_languages", [])}
    if result.source_lang in skip or result.text == text.strip():
        return Translation(source_lang=result.source_lang)
    return result
