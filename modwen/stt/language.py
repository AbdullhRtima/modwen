"""Language decisions for Arabic / English / code-switched speech (design §4)."""

from __future__ import annotations

import re

ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
LATIN_RE = re.compile(r"[A-Za-z]")
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def choose_language(
    probs: dict[str, float] | None,
    mode: str = "auto",
    allowed: list[str] | tuple[str, ...] = ("ar", "en"),
    arabic_threshold: float = 0.2,
) -> str:
    """Pick the language to transcribe an utterance in.

    * ``mode`` "en"/"ar" forces the language (Start dialog: English only / Arabic only).
    * In auto mode, probabilities are restricted to the allowed set (Whisper often labels Egyptian or Gulf
      Arabic as Persian or Urdu). If P(ar) >= threshold we transcribe as Arabic with the bilingual prompt,
      which keeps English terms in Latin script; forcing English on mixed speech tends to translate the Arabic.
    """
    if mode in ("en", "ar"):
        return mode
    allowed = list(allowed) or ["ar", "en"]
    if not probs:
        return "ar" if "ar" in allowed else allowed[0]
    # Fold close relatives into Arabic (dialects are often misdetected).
    folded = dict(probs)
    if "ar" in allowed:
        for alias in ("fa", "ur", "ps", "sd", "ug", "ku"):
            folded["ar"] = folded.get("ar", 0.0) + folded.pop(alias, 0.0)
    total = sum(folded.get(lang, 0.0) for lang in allowed)
    if total <= 0:
        return "ar" if "ar" in allowed else allowed[0]
    norm = {lang: folded.get(lang, 0.0) / total for lang in allowed}
    if "ar" in norm and norm["ar"] >= arabic_threshold:
        return "ar"
    return max(norm, key=norm.get)  # type: ignore[arg-type]


def script_language(text: str) -> str:
    """Tag a transcribed segment as 'ar', 'en' or 'mixed' (both scripts used: code-switching)."""
    ar_words = la_words = 0
    for word in text.split():
        a = len(ARABIC_RE.findall(word))
        latin = len(LATIN_RE.findall(word))
        if a >= 2 and a >= latin:
            ar_words += 1
        elif latin >= 2:
            la_words += 1
    if ar_words and la_words:
        return "mixed"
    return "ar" if ar_words else "en"


def arabic_share(text: str) -> float:
    ar = len(ARABIC_RE.findall(text))
    la = len(LATIN_RE.findall(text))
    return ar / (ar + la) if (ar + la) else 0.0


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))
