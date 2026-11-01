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
