"""Final-pass transcription of one channel: VAD -> per-utterance language -> Whisper -> filter."""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from modwen.audio import SAMPLE_RATE
from modwen.audio.vad import speech_regions
from modwen.stt.engines import STTEngine
from modwen.stt.hallucination import clean_text, is_hallucination
from modwen.stt.language import choose_language, script_language

log = logging.getLogger(__name__)


def transcribe_channel(
    audio: np.ndarray,
    engine: STTEngine,
    *,
    mode: str = "auto",
    allowed: list[str] | None = None,
    arabic_threshold: float = 0.2,
    prompt: str | None = None,
    max_segment_seconds: float = 20.0,
    extra_hallucinations: list[str] | None = None,
    progress: Callable[[float], None] | None = None,
    vad: str = "auto",
) -> list[dict]:
    """Return segments as dicts with absolute times in ms: start_ms, end_ms, text, language, confidence."""
    regions = speech_regions(audio, max_seconds=max_segment_seconds, method=vad)
    out: list[dict] = []
    for i, (a, b) in enumerate(regions):
        chunk = audio[a:b]
        try:
            probs = engine.detect_language(chunk) if mode == "auto" else None
            lang = choose_language(probs, mode, allowed or ["ar", "en"], arabic_threshold)
            # The bilingual prompt helps Arabic + English; for pure English it is harmless but we skip it.
            segs = engine.transcribe(chunk, lang, prompt if lang == "ar" else None)
        except Exception as exc:
            log.exception("Transcription failed for region %d: %s", i, exc)
            segs = []
        for s in segs:
            text, removed = clean_text(s.text.strip())
            if not text or is_hallucination(text, extra_hallucinations):
                continue
            confidence = float(s.confidence) * (0.5 if removed else 1.0)  # guessed characters -> low confidence
            if confidence < 0.12 or (confidence < 0.2 and len(text.split()) <= 3):
                continue  # a word or two Whisper was guessing at (cross-talk, noise): not worth keeping
            start = a / SAMPLE_RATE + s.start
            end = min(b / SAMPLE_RATE, a / SAMPLE_RATE + max(s.end, s.start + 0.2))
            out.append({
                "start_ms": int(start * 1000),
                "end_ms": int(end * 1000),
                "text": text,
                "language": script_language(text),
                "confidence": round(confidence, 3),
            })
        if progress and regions:
            progress((i + 1) / len(regions))
    if progress and not regions:
        progress(1.0)
    return out
