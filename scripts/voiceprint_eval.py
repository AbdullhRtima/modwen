"""Calibrate voiceprint matching (§5.1) on your own meetings. Reads only; changes nothing.

Every speaker you linked to a person (named in the Speakers drawer) is embedded from its meeting. Each one is
then matched against everyone's voice built from their *other* meetings, as if it were a new meeting, and the
script reports how many would be named right, named wrong, or left unnamed at each threshold.

    uv run python scripts/voiceprint_eval.py
"""

from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from modwen.audio.chunks import read_channel
from modwen.config import load_config
from modwen.crypto import KeyStore
from modwen.diarize import SpeakerEmbedder
from modwen.storage import Store
from modwen.voiceprints import MAX_SECONDS, MIN_SECONDS, kind_for, match, speaker_spans


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


def main() -> int:
    cfg = load_config()
    store, keys = Store(cfg.db_path), KeyStore(cfg)
    embedder = SpeakerEmbedder(cfg.models_dir)
    names = {p["id"]: p["display_name"] for p in store.list_persons()}
    samples = []  # (meeting id, person id, kind, vector)
    for m in store.list_meetings(limit=1000):
        linked = [sp for sp in store.get_speakers(m["id"]) if sp.get("person_id") in names]
        if not linked:
            continue
        try:
            key = keys.unwrap(store.get_wrapped_key(m["id"]))
        except Exception:
            continue
        segments = store.get_segments(m["id"])
        tracks: dict[str, np.ndarray] = {}
        for sp in linked:
            ch = sp.get("channel") or "system"
            try:
                tracks.setdefault(ch, read_channel(cfg.audio_dir / m["id"], ch, key))
            except Exception:
                continue  # audio already deleted (retention)
            vec, _ = embedder.embed(tracks[ch], speaker_spans(segments, sp["id"]), max_seconds=MAX_SECONDS,
                                    min_seconds=MIN_SECONDS)
            if vec is not None:
                samples.append((m["id"], sp["person_id"], kind_for(ch), vec))
    people = {pid for _, pid, _, _ in samples}
    print(f"{len(samples)} named speakers, {len(people)} people, "
          f"{len({mid for mid, *_ in samples})} meetings with audio")
    if len(people) < 2 or len(samples) < 3:
        print("Name (and link) people in a few more meetings first.")
        return 1

    margin, suggest = cfg.diarization.voiceprint_margin, cfg.diarization.voiceprint_suggest
    print(f"\nmargin {margin}; 'right' = named as the right person, 'wrong' = named as someone else")
    print(f"{'threshold':>9} {'right':>6} {'wrong':>6} {'unnamed':>8} {'no voice yet':>13}")
    for threshold in np.arange(0.50, 0.90, 0.05):
        right = wrong = unnamed = cold = 0
        for mid, pid, kind, vec in samples:
            bank: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
            for mid2, pid2, kind2, vec2 in samples:
                if mid2 != mid:  # only what Modwen would have known before this meeting
                    bank[pid2][kind2].append(vec2)
            if pid not in bank:
                cold += 1
                continue
            prints = {p: {k: _unit(np.mean(vs, axis=0)) for k, vs in ks.items()} for p, ks in bank.items()}
            found = match({"x": (vec, kind)}, prints, threshold=float(threshold), margin=margin, suggest=suggest)
            got = found.get("x")
            if not got or got[2] != "named":
                unnamed += 1
            elif got[0] == pid:
                right += 1
            else:
                wrong += 1
        mark = "  <- current" if abs(threshold - cfg.diarization.voiceprint_threshold) < 1e-6 else ""
        print(f"{threshold:>9.2f} {right:>6} {wrong:>6} {unnamed:>8} {cold:>13}{mark}")
    print("\nPick the lowest threshold with 0 wrong, and set diarization.voiceprint_threshold in config.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
