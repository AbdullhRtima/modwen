"""Voice activity detection: split audio into utterances of at most ~20 s.

Uses Silero VAD (bundled with faster-whisper, ONNX, no PyTorch) when available, otherwise a simple
energy-based detector. Feeding Whisper speech only is the main defence against hallucinations.
"""

from __future__ import annotations

import logging

import numpy as np

from modwen.audio import SAMPLE_RATE

log = logging.getLogger(__name__)


def _silero(audio: np.ndarray, max_seconds: float, min_silence_ms: int) -> list[tuple[int, int]] | None:
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:
        return None
    try:
        opts = VadOptions(
            threshold=0.5,
            min_speech_duration_ms=250,
            max_speech_duration_s=max_seconds,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=200,
        )
        ts = get_speech_timestamps(audio, opts, sampling_rate=SAMPLE_RATE)
        return [(int(t["start"]), int(t["end"])) for t in ts]
    except Exception as exc:  # model file missing, onnxruntime issue...
        log.warning("Silero VAD failed (%s); using energy VAD", exc)
        return None


def energy_regions(
    audio: np.ndarray,
    max_seconds: float = 20.0,
    min_silence_ms: int = 500,
    frame_ms: int = 30,
    threshold_db: float | None = None,
) -> list[tuple[int, int]]:
    if audio.size == 0:
        return []
    frame = int(SAMPLE_RATE * frame_ms / 1000)
    n = audio.size // frame
    if n == 0:
        return []
    frames = audio[: n * frame].reshape(n, frame)
    # frame energies block by block: squaring the whole meeting at once would copy it (~0.5 GB for 2 hours)
    rms = np.empty(n, dtype=np.float32)
    step = max(1, VAD_BLOCK // frame)
    for i in range(0, n, step):
        blk = frames[i:i + step]
        rms[i:i + step] = np.sqrt(np.einsum("ij,ij->i", blk, blk) / frame + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    if threshold_db is None:
        peak = float(np.percentile(db, 95))
        if peak < -55.0:  # effectively silent
            return []
        noise = float(np.percentile(db, 20))
        # Above the noise floor, but never above "loud" (continuous speech has no silent floor to measure).
        threshold_db = min(max(noise + 10.0, -50.0), peak - 10.0)
    speech = db > threshold_db
    min_sil = max(1, min_silence_ms // frame_ms)
    max_len = int(max_seconds * 1000 / frame_ms)
    regions: list[tuple[int, int]] = []
    start = None
    silence = 0
    for i, s in enumerate(speech):
        if s:
            if start is None:
                start = i
            silence = 0
        elif start is not None:
            silence += 1
            if silence >= min_sil:
                regions.append((start, i - silence + 1))
                start, silence = None, 0
        if start is not None and i - start >= max_len:
            regions.append((start, i + 1))
            start, silence = None, 0
    if start is not None:
        regions.append((start, n))
    pad = int(0.15 * SAMPLE_RATE)
    out = []
    for a, b in regions:
        if (b - a) * frame_ms < 250:
            continue
        out.append((max(0, a * frame - pad), min(audio.size, b * frame + pad)))
    return out


def merge_regions(regions: list[tuple[int, int]], max_gap_s: float = 1.5,
                  max_len_s: float = 20.0) -> list[tuple[int, int]]:
    """Join neighbouring speech regions separated by short pauses into windows of up to ``max_len_s``.

    Whisper is far more accurate (especially on Arabic dialects) with a full sentence or two of context
    than with 1-second fragments; it handles the short pauses inside a window itself.
    """
    if not regions:
        return []
    gap, limit = int(max_gap_s * SAMPLE_RATE), int(max_len_s * SAMPLE_RATE)
    merged = [regions[0]]
    for a, b in regions[1:]:
        ma, mb = merged[-1]
        if a - mb <= gap and b - ma <= limit:
            merged[-1] = (ma, b)
        else:
            merged.append((a, b))
    return merged


VAD_BLOCK = 600 * SAMPLE_RATE


def speech_regions(audio: np.ndarray, max_seconds: float = 20.0, min_silence_ms: int = 500,
                   method: str = "auto", merge_gap_s: float = 1.5) -> list[tuple[int, int]]:
    """Return (start_sample, end_sample) windows of speech, each at most ``max_seconds`` long.

    ``method``: "auto" (Silero if available, else energy), "silero" or "energy".
    Short pauses (< ``merge_gap_s``) are kept inside a window so Whisper sees whole sentences.
    """
    if audio.size < SAMPLE_RATE // 4:
        return []
    regions = None
    if method in ("auto", "silero"):
        # In 10-minute blocks: the detector copies its input twice, which for a whole 2-hour meeting is ~1 GB.
        # Speech cut at a block edge is joined again by merge_regions below (no gap between the two halves).
        regions = []
        for start in range(0, audio.size, VAD_BLOCK):
            part = _silero(audio[start:start + VAD_BLOCK], max_seconds, min_silence_ms)
            if part is None:
                regions = None
                break
            regions += [(a + start, b + start) for a, b in part]
    if regions is None:
        regions = energy_regions(audio, max_seconds, min_silence_ms)
    regions = merge_regions(regions, max_gap_s=merge_gap_s, max_len_s=max_seconds)
    # Hard cap the length in case the detector returned something longer.
    capped: list[tuple[int, int]] = []
    limit = int(max_seconds * SAMPLE_RATE)
    for a, b in regions:
        while b - a > limit:
            capped.append((a, a + limit))
            a += limit
        if b - a > SAMPLE_RATE // 5:
            capped.append((a, b))
    return capped


def rms_level(frames: np.ndarray) -> float:
    """0..1 level for UI meters."""
    if frames.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(frames, dtype=np.float32))))
    db = 20 * np.log10(rms + 1e-9)
    return float(np.clip((db + 60) / 60, 0, 1))
