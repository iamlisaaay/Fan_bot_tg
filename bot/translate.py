"""Free translation and romanization through Google's public web endpoint (no key).

The endpoint is unofficial, so failures must never cost us a post: on any error
the post simply goes out without a translation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)
ENDPOINT = "https://translate.googleapis.com/translate_a/single"


@dataclass
class Translation:
    text: str = ""
    romanized: str = ""
    source_lang: str = ""


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


def translate(text: str, cfg: dict) -> Translation:
    if not text.strip() or not cfg.get("enabled", True):
        return Translation()
    target = cfg.get("target", "uk")
    try:
        response = requests.post(
            ENDPOINT,
            params={"client": "gtx", "sl": "auto", "tl": target, "dt": ["t", "rm"]},
            data={"q": text},
            timeout=20,
        )
        response.raise_for_status()
        result = parse_response(response.json())
    except (requests.RequestException, ValueError, IndexError, TypeError) as exc:
        log.warning("Translation failed, posting without it: %s", exc)
        return Translation()

    if not cfg.get("romanize", True):
        result.romanized = ""
    skip = {target, *cfg.get("skip_languages", [])}
    if result.source_lang in skip or result.text == text.strip():
        return Translation(source_lang=result.source_lang)
    return result
