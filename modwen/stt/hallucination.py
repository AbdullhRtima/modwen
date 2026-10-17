"""Filter well-known Whisper hallucinations (mostly subtitle credits learned from YouTube data)."""

from __future__ import annotations

import re

_PHRASES = [
    # Arabic
    "ترجمة نانسي قنقر",
    "نانسي قنقر",
    "اشتركوا في القناة",
    "اشترك في القناة",
    "لا تنسوا الاشتراك",
    "شكرا للمشاهدة",
    "شكراً للمشاهدة",
    "شكرا على المشاهدة",
    "شكراً على المشاهدة",
    "ترجمة وتدقيق",
    "تم الترجمة",
    "سبحان الله وبحمده",
    "موسيقى",
    # English
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe to my channel",
    "like and subscribe",
    "subtitles by",
    "amara.org",
    "transcribed by",
]

_norm_re = re.compile(r"[\s\.\!\?،,:;\-–—\"'«»()\[\]]+")


def _norm(text: str) -> str:
    text = text.lower().replace("ً", "").replace("ـ", "")
    return _norm_re.sub(" ", text).strip()


# Anything that is not Arabic, Latin, digits, whitespace or common punctuation cannot appear in an
# Arabic/English meeting; Whisper emits such characters (CJK, Cyrillic, ...) when it is guessing.
_FOREIGN_RE = re.compile(
    r"[^\u0000-ɏ؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿"
    r" -⁯₠-⃏،؛؟٪-٭]"
)


def clean_text(text: str) -> tuple[str, int]:
    """Remove foreign-script characters. Returns (cleaned text, number of characters removed)."""
    cleaned, n = _FOREIGN_RE.subn("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip(), n


def is_hallucination(text: str, extra: list[str] | None = None) -> bool:
    t = _norm(text)
    if not t:
        return True
    phrases = _PHRASES + list(extra or [])
    for p in phrases:
        pn = _norm(p)
        # Whole segment is (mostly) the phrase.
        if pn and pn in t and len(t) <= len(pn) + 12:
            return True
    # Degenerate repetition: one token repeated ("this this this this"), or two alternating many times.
    tokens = t.split()
    if (len(tokens) >= 4 and len(set(tokens)) == 1) or (len(tokens) >= 6 and len(set(tokens)) <= 2):
        return True
    # Single bracketed sound tag like [Music] / (موسيقى).
    if re.fullmatch(r"[\[\(].{0,20}[\]\)]", text.strip()):
        return True
    return False
