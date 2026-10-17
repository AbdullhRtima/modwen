"""Merge channels into one named timeline: speaker assignment, echo de-duplication, overlap flags."""

from __future__ import annotations

from difflib import SequenceMatcher

from modwen.diarize import Turn


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_speakers(segments: list[dict], turns: list[Turn]) -> list[dict]:
    """Give each segment the diarization label that overlaps it most (nearest turn if none overlaps)."""
    for s in segments:
        s0, s1 = s["start_ms"] / 1000, s["end_ms"] / 1000
        best, best_ov = None, 0.0
        for t in turns:
            ov = _overlap(s0, s1, t.start, t.end)
            if ov > best_ov:
                best, best_ov = t.label, ov
        if best is None and turns:
            mid = (s0 + s1) / 2
            best = min(turns, key=lambda t: min(abs(t.start - mid), abs(t.end - mid))).label
        s["speaker_label"] = best or "SPEAKER_00"
    return segments


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _strip_echo(mic_text: str, sys_text: str, min_chars: int = 10, coverage: float = 0.8) -> str:
    """Remove the part of a mic line that repeats a remote line (speaker -> mic leak)."""
    if len(sys_text.strip()) < min_chars:
        return mic_text
    sm = SequenceMatcher(None, mic_text, sys_text, autojunk=False)
    match = sm.find_longest_match(0, len(mic_text), 0, len(sys_text))
    if match.size >= coverage * len(sys_text.strip()):
        mic_text = mic_text[: match.a] + mic_text[match.a + match.size :]
    return " ".join(mic_text.split())


def dedupe_echo(mic: list[dict], system: list[dict], threshold: float = 0.72) -> list[dict]:
    """Drop mic segments that are just the remote audio leaking into the mic, and cut echoed
    remote sentences out of longer mic lines."""
    kept = []
    for m in mic:
        m0, m1 = m["start_ms"], m["end_ms"]
        echo = False
        for s in system:
            ov = _overlap(m0, m1, s["start_ms"], s["end_ms"])
            if ov <= 0:
                continue
            if ov / max(1, m1 - m0) >= 0.5 and _similar(m["text"], s["text"]) >= threshold:
                echo = True
                break
            m["text"] = _strip_echo(m["text"], s["text"])
        if not echo and len(m["text"].strip()) >= 2:
            kept.append(m)
    return kept


def mark_overlaps(segments: list[dict], min_ms: int = 600) -> list[dict]:
    """Flag segments from different speakers that overlap in time (shown hatched in review)."""
    ordered = sorted(segments, key=lambda s: s["start_ms"])
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b["start_ms"] >= a["end_ms"]:
                break
            if a.get("speaker_id") != b.get("speaker_id") and _overlap(
                a["start_ms"], a["end_ms"], b["start_ms"], b["end_ms"]
            ) >= min_ms:
                a["overlap"] = b["overlap"] = 1
    return ordered
