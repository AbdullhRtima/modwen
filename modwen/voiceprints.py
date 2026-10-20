"""Voiceprints: recognise people across meetings (design §5.1).

A voiceprint is the speaker embedding of someone's clean lines (``SpeakerEmbedder``). It is saved when the user
names a speaker and ticks "Remember this voice" (their consent), encrypted with the master key, and compared
with the speakers of later meetings. Nothing here leaves the machine or reaches the LLM.
"""

from __future__ import annotations

import logging
import re

import numpy as np

from modwen.crypto import decrypt, encrypt

log = logging.getLogger(__name__)

REMOTE, LOCAL = "remote", "local"  # system audio (codec-compressed) vs mic / room
MIN_SECONDS = 10.0  # to SAVE a voiceprint: less clean speech than this is not a reliable reference
# To RECOGNISE someone against a saved voiceprint, less is enough: on real meetings 8 s scored 0.81 for the right
# person and <0.2 for everyone else. Short speakers were never even compared before.
MATCH_MIN_SECONDS = 4.0
# Line by line: a line of at least this long that scores this well for one saved person, clearly ahead of the
# next, is that person's line, whatever cluster the diarizer put it in. Short meetings with two voices in turn
# are often detected as one voice; on a real one, each line went to the right person this way (0.54-0.80 for
# the speaker vs <0.35 for the other).
LINE_MIN_SECONDS = 1.5
LINE_MIN_SCORE = 0.5
LINE_MARGIN = 0.15
MAX_SECONDS = 60.0
GENERIC = re.compile(r"(Person \d+|Speaker \d+|Others|Room)")


def kind_for(channel: str | None) -> str:
    return REMOTE if channel == "system" else LOCAL


def _aad(pid: str) -> bytes:
    return b"modwen-voiceprint:" + pid.encode()


def speaker_spans(segments: list[dict], speaker_id: str) -> list[tuple[float, float]]:
    """(start, end) seconds of a speaker's lines, leaving out overlapping speech (two voices at once)."""
    return [(s["start_ms"] / 1000, s["end_ms"] / 1000) for s in segments
            if s.get("speaker_id") == speaker_id and not s.get("overlap")]


class VoiceBank:
    """Encrypted voiceprints in the Store, decrypted only in memory."""

    def __init__(self, store, keystore, keep: int = 10):
        self.store, self.keystore, self.keep = store, keystore, keep

    def add(self, pid: str, kind: str, vec: np.ndarray, seconds: float, mid: str | None = None,
            sid: str | None = None) -> None:
        blob = encrypt(self.keystore.master, np.asarray(vec, dtype=np.float32).tobytes(), _aad(pid))
        self.store.add_voiceprint(pid, kind, blob, seconds, mid, sid, keep=self.keep)

    def centroids(self) -> dict[str, dict[str, np.ndarray]]:
        """person id -> kind -> mean voiceprint (normalised)."""
        acc: dict[str, dict[str, list[np.ndarray]]] = {}
        for row in self.store.list_voiceprints():
            try:
                vec = np.frombuffer(decrypt(self.keystore.master, row["vector"], _aad(row["person_id"])),
                                    dtype=np.float32)
            except Exception:  # wrong key (restored database) or damaged row: skip it
                log.warning("Could not decrypt a voiceprint of %s", row["person_id"])
                continue
            acc.setdefault(row["person_id"], {}).setdefault(row["kind"], []).append(vec)
        out: dict[str, dict[str, np.ndarray]] = {}
        for pid, kinds in acc.items():
            for kind, vecs in kinds.items():
                if len({v.size for v in vecs}) != 1:
                    continue  # embeddings from a different model: unusable together
                v = np.mean(vecs, axis=0)
                out.setdefault(pid, {})[kind] = v / (np.linalg.norm(v) or 1.0)
        return out

    def empty(self) -> bool:
        return not self.store.voice_summary()


def score(vec: np.ndarray, prints: dict[str, np.ndarray], kind: str) -> float:
    """Cosine similarity to a person's voice, same kind of audio first, else the other kind."""
    ref = prints.get(kind)
    if ref is None:
        ref = next(iter(prints.values()), None)
    if ref is None or ref.size != vec.size:
        return 0.0
    return float(vec @ ref)


def match(candidates: dict[str, tuple[np.ndarray, str]], people: dict[str, dict[str, np.ndarray]], *,
          threshold: float, margin: float, suggest: float,
          banned: set[tuple[str, str]] = frozenset()) -> dict[str, tuple[str, float, str]]:
    """Pair speakers with enrolled people. Returns speaker id -> (person id, score, "named" | "suggested").

    Greedy by score, at most one speaker per person and one person per speaker. The best person must beat
    the runner-up by ``margin``; then the speaker is named at ``threshold`` or more, and only suggested
    between ``suggest`` and ``threshold``. ``banned`` holds (speaker id, person id) pairs the user rejected."""
    table = []
    for sid, (vec, kind) in candidates.items():
        scores = sorted(((score(vec, prints, kind), pid) for pid, prints in people.items()
                         if (sid, pid) not in banned), reverse=True)
        if not scores:
            continue
        best, pid = scores[0]
        runner = scores[1][0] if len(scores) > 1 else -1.0
        if best - runner < margin:
            continue  # two people sound about the same: don't guess, not even as a suggestion
        if best >= threshold:
            table.append((best, sid, pid, "named"))
        elif best >= suggest:
            table.append((best, sid, pid, "suggested"))
    out: dict[str, tuple[str, float, str]] = {}
    used: set[str] = set()
    for best, sid, pid, how in sorted(table, reverse=True):
        if sid in out or pid in used:
            continue
        out[sid] = (pid, round(best, 3), how)
        used.add(pid)
    return out


def enroll(bank: VoiceBank, embedder, audio: dict[str, np.ndarray], segments: list[dict], speaker: dict,
           pid: str, mid: str | None) -> tuple[bool, float]:
    """Save a sample of ``speaker``'s voice for person ``pid``. Returns (saved, seconds of speech used)."""
    channel = speaker.get("channel") or "system"
    track = audio.get(channel)
    if track is None:
        return False, 0.0
    vec, secs = embedder.embed(track, speaker_spans(segments, speaker["id"]), max_seconds=MAX_SECONDS,
                               min_seconds=MIN_SECONDS)
    if vec is None:
        return False, secs
    bank.add(pid, kind_for(channel), vec, secs, mid, speaker["id"])
    return True, secs


def recognise(bank: VoiceBank, embedder, audio: dict[str, np.ndarray], speakers: list[dict],
              segments: list[dict], people: dict[str, dict], *, online: bool, threshold: float, margin: float,
              suggest: float, rejects: list[tuple[str, set[str]]] = ()) -> None:
    """Name the meeting's speakers whose voice matches an enrolled person (updates ``speakers`` in place).

    Names the user typed always win. On an online meeting "You" is the mic channel, so your own voiceprint is
    only used in in-person meetings, where it tells Modwen which voice in the room is you."""
    prints = bank.centroids()
    self_ids = {pid for pid, p in people.items() if p.get("is_self")}
    if online:
        prints = {pid: v for pid, v in prints.items() if pid not in self_ids}
    if not prints:
        return
    lines_of: dict[str, set[str]] = {}
    for s in segments:
        lines_of.setdefault(s.get("speaker_id"), set()).add(s["id"])
    banned = set()
    for pid, seg_ids in rejects:
        for sid, mine in lines_of.items():
            if mine and len(mine & seg_ids) >= 0.5 * len(mine):
                banned.add((sid, pid))
    candidates = {}
    for sp in speakers:
        if sp.get("name_source") == "manual" or sp.get("is_self"):
            continue
        track = audio.get(sp.get("channel") or "system")
        if track is None:
            continue
        vec, _ = embedder.embed(track, speaker_spans(segments, sp["id"]), max_seconds=MAX_SECONDS,
                                min_seconds=MATCH_MIN_SECONDS)
        if vec is not None:
            candidates[sp["id"]] = (vec, kind_for(sp.get("channel")))
    manual = [sp for sp in speakers if sp.get("name_source") == "manual"]
    taken = {sp["person_id"] for sp in manual if sp.get("person_id")}
    taken_names = {sp["display_name"].lower() for sp in manual}
    people_prints = {pid: v for pid, v in prints.items() if pid not in taken and pid in people
                     and people[pid]["display_name"].lower() not in taken_names}
    found = match(candidates, people_prints, threshold=threshold, margin=margin, suggest=suggest, banned=banned)
    by_id = {sp["id"]: sp for sp in speakers}
    for sid, (pid, sc, how) in found.items():
        sp, person = by_id[sid], people.get(pid)
        if person is None:
            continue
        name = person["display_name"]
        if how == "named":
            sp.update(display_name=name, person_id=pid, name_source="voiceprint", confidence=sc, suggestion=None)
            if person.get("is_self"):
                sp["is_self"] = 1
        else:
            sp["suggestion"] = {"person_id": pid, "name": name, "score": sc}
    _rejoin_split_speakers(candidates, people_prints, found, speakers, segments, suggest)
    _identify_lines(embedder, audio, speakers, segments, people, people_prints, rejects, online)


def _identify_lines(embedder, audio, speakers: list[dict], segments: list[dict], people: dict, prints: dict,
                    rejects, online: bool) -> None:
    """Give each clearly identified line to its person (see LINE_MIN_SCORE). Names typed by the user are never
    changed; in online meetings the mic is you by channel, so only the call's lines are checked.

    When a speaker turns out to be a mix (some of its lines belong to someone else), its remaining lines that
    can't be told apart go to an unnamed "Room" / "Not sure" speaker and are flagged ``uncertain``, so the
    review shows a warning instead of guessing "You"."""
    if not prints:
        return
    by_id = {sp["id"]: sp for sp in speakers}
    rejected = {(pid, sid) for pid, ids in rejects for sid in ids}
    verdict: dict[str, str] = {}  # line id -> the person it clearly sounds like
    checked: set[str] = set()
    for s in segments:
        sp = by_id.get(s.get("speaker_id"))
        if sp is None or sp.get("name_source") == "manual" or (online and sp.get("channel") == "mic"):
            continue
        checked.add(s["id"])
        if s.get("overlap") or (s["end_ms"] - s["start_ms"]) / 1000 < LINE_MIN_SECONDS:
            continue
        track = audio.get(sp.get("channel") or "system")
        if track is None:
            continue
        vec, _ = embedder.embed(track, [(s["start_ms"] / 1000, s["end_ms"] / 1000)], max_seconds=MAX_SECONDS,
                                min_seconds=LINE_MIN_SECONDS * 0.8, min_piece=1.0)
        if vec is None:
            continue
        kind = kind_for(sp.get("channel"))
        ranked = sorted(((score(vec, p, kind), pid) for pid, p in prints.items()
                         if pid in people and (pid, s["id"]) not in rejected), reverse=True)
        if not ranked:
            continue
        best, pid = ranked[0]
        runner = ranked[1][0] if len(ranked) > 1 else -1.0
        if best >= LINE_MIN_SCORE and best - runner >= LINE_MARGIN:
            verdict[s["id"]] = pid

    mixed: set[str] = set()
    moved = 0
    for s in segments:
        pid = verdict.get(s["id"])
        sp = by_id.get(s.get("speaker_id"))
        if pid is None or sp is None or sp.get("person_id") == pid:
            continue
        target = _speaker_for(pid, sp.get("channel"), speakers, by_id, people)
        if target is None:
            continue
        mixed.add(sp["id"])
        s["speaker_id"] = target["id"]
        moved += 1

    unsure = None
    for s in segments:
        sp = by_id.get(s.get("speaker_id"))
        if sp is None or sp["id"] not in mixed or s["id"] not in checked:
            continue
        if sp.get("person_id") and verdict.get(s["id"]) == sp["person_id"]:
            continue  # clearly the person this speaker was named after
        if unsure is None:
            room = sp.get("channel") == "room"
            unsure = {"id": _new_speaker_id(speakers), "label": "UNSURE", "display_name": "Room" if room else
                      "Not sure", "channel": sp.get("channel"), "is_self": 0, "name_source": None,
                      "confidence": 0.0, "person_id": None, "suggestion": None}
            speakers.append(unsure)
            by_id[unsure["id"]] = unsure
        s["speaker_id"] = unsure["id"]
        s["uncertain"] = 1
    if moved:
        used = {s.get("speaker_id") for s in segments}
        speakers[:] = [sp for sp in speakers if sp["id"] in used or sp.get("is_self")]
        log.info("Voiceprints moved %d line(s) to the person they sound like", moved)


def _speaker_for(pid: str, channel: str | None, speakers: list[dict], by_id: dict, people: dict) -> dict | None:
    """The speaker that holds ``pid`` on this channel, created (named by voice) if there is none yet."""
    target = next((x for x in speakers if x.get("person_id") == pid and x.get("channel") == channel), None)
    if target is not None:
        return target
    if any(x.get("person_id") == pid and x.get("name_source") == "manual" for x in speakers):
        return None  # that person is already named elsewhere by the user: don't create a second one
    person = people[pid]
    target = {"id": _new_speaker_id(speakers), "label": "VOICE", "display_name": person["display_name"],
              "channel": channel, "is_self": int(bool(person.get("is_self"))), "name_source": "voiceprint",
              "confidence": LINE_MIN_SCORE, "person_id": pid, "suggestion": None}
    speakers.append(target)
    by_id[target["id"]] = target
    return target


def _new_speaker_id(speakers: list[dict]) -> str:
    ids = {sp["id"] for sp in speakers}
    n = len(speakers) + 1
    while f"spk{n}" in ids:
        n += 1
    return f"spk{n}"


def _rejoin_split_speakers(candidates, prints, found, speakers: list[dict], segments: list[dict],
                           floor: float) -> None:
    """The diarizer sometimes splits one person in two (short meetings especially). A second speaker on the same
    channel whose voice is also close to a person just recognised there is that person: merge it in."""
    by_id = {sp["id"]: sp for sp in speakers}
    for sid, (pid, _, how) in list(found.items()):
        if how != "named":
            continue
        target = by_id[sid]
        for other, (vec, kind) in candidates.items():
            if other == sid or other not in by_id or other in found and found[other][2] == "named":
                continue
            if by_id[other].get("channel") != target.get("channel"):
                continue
            if pid in prints and score(vec, prints[pid], kind) >= floor:
                for s in segments:
                    if s.get("speaker_id") == other:
                        s["speaker_id"] = sid
                speakers.remove(by_id.pop(other))
