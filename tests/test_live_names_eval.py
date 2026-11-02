"""The pure logic of scripts/live_names_eval.py (trials, rates, sweep, best pick), on synthetic vectors only."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

from modwen.voiceprints import LiveThresholds

_spec = importlib.util.spec_from_file_location("live_names_eval", Path(__file__).parents[1] / "scripts" / "live_names_eval.py")
ev = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ev
_spec.loader.exec_module(ev)

DIM = 8


def unit(i: int, tilt: float = 0.0, j: int = 7) -> np.ndarray:
    v = np.zeros(DIM, np.float32)
    v[i] = 1.0
    v[j] += tilt
    return v / np.linalg.norm(v)


def world():
    """Three people (axes 0-2) in meetings m1..m3; each speaks in two meetings, one is a stranger in m1."""
    samples = [("m1", "ann", "remote", unit(0)), ("m2", "ann", "remote", unit(0, 0.1)),
               ("m1", "bob", "remote", unit(1)), ("m2", "bob", "remote", unit(1, 0.1)),
               ("m3", "cy", "remote", unit(2))]
    utts = [("m1", "ann", "remote", unit(0, 0.2)), ("m2", "bob", "remote", unit(1, 0.2)),
            ("m3", "cy", "remote", unit(2, 0.2))]
    return samples, utts


def test_leave_one_out_excludes_meeting_and_person():
    samples, _ = world()
    bank = ev.leave_one_out_bank(samples, "m1")
    assert set(bank) == {"ann", "bob", "cy"}
    assert np.allclose(bank["ann"]["remote"], unit(0, 0.1) / np.linalg.norm(unit(0, 0.1)))
    assert set(ev.leave_one_out_bank(samples, "m1", drop_pid="ann")) == {"bob", "cy"}
    assert "cy" not in ev.leave_one_out_bank(samples, "m3")


def test_build_trials_genuine_and_impostor():
    samples, utts = world()
    trials = ev.build_trials(utts, samples)
    kinds = [(t.pid, t.genuine) for t in trials]
    # cy has no other meeting: only an impostor trial; ann and bob get both
    assert ("cy", True) not in kinds and ("cy", False) in kinds
    assert ("ann", True) in kinds and ("ann", False) in kinds
    for t in trials:
        assert not t.genuine or t.pid in t.bank
        assert t.genuine or t.pid not in t.bank
    ann_g = next(t for t in trials if t.pid == "ann" and t.genuine)
    assert np.allclose(ann_g.bank["ann"]["remote"], unit(0, 0.1) / np.linalg.norm(unit(0, 0.1)))


def test_no_impostor_trial_when_nobody_else_is_saved():
    samples = [("m1", "ann", "remote", unit(0)), ("m2", "ann", "remote", unit(0))]
    assert [t.genuine for t in ev.build_trials([("m1", "ann", "remote", unit(0))], samples)] == [True]


def test_tally_counts_each_outcome():
    bank = {"ann": {"remote": unit(0)}, "bob": {"remote": unit(1)}}
    th = LiveThresholds(named=0.6, named_margin=0.15, maybe=0.45, maybe_margin=0.1)
    trials = [
        ev.Trial(unit(0), "remote", "ann", True, bank),  # named right
        ev.Trial(unit(1), "remote", "ann", True, bank),  # named wrong
        ev.Trial(unit(6), "remote", "ann", True, bank),  # none
        ev.Trial(unit(0), "remote", "zed", False, bank),  # impostor named
        ev.Trial(unit(6), "remote", "zed", False, bank),  # impostor none
    ]
    c = ev.tally(trials, th)
    assert (c.g_n, c.g_correct, c.g_wrong, c.g_none, c.i_n, c.i_named, c.i_none) == (3, 1, 1, 1, 2, 1, 1)
    assert c.named == 3 and c.wrong == 2
    r = ev.rates(c)
    assert r["correct"] == 1 / 3 and r["wrong_of_named"] == 2 / 3 and r["imp_named"] == 0.5


def test_maybe_band_counted_separately():
    bank = {"ann": {"remote": unit(0)}, "bob": {"remote": unit(1)}}
    th = LiveThresholds(named=0.9, named_margin=0.1, maybe=0.5, maybe_margin=0.1)
    v = np.array([0.7, 0, 0, 0, 0, 0, 0, 0.714], np.float32)
    v /= np.linalg.norm(v)
    c = ev.tally([ev.Trial(v, "remote", "ann", True, bank), ev.Trial(v, "remote", "bob", True, bank),
                  ev.Trial(v, "remote", "zed", False, bank)], th)
    assert (c.g_maybe_right, c.g_maybe_wrong, c.i_maybe, c.named) == (1, 1, 1, 0)
    assert ev.rates(c)["wrong_of_named"] is None


def test_rates_empty_is_none_not_error():
    r = ev.rates(ev.Counts())
    assert r["correct"] is None and r["wrong_of_named"] is None


def _row(named, margin, correct, wrong_of_named, named_n=100):
    return {"th": {"named": named, "named_margin": margin}, "correct": correct,
            "wrong_of_named": wrong_of_named, "named_captions": named_n}


def test_pick_best_highest_correct_under_the_bar():
    rows = [_row(0.5, 0.1, 0.9, 0.05), _row(0.6, 0.1, 0.7, 0.02), _row(0.7, 0.1, 0.5, 0.0),
            _row(0.65, 0.1, 0.7, 0.01)]
    assert ev.pick_best(rows)["th"]["named"] == 0.65  # ties on correct: the stricter threshold


def test_pick_best_none_when_nothing_qualifies():
    assert ev.pick_best([_row(0.5, 0.1, 0.9, 0.05)]) is None
    assert ev.pick_best([_row(0.8, 0.2, 0.3, 0.0, named_n=3)]) is None  # too few named to show 2%
    assert ev.pick_best([]) is None


def test_sweep_and_best_on_separable_voices():
    # many people, clearly different voices, a bit of noise: a strict pair should reach 0 wrong
    rng = np.random.default_rng(0)
    dim, people = 32, 6
    centres = np.eye(dim, dtype=np.float32)[:people]

    def noisy(i):
        v = centres[i] + rng.normal(0, 0.05, dim).astype(np.float32)
        return v / np.linalg.norm(v)

    samples = [(f"m{k}", f"p{i}", "remote", noisy(i)) for i in range(people) for k in range(3)]
    utts = [(f"m{k}", f"p{i}", "remote", noisy(i)) for i in range(people) for k in range(3) for _ in range(10)]
    res = ev.evaluate(utts, samples, [100.0, 200.0, 300.0])
    assert res["trials"] == len(utts) * 2 and res["rows"]
    assert res["best"] is not None and res["best"]["correct"] > 0.9
    assert res["best"]["wrong_of_named"] <= 0.02
    assert res["latency"]["p50_ms"] == 200.0 and res["latency"]["n"] == 3
    json.dumps(res)  # aggregates serialise as they are


def test_latency_percentiles_and_empty():
    r = ev.latency(list(range(1, 101)))
    assert r["p50_ms"] == 50.5 and 95 <= r["p95_ms"] <= 96
    assert ev.latency([]) == {"n": 0, "p50_ms": None, "p95_ms": None}


def test_result_holds_no_vectors_or_ids():
    samples, utts = world()
    text = json.dumps(ev.evaluate(utts, samples, [1.0]))
    for secret in ("ann", "bob", "cy", "m1", "m2", "m3"):
        assert f'"{secret}"' not in text


# ----- reading a database: consent, read-only, no key creation (synthetic data and fakes only) --------------

class FakeEmbedder:
    """Stands in for the voice model: one fixed vector per call, nothing is loaded."""

    def embed(self, audio, spans, **kw):
        return unit(0), 2.0


def _synthetic_db(data_dir, forget=(), unconfirmed=(), voice_in=("m1", "m2"), me=None):
    """A closed database: two meetings; ann has consent and a saved voice, bob does not; both linked on 'system'.
    ``forget``: people who consented, then used "Forget voice" (consent_at stays, the voiceprint rows go).
    ``unconfirmed``: meetings without confirmed recording consent (like imports). ``voice_in``: meetings that
    ann's voiceprint was saved from. ``me``: None, or "voice" / "novoice": the user's own person (never has
    consent_at) linked on the 'room' channel, with or without a saved self voiceprint."""
    from modwen.crypto import DATA_KEY_AAD, encrypt, generate_key
    from modwen.storage import Store

    master = generate_key()
    st = Store(data_dir / "ro.db")
    for pid in ("ann", "bob"):
        st.upsert_person({"id": pid, "display_name": pid.title(), "create": True})
    st.record_consent("ann", None)
    for mid in voice_in:
        st.add_voiceprint("ann", "remote", b"not a real vector", 20.0, mid)
    for pid in forget:
        st.delete_voiceprints(pid)
    if me:
        st.upsert_person({"id": "me", "display_name": "Me", "is_self": 1, "create": True})
        if me == "voice":
            st.add_voiceprint("me", "local", b"not a real vector", 20.0, "m1")
    for mid in ("m1", "m2"):
        st.create_meeting(mid, id=mid, wrapped_key=encrypt(master, generate_key(), DATA_KEY_AAD),
                          consent={} if mid in unconfirmed else {"confirmed": True})
        sps = [{"id": "s1", "label": "A", "display_name": "A", "channel": "system", "person_id": "ann"},
               {"id": "s2", "label": "B", "display_name": "B", "channel": "system", "person_id": "bob"}]
        order = ["s1", "s2", "s1", "s2"]
        if me:
            sps.append({"id": "s3", "label": "C", "display_name": "C", "channel": "room", "person_id": "me"})
            order += ["s3", "s3"]
        st.replace_speakers(mid, sps)
        st.replace_segments(mid, [{"id": f"g{i}", "speaker_id": sp, "start_ms": 3000 * i, "end_ms": 3000 * i + 2500,
                                   "text": "x"} for i, sp in enumerate(order)])
    st.close()
    return master


def _fake_audio(meeting_dir, channel, key):
    return np.random.default_rng(1).uniform(-0.3, 0.3, 16000 * 14).astype(np.float32)


def _snapshot(path: Path):
    st = path.stat()
    return path.read_bytes(), st.st_mtime_ns, sorted(p.name for p in path.parent.iterdir())


def test_collect_uses_only_consented_people_and_leaves_the_database_untouched(data_dir):
    master = _synthetic_db(data_dir)
    db = data_dir / "ro.db"
    before = _snapshot(db)
    assert before[2] == ["ro.db"]  # closed cleanly: no -wal / -shm left behind
    store = ev.ReadOnlyStore(db)
    try:
        got = ev.collect_from(store, master, FakeEmbedder(), data_dir / "audio", _fake_audio, max_lines=0)
        assert _snapshot(db) == before  # while still open: nothing written, no -wal, -shm or journal file yet
    finally:
        store.close()
    assert _snapshot(db) == before  # same bytes, same mtime, no -wal, -shm or journal file
    assert {s[1] for s in got.samples} == {"ann"} and {u[1] for u in got.utterances["online"]} == {"ann"}
    assert got.utterances["online"] and got.skipped["people_no_consent"] == 1  # bob: a count only


def test_read_only_store_cannot_write(data_dir):
    import sqlite3

    import pytest

    _synthetic_db(data_dir)
    store = ev.ReadOnlyStore(data_dir / "ro.db")
    with pytest.raises(sqlite3.OperationalError):
        store.record_consent("bob", None)
    store.close()


def test_read_while_modwen_is_running_creates_no_new_file_and_leaves_the_database_bytes(data_dir):
    """A live writer (the app) has a -wal file: the evaluation reads through it and adds nothing."""
    from modwen.storage import Store

    _synthetic_db(data_dir)
    db = data_dir / "ro.db"
    app = Store(db)  # like the running app: WAL on
    app.record_consent("bob", None)  # uncheckpointed changes that only the -wal holds
    app.add_voiceprint("bob", "remote", b"not a real vector", 20.0, "m1")
    names = sorted(p.name for p in data_dir.iterdir())
    assert "ro.db-wal" in names
    main_before = db.read_bytes()
    store = ev.ReadOnlyStore(db)
    ok, skipped = ev.consented_people(store)
    store.close()
    assert set(ok) == {"ann", "bob"} and sum(skipped.values()) == 0  # saw the change in the -wal
    assert sorted(p.name for p in data_dir.iterdir()) == names and db.read_bytes() == main_before
    app.close()


def test_no_master_key_means_stop_and_nothing_is_created(data_dir):
    from modwen.config import Config

    cfg = Config()  # data_dir comes from the fixture's MODWEN_DATA_DIR; the keystore is the file store
    assert cfg.data_dir == data_dir
    assert ev.existing_master_key(cfg) is None
    assert sorted(p.name for p in data_dir.iterdir()) == [], "no key file may be created"
    (data_dir / ".secrets.json").write_text(json.dumps({"master-key": "AAAA"}), encoding="utf-8")
    assert ev.existing_master_key(cfg) == b"\x00\x00\x00"


def test_forgotten_voice_excludes_the_person_even_though_consent_at_stays(data_dir):
    _synthetic_db(data_dir, forget=("ann",))
    store = ev.ReadOnlyStore(data_dir / "ro.db")
    ok, skipped = ev.consented_people(store)
    consent_at = store._conn.execute("SELECT consent_at FROM persons WHERE id = 'ann'").fetchone()[0]
    store.close()
    assert consent_at, "consent_at is never cleared, so it alone must not admit anyone"
    assert ok == {} and skipped == {"people_no_consent": 1, "people_no_voiceprint": 1}  # counts only


def _collect(data_dir, master):
    store = ev.ReadOnlyStore(data_dir / "ro.db")
    try:
        return ev.collect_from(store, master, FakeEmbedder(), data_dir / "audio", _fake_audio, max_lines=0)
    finally:
        store.close()


def test_meeting_without_a_voiceprint_from_it_is_not_used(data_dir):
    master = _synthetic_db(data_dir, voice_in=("m1",))  # ann's voice was saved from m1 only
    got = _collect(data_dir, master)
    assert {s[0] for s in got.samples} == {"m1"}  # m2 predates / lacks the saved voice: not replayed
    assert got.skipped["pairs_no_voiceprint_from_meeting"] == 1


def test_meeting_without_confirmed_recording_consent_is_not_used(data_dir):
    master = _synthetic_db(data_dir, unconfirmed=("m2",))  # an imported meeting stores consent = {}
    got = _collect(data_dir, master)
    assert {s[0] for s in got.samples} == {"m1"}
    assert got.skipped["pairs_meeting_not_confirmed"] == 1


def test_own_voice_is_used_when_a_self_voiceprint_exists_even_without_consent_at(data_dir):
    master = _synthetic_db(data_dir, me="voice")
    got = _collect(data_dir, master)
    assert "me" in {s[1] for s in got.samples}  # in m1, where the self voice was saved
    assert {s[0] for s in got.samples if s[1] == "me"} == {"m1"}


def test_own_voice_is_left_out_without_a_self_voiceprint(data_dir):
    master = _synthetic_db(data_dir, me="novoice")
    got = _collect(data_dir, master)
    assert "me" not in {s[1] for s in got.samples} and got.skipped["people_no_voiceprint"] == 1


def test_own_voice_is_left_out_when_the_switch_is_off(data_dir, monkeypatch):
    master = _synthetic_db(data_dir, me="voice")
    monkeypatch.setattr(ev, "ALLOW_SELF_WITHOUT_CONSENT", False)
    got = _collect(data_dir, master)
    assert "me" not in {s[1] for s in got.samples} and got.skipped["people_no_consent"] == 2  # bob and me


def test_the_self_switch_is_a_single_named_constant_defaulting_on():
    assert ev.ALLOW_SELF_WITHOUT_CONSENT is True
