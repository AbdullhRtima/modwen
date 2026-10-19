"""Who said each line: diarize every channel, keep "You" to your own voice, number everyone else.

Online meetings record two channels. The system channel holds the remote people. The mic channel holds you,
anyone sitting next to you, and (without headphones) the call itself leaking from the speakers. That echo
is transcribed again from the mic, often with different words, so it cannot be caught by comparing text
alone. Here the mic is diarized too: a mic voice that is almost only heard while the remote side is talking
is the echo, and its lines are dropped (the clean copy is in the system channel). Of the remaining mic voices
the one that speaks most is "You"; others are people in the room with you.

Everyone except you is called "Person 1", "Person 2", ... in order of first appearance; the user names them
in the review screen.
"""

from __future__ import annotations

import numpy as np

from modwen.audio import SAMPLE_RATE
from modwen.diarize import Turn
from modwen.pipeline.merge import _overlap, assign_speakers, dedupe_echo, mark_overlaps

# A mic voice heard at least this share of its time while the remote side talks is the call leaking in,
ECHO_VOICE_COVERAGE = 0.7
# ... and so is one that sounds like a remote voice and is heard mostly while the remote side talks.
ECHO_VOICE_MIN_COVERAGE = 0.4
ECHO_VOICE_LIKE_REMOTE = 0.7
# ... and its lines are dropped when at least this share of the line overlaps remote speech.
ECHO_LINE_COVERAGE = 0.5
# A second mic voice this similar to yours is you (diarizers sometimes split one voice in two).
SAME_VOICE = 0.6
# Clusters this small are fragments of a bigger voice, not a person.
MIN_VOICE_SECONDS = 8.0
MIN_VOICE_SHARE = 0.02


def _covered(a0: float, a1: float, spans: list[tuple[float, float]]) -> float:
    """Share of [a0, a1] covered by ``spans`` (assumed not to overlap each other much)."""
    if a1 <= a0:
        return 0.0
    return min(1.0, sum(_overlap(a0, a1, b0, b1) for b0, b1 in spans) / (a1 - a0))


def _totals(turns: list[Turn]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in turns:
        out[t.label] = out.get(t.label, 0.0) + (t.end - t.start)
    return out


def fold_small_voices(turns: list[Turn], min_seconds: float = MIN_VOICE_SECONDS,
                      min_share: float = MIN_VOICE_SHARE) -> list[Turn]:
    """Give the turns of tiny clusters to the nearest turn (in time) of a real voice."""
    totals = _totals(turns)
    limit = max(min_seconds, min_share * sum(totals.values()))
    big = [t for t in turns if totals[t.label] >= limit]
    if not big or len(big) == len(turns):
        return turns
    out = []
    for t in turns:
        if totals[t.label] >= limit:
            out.append(t)
            continue
        mid = (t.start + t.end) / 2
        near = min(big, key=lambda b: 0.0 if b.start <= mid <= b.end else min(abs(b.start - mid), abs(b.end - mid)))
        out.append(Turn(t.start, t.end, near.label))
    return out


def _diarize(diarizer, audio: np.ndarray, *, num: int | None, lo: int | None, hi: int | None) -> list[Turn]:
    turns = diarizer.diarize(audio, lo, hi, num)
    return turns if num else fold_small_voices(turns)


def _prints(diarizer, audio: np.ndarray, turns: list[Turn]) -> dict[str, np.ndarray]:
    if not hasattr(diarizer, "voiceprints") or not turns:
        return {}
    try:
        return diarizer.voiceprints(audio, turns)
    except Exception:
        return {}


def _split_mic(diarizer, audio: np.ndarray, mic: list[dict], remote: list[tuple[float, float]],
               remote_prints: dict[str, np.ndarray]) -> tuple[list[dict], dict[str, str]]:
    """Diarize the mic. Returns the kept mic lines (echo dropped) and a map mic-label -> "you" | "mic:<label>"."""
    turns = _diarize(diarizer, audio, num=None, lo=None, hi=None)
    if not turns:
        return mic, {}
    totals = _totals(turns)
    prints = _prints(diarizer, audio, turns)
    echo = set()
    for lab in totals:
        cov = sum(_covered(t.start, t.end, remote) * (t.end - t.start) for t in turns if t.label == lab) / totals[lab]
        like_remote = max((float(prints[lab] @ v) for v in remote_prints.values()), default=0.0) \
            if lab in prints else 0.0
        if cov >= ECHO_VOICE_COVERAGE or (cov >= ECHO_VOICE_MIN_COVERAGE and like_remote >= ECHO_VOICE_LIKE_REMOTE):
            echo.add(lab)
    local = sorted((lab for lab in totals if lab not in echo), key=lambda lab: -totals[lab])
    who: dict[str, str] = {lab: "you" for lab in echo}  # echo lines that survive (you talking over them) are you
    if local:
        me = local[0]
        who[me] = "you"
        for lab in local[1:]:
            same = me in prints and lab in prints and float(prints[me] @ prints[lab]) >= SAME_VOICE
            who[lab] = "you" if same else f"mic:{lab}"
    assign_speakers(mic, turns)
    kept = []
    for s in mic:
        s0, s1 = s["start_ms"] / 1000, s["end_ms"] / 1000
        if s["speaker_label"] in echo and _covered(s0, s1, remote) >= ECHO_LINE_COVERAGE:
            continue
        kept.append(s)
    return kept, who


def label_speakers(audio: dict[str, np.ndarray], per_channel: dict[str, list[dict]], diarizer, *, online: bool,
                   num_speakers: int | None = None, min_speakers: int | None = None,
                   max_speakers: int | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (speakers, segments) for one meeting. ``num_speakers`` counts everyone, you included."""
    smart = getattr(diarizer, "name", "") != "single"
    per_channel = {ch: list(segs) for ch, segs in per_channel.items()}
    if "mic" in per_channel and "system" in per_channel:
        per_channel["mic"] = dedupe_echo(per_channel["mic"], per_channel["system"])
    mic = per_channel.pop("mic") if online and "mic" in per_channel else None

    # Remote / room channels first: their speech turns tell the mic's echo apart from the people at the mic.
    # With a head count, assume one person at the mic and correct below if the mic says otherwise.
    def remote_count(local: int) -> int | None:
        if not num_speakers:
            return None
        return max(1, num_speakers - local) if mic is not None else num_speakers

    turns_by: dict[str, list[Turn]] = {}
    for ch, segs in per_channel.items():
        if segs:
            turns_by[ch] = _diarize(diarizer, audio[ch], num=remote_count(1), lo=min_speakers, hi=max_speakers)

    segments: list[dict] = []
    if mic is not None:
        sys_turns = turns_by.get("system", [])
        remote = ([(t.start, t.end) for t in sys_turns] if smart and sys_turns else
                  [(s["start_ms"] / 1000, s["end_ms"] / 1000) for s in per_channel.get("system", [])])
        who: dict[str, str] = {}
        if smart and audio.get("mic") is not None and audio["mic"].size >= SAMPLE_RATE:
            remote_prints = _prints(diarizer, audio["system"], sys_turns) if sys_turns else {}
            mic, who = _split_mic(diarizer, audio["mic"], mic, remote, remote_prints)
        for s in mic:
            s["_key"] = who.get(s.get("speaker_label", ""), "you")
            s["channel"] = "mic"
        local = len({s["_key"] for s in mic})
        if num_speakers and local != 1 and "system" in turns_by:
            turns_by["system"] = _diarize(diarizer, audio["system"], num=remote_count(local), lo=None, hi=None)
        segments.extend(mic)

    for ch, segs in per_channel.items():
        if not segs:
            continue
        assign_speakers(segs, turns_by[ch])
        for s in segs:
            s["_key"] = f"{ch}:{s['speaker_label']}"
            s["channel"] = ch
        segments.extend(segs)

    segments.sort(key=lambda s: (s["start_ms"], s["end_ms"]))
    speakers: list[dict] = []
    key_to_id: dict[str, str] = {}
    if any(s["_key"] == "you" for s in segments):
        key_to_id["you"] = "you"
        speakers.append({"id": "you", "label": "SELF", "display_name": "You", "channel": "mic",
                         "is_self": 1, "name_source": "self_channel", "confidence": 1.0})
    for s in segments:
        key = s["_key"]
        if key not in key_to_id:
            n = len(key_to_id) - (1 if "you" in key_to_id else 0) + 1
            key_to_id[key] = f"spk{n}"
            ch, _, label = key.partition(":")
            speakers.append({"id": f"spk{n}", "label": label or key, "display_name": f"Person {n}",
                             "channel": ch, "is_self": 0, "name_source": None, "confidence": 0.0})
        s["speaker_id"] = key_to_id[key]
    for s in segments:
        s.pop("_key", None)
        s.pop("speaker_label", None)
        s["overlap"] = 0
    return speakers, mark_overlaps(segments)


def carry_names(speakers: list[dict], segments: list[dict], previous: dict[str, tuple[str, int]],
                share: float = 0.6) -> None:
    """After re-detection, keep a name the user gave when most of a new speaker's speech had that name."""
    votes: dict[str, dict[str, int]] = {}
    total: dict[str, int] = {}
    for s in segments:
        total[s["speaker_id"]] = total.get(s["speaker_id"], 0) + (s["end_ms"] - s["start_ms"])
        if s["id"] in previous:
            name, ms = previous[s["id"]]
            votes.setdefault(s["speaker_id"], {})[name] = votes.setdefault(s["speaker_id"], {}).get(name, 0) + ms
    taken: set[str] = set()
    ranked = sorted(((ms, sid, name) for sid, v in votes.items() for name, ms in v.items()), reverse=True)
    by_id = {sp["id"]: sp for sp in speakers}
    for ms, sid, name in ranked:
        sp = by_id.get(sid)
        if sp is None or sp.get("name_source") == "manual" or name in taken or ms < share * total.get(sid, 1):
            continue
        sp.update(display_name=name, name_source="manual", confidence=1.0)
        taken.add(name)
