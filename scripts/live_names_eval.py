"""Calibrate LIVE speaker names (thresholds and margins) on your own meetings. Reads only; changes nothing.

Only people with a recorded consent (``persons.consent_at``, i.e. a consented voiceprint) are used; the number
of linked people left out for lack of it is printed as a count. The database is opened read-only (sqlite
``mode=ro``: no WAL change, no migration, no new file) and no key is ever created: with no master key it stops.

Every consented speaker you linked to a person (named in the Speakers drawer) on the system-audio channel (online
meetings) or the Room channel (in-person meetings) is replayed line by line, as if each line were a live
utterance: same tail trimming, minimum speech, length cap and embedding call as the live identifier
(``modwen.pipeline.live_names``), scored with the real ``classify_live`` against voiceprints built from that
person's OTHER meetings (leave one meeting out). Each line is also scored as an impostor, against the same
voiceprints with that person removed (a voice nobody saved).

Privacy: the output is aggregate numbers only (counts, rates, timings). No names, titles, ids, text, vectors or
audio are printed. Nothing is written unless you pass ``--out FILE``, which gets the same aggregates as JSON.

    uv run python scripts/live_names_eval.py [--max-lines 60] [--out results.json]

Latency is the embedding call on this machine with nothing else running; the live app also shares the CPU
with Whisper, so treat it as a best case.
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from modwen.crypto import DATA_KEY_AAD, KeyStore, decrypt
from modwen.storage import Store
from modwen.voiceprints import LiveThresholds, classify_live

MAX_WRONG_RATE = 0.02  # the brief's bar: wrong names among confident ("named") captions
MIN_NAMED_FOR_BAR = 50  # fewer confident captions than this cannot show a 2% rate
NAMED_GRID = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
MARGIN_GRID = (0.05, 0.10, 0.15, 0.20)
MAYBE_GAPS = (0.10, 0.15)  # "Name?" starts this far below the named threshold
# The user's own voice (the ``is_self`` person) is used when a saved self voiceprint exists, even though the app never
# sets ``consent_at`` for it ("Remember my voice" is the user's own act). The CEO has NOT yet confirmed this rule.
# Set it to False to leave the user's own voice out (the Room results then lack the user, so look better than real).
ALLOW_SELF_WITHOUT_CONSENT = True


@dataclass(frozen=True)
class Trial:
    """One replayed utterance: its voice vector, the audio kind, the true person and the saved voices to match."""

    vec: np.ndarray
    kind: str
    pid: str
    genuine: bool  # True: the person IS saved in `bank`; False: they are not (impostor)
    bank: dict


@dataclass
class Counts:
    g_n: int = 0  # genuine trials
    g_correct: int = 0  # named as the right person
    g_maybe_right: int = 0  # "Name?" with the right person
    g_maybe_wrong: int = 0  # "Name?" with another person
    g_wrong: int = 0  # named as another person
    g_none: int = 0
    i_n: int = 0  # impostor trials
    i_named: int = 0  # a saved name given to someone not saved (wrong)
    i_maybe: int = 0
    i_none: int = 0

    @property
    def named(self) -> int:
        return self.g_correct + self.g_wrong + self.i_named

    @property
    def wrong(self) -> int:
        return self.g_wrong + self.i_named


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


def _rate(a: int, b: int) -> float | None:
    return a / b if b else None


def leave_one_out_bank(samples: list[tuple], mid: str, drop_pid: str | None = None) -> dict:
    """person id -> kind -> unit mean voice, from every sample NOT in meeting ``mid`` (and not ``drop_pid``'s)."""
    acc: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for mid2, pid2, kind2, vec2 in samples:
        if mid2 != mid and pid2 != drop_pid:
            acc[pid2][kind2].append(vec2)
    return {p: {k: _unit(np.mean(vs, axis=0)) for k, vs in ks.items()} for p, ks in acc.items()}


def build_trials(utterances: list[tuple], samples: list[tuple]) -> list[Trial]:
    """Turn replayed utterances (mid, pid, kind, vec) into trials against leave-one-meeting-out voiceprints.

    The person is saved elsewhere: a genuine trial against the full bank. Always also an impostor trial against
    the bank without that person (empty bank: no trial, there would be nobody to give a name)."""
    trials = []
    for mid, pid, kind, vec in utterances:
        full = leave_one_out_bank(samples, mid)
        if pid in full:
            trials.append(Trial(vec, kind, pid, True, full))
        others = {p: k for p, k in full.items() if p != pid}
        if others:
            trials.append(Trial(vec, kind, pid, False, others))
    return trials


def tally(trials: list[Trial], th: LiveThresholds) -> Counts:
    c = Counts()
    for t in trials:
        got, band = classify_live(t.vec, t.bank, t.kind, th)
        if t.genuine:
            c.g_n += 1
            if band == "named":
                c.g_correct += got == t.pid
                c.g_wrong += got != t.pid
            elif band == "maybe":
                c.g_maybe_right += got == t.pid
                c.g_maybe_wrong += got != t.pid
            else:
                c.g_none += 1
        else:
            c.i_n += 1
            c.i_named += band == "named"
            c.i_maybe += band == "maybe"
            c.i_none += band == "other"
    return c


def rates(c: Counts) -> dict:
    """The numbers to read. ``correct`` and the g_* rates are over genuine trials, ``imp_*`` over impostor
    trials, ``wrong_of_named`` is the bar (wrong names among all confident names)."""
    return {
        "genuine": c.g_n, "impostor": c.i_n, "named_captions": c.named,
        "correct": _rate(c.g_correct, c.g_n),
        "wrong": _rate(c.g_wrong, c.g_n),
        "maybe": _rate(c.g_maybe_right + c.g_maybe_wrong, c.g_n),
        "maybe_right": _rate(c.g_maybe_right, c.g_n),
        "none": _rate(c.g_none, c.g_n),
        "imp_named": _rate(c.i_named, c.i_n),
        "imp_maybe": _rate(c.i_maybe, c.i_n),
        "imp_none": _rate(c.i_none, c.i_n),
        "wrong_of_named": _rate(c.wrong, c.named),
        "wrong_per_trial": _rate(c.wrong, c.g_n + c.i_n),
    }


def grid(named=NAMED_GRID, margins=MARGIN_GRID, gaps=MAYBE_GAPS) -> list[LiveThresholds]:
    out = []
    for n in named:
        for m in margins:
            for g in gaps:
                out.append(LiveThresholds(named=round(n, 3), named_margin=round(m, 3), maybe=round(n - g, 3),
                                          maybe_margin=round(max(m - 0.05, 0.0), 3)))
    return out


def sweep(trials: list[Trial], thresholds: list[LiveThresholds] | None = None) -> list[dict]:
    rows = []
    for th in thresholds or grid():
        r = rates(tally(trials, th))
        r["th"] = asdict(th)
        rows.append(r)
    return rows


def pick_best(rows: list[dict], max_wrong: float = MAX_WRONG_RATE, min_named: int = MIN_NAMED_FOR_BAR) -> dict | None:
    """Row with wrong-of-named <= ``max_wrong`` and the highest correct-name rate, or None if none qualifies.

    Rows that name fewer than ``min_named`` captions are ignored: with so few, a low rate means nothing.
    Ties go to the stricter (higher) named threshold, then the larger margin."""
    ok = [r for r in rows if r["named_captions"] >= min_named and r["correct"] is not None
          and (r["wrong_of_named"] or 0.0) <= max_wrong]
    if not ok:
        return None
    return max(ok, key=lambda r: (r["correct"], r["th"]["named"], r["th"]["named_margin"]))


def latency(ms: list[float]) -> dict:
    if not ms:
        return {"n": 0, "p50_ms": None, "p95_ms": None}
    return {"n": len(ms), "p50_ms": float(np.percentile(ms, 50)), "p95_ms": float(np.percentile(ms, 95))}


def evaluate(utterances: list[tuple], samples: list[tuple], latencies_ms: list[float]) -> dict:
    trials = build_trials(utterances, samples)
    rows = sweep(trials)
    return {"utterances": len(utterances), "trials": len(trials), "latency": latency(latencies_ms),
            "rows": rows, "best": pick_best(rows)}


# ----- reading the data (collect_from is tested on a synthetic database; collect() needs the real model) -----

@dataclass
class Collected:
    samples: list = field(default_factory=list)  # (mid, pid, kind, vec): a person's whole voice in a meeting
    selves: set = field(default_factory=set)  # person ids of the user themself (dropped online, as live does)
    utterances: dict = field(default_factory=lambda: {"online": [], "in_person": []})
    latencies: dict = field(default_factory=lambda: {"online": [], "in_person": []})
    skipped: dict = field(default_factory=dict)  # reason -> how many people / (person, meeting) pairs were left out


def _fmt(x: float | None, pct: bool = True) -> str:
    return "   -  " if x is None else (f"{100 * x:5.1f}%" if pct else f"{x:6.2f}")


def _print_group(name: str, res: dict) -> None:
    lat = res["latency"]
    print(f"\n=== {name}: {res['utterances']} replayed utterances, {res['trials']} trials ===")
    if not res["trials"]:
        print("Not enough linked speakers with other meetings to test. Link more speakers first.")
        return
    print(f"embedding time: p50 {_fmt(lat['p50_ms'], False)} ms, p95 {_fmt(lat['p95_ms'], False)} ms "
          f"({lat['n']} calls)")
    print("correct/wrong/name?/none: of lines whose person IS saved (other meetings). "
          "imp: of lines whose person is NOT saved.")
    print("wrong-of-named: wrong names among all confident names (the 2% bar).")
    print(f"{'named':>5} {'margin':>6} {'gap':>4} | {'correct':>7} {'wrong':>6} {'name?':>6} {'none':>6} | "
          f"{'imp name':>8} {'imp name?':>9} {'imp none':>8} | {'named n':>7} {'wrong-of-named':>14}")
    for r in res["rows"]:
        th = r["th"]
        print(f"{th['named']:>5.2f} {th['named_margin']:>6.2f} {th['named'] - th['maybe']:>4.2f} | "
              f"{_fmt(r['correct']):>7} {_fmt(r['wrong']):>6} {_fmt(r['maybe']):>6} {_fmt(r['none']):>6} | "
              f"{_fmt(r['imp_named']):>8} {_fmt(r['imp_maybe']):>9} {_fmt(r['imp_none']):>8} | "
              f"{r['named_captions']:>7} {_fmt(r['wrong_of_named']):>14}")
    b = res["best"]
    if b is None:
        print(f"\nNo threshold pair keeps wrong-of-named at {100 * MAX_WRONG_RATE:.0f}% or less "
              f"(with at least {MIN_NAMED_FOR_BAR} confident captions) for {name}.")
    else:
        th = b["th"]
        print(f"\nBest for {name} with wrong-of-named <= {100 * MAX_WRONG_RATE:.0f}%: named {th['named']}, "
              f"margin {th['named_margin']}, 'Name?' from {th['maybe']} with margin {th['maybe_margin']}; "
              f"correct {_fmt(b['correct'])}, wrong-of-named {_fmt(b['wrong_of_named'])}, "
              f"'Name?' {_fmt(b['maybe'])}, none {_fmt(b['none'])}.")


class ReadOnlyStore(Store):
    """The Store's read methods over a connection that cannot write.

    Not the normal ``Store()`` constructor: that turns on WAL, migrates and writes the schema. Here the database
    file is opened with sqlite ``mode=ro`` and ``query_only``, so any write raises instead of happening, and no
    -wal, -shm or journal file is created by this script."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        path = Path(self.db_path).resolve()
        # Modwen keeps the database in WAL mode. Plain mode=ro would create -wal and -shm files when none exist
        # (nothing is running), so then the file is opened immutable: no locks, no files, and nothing to miss
        # because there is no -wal. When a -wal file exists (Modwen is running or crashed), mode=ro reads it
        # and creates nothing new. Closing Modwen first gives the cleanest read.
        has_wal = path.with_name(path.name + "-wal").exists()
        uri = path.as_uri() + ("?mode=ro" if has_wal else "?mode=ro&immutable=1")
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only = ON")


def _voiceprint_rows(store: Store) -> set[tuple[str, str | None]]:
    """(person id, meeting id) of every saved voiceprint right now. Ids only: no vector is read."""
    with store._lock:
        try:
            return {(r[0], r[1]) for r in store._conn.execute("SELECT person_id, meeting_id FROM voiceprints")}
        except sqlite3.Error:
            return set()  # no table: nobody has a saved voice


def consented_people(store: Store) -> tuple[dict[str, dict], dict[str, int]]:
    """(people who may be used by id, counts of the other linked people by the reason they were left out).

    A person is used only with ``consent_at`` set AND a saved voiceprint right now: someone who used "Forget voice"
    has no voiceprint row left and is out, even though ``consent_at`` is never cleared. The user's own person
    (``is_self``) needs only the voiceprint row while ``ALLOW_SELF_WITHOUT_CONSENT`` is on. Each left-out person is
    counted once, under the first reason that applies; nothing but these counts is ever reported."""
    with store._lock:
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(persons)").fetchall()}
        linked = {r[0] for r in store._conn.execute(
            "SELECT DISTINCT person_id FROM speakers WHERE person_id IS NOT NULL")}
    with_voice = {pid for pid, _ in _voiceprint_rows(store)}
    people = {p["id"]: p for p in store.list_persons()}
    ok: dict[str, dict] = {}
    skipped = {"people_no_consent": 0, "people_no_voiceprint": 0}
    for pid in linked:
        p = people.get(pid) or {}
        consented = ("consent_at" in cols and bool(p.get("consent_at"))) or (
            ALLOW_SELF_WITHOUT_CONSENT and bool(p.get("is_self")))
        if not consented:
            skipped["people_no_consent"] += 1
        elif pid not in with_voice:
            skipped["people_no_voiceprint"] += 1
        else:
            ok[pid] = p
    return ok, skipped


def existing_master_key(cfg) -> bytes | None:
    """The master key if one exists, else None. Never creates, stores or migrates a key (``KeyStore.master`` would)."""
    from modwen.crypto import LEGACY_SERVICE, SERVICE, SecretStore, _HANDED, _keystore_mode

    name = KeyStore.MASTER
    sec = SecretStore(cfg)
    val = _HANDED.get(name) if sec.mode in ("auto", "keyring") else None
    if not val and sec.mode in ("auto", "keyring"):
        try:
            import keyring

            val = keyring.get_password(SERVICE, name) or keyring.get_password(LEGACY_SERVICE, name)  # read only
        except Exception:
            val = None
    if not val and _keystore_mode(cfg) != "keyring":
        try:
            val = sec._read_file().get(name)  # a read of the 0600 file; never written here
        except Exception:
            val = None
    return base64.b64decode(val) if val else None


def collect_from(store: Store, master: bytes, embedder, audio_dir: Path, read_channel, max_lines: int) -> Collected:
    """Replay consented people's lines. ``store`` is read-only; ``embedder`` and ``read_channel`` are injected."""
    from modwen.audio import SAMPLE_RATE
    from modwen.pipeline.live_names import MAX_SPEECH_S, MIN_SPEECH_S, trim_tail
    from modwen.voiceprints import MAX_SECONDS, MIN_SECONDS, kind_for, speaker_spans

    persons, skipped = consented_people(store)
    skipped.update(pairs_no_voiceprint_from_meeting=0, pairs_meeting_not_confirmed=0)
    out = Collected(skipped=skipped)
    voice_rows = _voiceprint_rows(store)
    warmed = False
    for m in store.list_meetings(limit=1000):
        linked = [sp for sp in store.get_speakers(m["id"]) if sp.get("person_id") in persons]
        # A (person, meeting) pair is used only if a voiceprint of that person was saved FROM this meeting and the
        # meeting's recording consent was confirmed (imported meetings have none). Counted per pair, never named.
        confirmed = bool((m.get("consent") or {}).get("confirmed"))
        usable = []
        for pid in {sp["person_id"] for sp in linked}:
            if (pid, m["id"]) not in voice_rows:
                skipped["pairs_no_voiceprint_from_meeting"] += 1
            elif not confirmed:
                skipped["pairs_meeting_not_confirmed"] += 1
            else:
                usable.append(pid)
        linked = [sp for sp in linked if sp["person_id"] in usable]
        if not linked:
            continue
        try:
            key = decrypt(master, store.get_wrapped_key(m["id"]), DATA_KEY_AAD)
        except Exception:
            continue
        segments = store.get_segments(m["id"])
        tracks: dict[str, np.ndarray] = {}
        for sp in linked:
            ch = sp.get("channel") or "system"
            group = {"system": "online", "room": "in_person"}.get(ch)
            if group is None:
                continue  # online mic is "You" by channel: never matched by voice
            if persons[sp["person_id"]].get("is_self"):
                out.selves.add(sp["person_id"])
                if group == "online":
                    continue  # live drops the user's own print online
            try:
                if ch not in tracks:
                    tracks[ch] = read_channel(Path(audio_dir) / m["id"], ch, key)
            except Exception:
                continue  # audio already deleted (retention)
            audio = tracks[ch]
            vec, _ = embedder.embed(audio, speaker_spans(segments, sp["id"]), max_seconds=MAX_SECONDS,
                                    min_seconds=MIN_SECONDS)
            if vec is None:
                continue  # too little clean speech to be a voiceprint sample
            out.samples.append((m["id"], sp["person_id"], kind_for(ch), vec))
            lines = [s for s in segments if s.get("speaker_id") == sp["id"] and not s.get("overlap")]
            if max_lines and len(lines) > max_lines:
                lines = [lines[int(i * len(lines) / max_lines)] for i in range(max_lines)]
            for s in lines:
                utt = trim_tail(audio[int(s["start_ms"] / 1000 * SAMPLE_RATE): int(s["end_ms"] / 1000 * SAMPLE_RATE)])
                if utt.size < MIN_SPEECH_S * SAMPLE_RATE:
                    continue  # the live identifier would not embed it either
                utt = np.array(utt[: int(MAX_SPEECH_S * SAMPLE_RATE)])
                dur = utt.size / SAMPLE_RATE
                if not warmed:  # the first call pays the model load: not a live latency
                    embedder.embed(utt, [(0.0, dur)], min_seconds=MIN_SPEECH_S, min_piece=1.0,
                                   max_seconds=MAX_SPEECH_S)
                    warmed = True
                t0 = time.perf_counter()
                v, _ = embedder.embed(utt, [(0.0, dur)], min_seconds=MIN_SPEECH_S, min_piece=1.0,
                                      max_seconds=MAX_SPEECH_S)
                ms = (time.perf_counter() - t0) * 1000
                if v is not None:
                    out.latencies[group].append(ms)
                    out.utterances[group].append((m["id"], sp["person_id"], kind_for(ch), v))
        print(".", end="", file=sys.stderr, flush=True)  # progress only, one dot per meeting
    print(file=sys.stderr)
    return out


def collect(max_lines: int) -> Collected | None:
    from modwen.audio.chunks import read_channel
    from modwen.config import load_config
    from modwen.diarize import SpeakerEmbedder, sherpa_model_paths, sherpa_models_present

    cfg = load_config()
    if not sherpa_models_present(cfg.models_dir):
        print("The voice model is not on disk (nothing is downloaded here). Run a meeting through Modwen first.")
        return None
    if not Path(cfg.db_path).is_file():
        print("There is no Modwen database to read here.")
        return None
    master = existing_master_key(cfg)
    if master is None:
        print("No master key was found, so no recording can be read. Nothing was created or changed; stopping.")
        return None
    store = ReadOnlyStore(cfg.db_path)
    embedder = SpeakerEmbedder(emb_path=str(sherpa_model_paths(cfg.models_dir)[1]), threads=2)  # as live
    try:
        return collect_from(store, master, embedder, cfg.audio_dir, read_channel, max_lines)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max-lines", type=int, default=60,
                    help="at most this many lines per speaker per meeting, spread evenly (0 = all)")
    ap.add_argument("--out", help="also write the aggregate numbers (no names, ids or text) to this JSON file")
    args = ap.parse_args(argv)
    got = collect(args.max_lines)
    if got is None:
        return 1
    if ALLOW_SELF_WITHOUT_CONSENT:
        print("Note: your own voice is used when you saved it (Remember my voice), without a separate consent record. "
              "This is not yet confirmed by you; set ALLOW_SELF_WITHOUT_CONSENT = False in this script to leave it out.")
    print("\nLeft out (counts only):")
    for reason, n in got.skipped.items():
        print(f"  {reason.replace('_', ' ')}: {n}")
    results = {"skipped": got.skipped}
    for group in ("online", "in_person"):
        # One bank for both, as in the app (remote and local prints side by side); online never uses the self print.
        samples = [x for x in got.samples if group == "in_person" or x[1] not in got.selves]
        results[group] = evaluate(got.utterances[group], samples, got.latencies[group])
        results[group]["people"] = len({s[1] for s in samples})
        results[group]["meetings"] = len({s[0] for s in samples})
        print(f"\n{group}: {results[group]['people']} people, {results[group]['meetings']} meetings with audio")
        _print_group(group, results[group])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
        print(f"\nAggregates written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
