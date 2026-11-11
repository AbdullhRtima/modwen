"""Deeper analysis of the synthetic evaluation harness's own output (item 001, task 29).

This is a SEPARATE, NEW analysis file. It does not edit, replace, or duplicate the logic in
``tests/test_live_names_synthetic_eval.py`` (report 26's harness) -- per this task's explicit
restriction, that file is read-only here. This module:

1. Re-runs the harness's own corpus-building fixtures (imported, not copied) to reproduce report
   26/27's two headline numbers (today's placeholder-threshold correct rate, and the best grid
   point's correct/wrong-of-named rate) as an independent check.
2. Derives a false-acceptance / false-rejection / unknown-speaker-rejection breakdown PURELY from
   the ``Counts`` objects ``scripts/live_names_eval.py::tally()`` already produces -- no additional
   identification calls are made for this breakdown. ``Counts`` already separately tracks
   ``g_wrong`` (an enrolled person named as a DIFFERENT enrolled person -- false acceptance),
   ``g_maybe_right``/``g_maybe_wrong``/``g_none`` (an enrolled person who should be named but isn't
   -- false rejection), and ``i_named``/``i_maybe``/``i_none`` (never-enrolled people -- unknown-
   speaker rejection). The existing harness's own ``rates()`` collapses some of these into a single
   ``wrong_of_named``/``correct`` pair; this module does not change ``rates()``, it reads the
   ``Counts`` fields directly and prints the finer breakdown ``rates()`` does not expose.
3. Walks a representative subset of ``grid()``'s threshold/margin grid (already built by the
   harness's reused ``sweep()``) and prints the breakdown per point, to show the actual trade-off
   surface, not just the one "best" point ``pick_best()`` picks.
4. Extends report 26/27's one-rival latency measurement to two further concurrency levels (2 and 3
   simultaneous rival embed calls against the same single-worker process pool,
   ``modwen/diarize.py``'s ``_POOL = ProcessPoolExecutor(max_workers=1, ...)``), to see whether
   latency degrades linearly, superlinearly, or plateaus as load increases. This DOES require new
   embed calls (there is no way to derive contention behavior from the existing trial data), so it
   is the one place in this file that is not pure re-analysis.

Needs the real model files on disk, exactly like the harness it reads from, so this module is also
marked ``slow`` (project convention, ``pyproject.toml``'s ``addopts = "-m 'not slow'"``).
Run explicitly with: ``uv run pytest tests/test_live_names_synthetic_eval_threshold_analysis.py -v -s -m slow``

**Every number this module prints is a synthetic-data result, not a real-world accuracy claim** --
see this module's own "Not done / uncertain" discussion in the task-29 report, and the identical
caveat already stated in ``test_live_names_synthetic_eval.py``'s docstring. Multi-harmonic synthetic
tones are a different, much simpler separation problem than real human voices: no real timbre
overlap between similar-sounding people, no real room noise or codec artifacts, no accent or
sibling-voice confusability, no natural pitch/prosody variation within one "voice". False-
acceptance / false-rejection rates measured here are NOT predictive of real-world rates.
"""

from __future__ import annotations

import threading
import time

import pytest

from modwen.audio import SAMPLE_RATE
from modwen.pipeline.live_names import MAX_SPEECH_S, MIN_SPEECH_S
from modwen.voiceprints import LiveThresholds
from scripts.live_names_eval import (
    Counts,
    build_trials,
    grid,
    latency,
    pick_best,
    rates,
    sweep,
    tally,
)

# Reuse report 26's fixtures and helpers by import -- never copy them.
from tests.test_live_names_synthetic_eval import (
    _synthetic_voice_audio,
    corpus,  # noqa: F401 -- imported so pytest picks it up as a fixture in THIS module too
    real_embedder,  # noqa: F401 -- same
)

pytestmark = pytest.mark.slow  # needs the real model files on disk; deselected by default


# ----- item 2: correct / false-accept / false-reject / unknown-rejection, derived from Counts only --

def breakdown(c: Counts) -> dict:
    """The finer breakdown task 29 asks for, computed ONLY from fields ``Counts`` already has.

    No identification call is made here -- ``c`` is whatever ``tally()`` (report 26's own, reused
    unmodified) already produced over the already-built trial set.

    - correct_rate: enrolled person, named, correct identity (``g_correct / g_n``).
    - false_accept_rate: enrolled person, named, but as a DIFFERENT enrolled person (``g_wrong /
      g_n``) -- the dangerous case: a wrong identity asserted with confidence. This is NOT the same
      denominator as ``rates()``'s ``wrong_of_named`` (which divides by ``named``, mixing in impostor
      trials); here it is a rate over genuine trials only, so it answers "of people who ARE enrolled,
      how often do we confidently name the wrong one".
    - false_reject_rate: enrolled person who should be named but comes back uncertain ("maybe") or
      unnamed ("other") -- safe but less useful (``(g_maybe_right + g_maybe_wrong + g_none) / g_n``).
    - unknown_rejection_rate: of never-enrolled ("impostor") trials, the fraction that correctly stay
      unnamed (maybe or none, i.e. NOT matched to an enrolled person) -- ``(i_maybe + i_none) / i_n``.
      Its complement, ``unknown_false_match_rate`` (``i_named / i_n``), is the fraction of unknown
      speakers incorrectly matched to an enrolled person -- the other safety-critical number task 29
      asks for, reported alongside for symmetry.
    """
    g_n, i_n = c.g_n, c.i_n
    return {
        "correct_rate": c.g_correct / g_n if g_n else None,
        "false_accept_rate": c.g_wrong / g_n if g_n else None,
        "false_reject_rate": (c.g_maybe_right + c.g_maybe_wrong + c.g_none) / g_n if g_n else None,
        "unknown_rejection_rate": (c.i_maybe + c.i_none) / i_n if i_n else None,
        "unknown_false_match_rate": c.i_named / i_n if i_n else None,
        "g_n": g_n, "i_n": i_n,
    }


def _fmt_pct(x: float | None) -> str:
    return "   - " if x is None else f"{100 * x:5.2f}%"


def _print_breakdown(label: str, b: dict) -> None:
    print(f"\n[SYNTHETIC DATA ONLY] {label}: n_genuine={b['g_n']} n_impostor={b['i_n']} | "
          f"correct={_fmt_pct(b['correct_rate'])} false_accept={_fmt_pct(b['false_accept_rate'])} "
          f"false_reject={_fmt_pct(b['false_reject_rate'])} "
          f"unknown_rejected={_fmt_pct(b['unknown_rejection_rate'])} "
          f"unknown_false_match={_fmt_pct(b['unknown_false_match_rate'])}")


def test_reproduce_baselines(corpus):  # noqa: F811 -- `corpus` re-imported above so pytest finds it as a fixture in this module
    """Item 1: reproduce report 26/27's two headline numbers, independently, on this run.

    Report 26's run and report 27's independent re-run both got: placeholder-threshold correct =
    0.271 (27.1%), best-grid-point correct = 0.682 (68.2%) with wrong_of_named = 0.0076 (0.76%).
    This test asserts those numbers reproduce here too (within a small float tolerance -- the corpus
    is deterministically seeded, so these should match to several decimal places, not just "close").
    """
    for group_name, data in corpus.items():
        trials = build_trials(data["utterances"], data["samples"])
        genuine = [t for t in trials if t.genuine]
        placeholder = rates(tally(genuine, LiveThresholds()))
        best = pick_best(sweep(trials, grid()))
        print(f"\n[REPRODUCE] {group_name}: placeholder correct={placeholder['correct']:.4f} "
              f"(report 26/27: 0.271) | best correct={best['correct']:.4f} "
              f"wrong_of_named={best['wrong_of_named']:.4f} (report 26/27: 0.682 / 0.0076)")
        assert placeholder["correct"] == pytest.approx(0.271, abs=0.01)
        assert best["correct"] == pytest.approx(0.682, abs=0.01)
        assert best["wrong_of_named"] == pytest.approx(0.0076, abs=0.01)


def test_breakdown_at_placeholder_and_best(corpus):  # noqa: F811 -- see note above `corpus`'s import
    """Item 2: correct / false-accept / false-reject / unknown-rejection, separately, at both the
    placeholder thresholds and the best grid point -- derived only from Counts, no new embed calls."""
    for group_name, data in corpus.items():
        trials = build_trials(data["utterances"], data["samples"])
        placeholder_c = tally(trials, LiveThresholds())
        _print_breakdown(f"{group_name} @ placeholder thresholds", breakdown(placeholder_c))

        best_row = pick_best(sweep(trials, grid()))
        assert best_row is not None
        best_th = LiveThresholds(**best_row["th"])
        best_c = tally(trials, best_th)
        _print_breakdown(f"{group_name} @ best grid point {best_row['th']}", breakdown(best_c))

        # Sanity: false_accept + false_reject + correct must account for all genuine trials.
        bd = breakdown(best_c)
        assert bd["correct_rate"] + bd["false_accept_rate"] + bd["false_reject_rate"] == pytest.approx(1.0, abs=1e-9)
        assert bd["unknown_rejection_rate"] + bd["unknown_false_match_rate"] == pytest.approx(1.0, abs=1e-9)


# ----- item 3: threshold trade-off surface -----------------------------------------------------------

def test_threshold_tradeoff_surface(corpus):  # noqa: F811 -- see note above `corpus`'s import
    """Item 3: how correct / false-accept / false-reject / unknown-rejection each move across the
    grid -- a representative subset, not the full ~128-point grid, to keep the printed table legible.
    Picks one representative point per distinct `named` threshold value in grid(), holding margin/gap
    at the grid's middle values, so the table shows the effect of tightening/loosening the NAMED
    threshold specifically (the axis report 18/24 flagged as the main accuracy/safety lever)."""
    all_rows = grid()
    named_values = sorted({th.named for th in all_rows})
    # one representative threshold per distinct `named` value (first match at that `named` value)
    representative = []
    seen = set()
    for th in all_rows:
        if th.named not in seen and th.named_margin == 0.10:  # hold margin at the grid's middle value
            representative.append(th)
            seen.add(th.named)
    representative.sort(key=lambda th: th.named)

    for group_name, data in corpus.items():
        trials = build_trials(data["utterances"], data["samples"])
        print(f"\n[SYNTHETIC DATA ONLY] {group_name}: threshold trade-off surface "
              f"(named_margin held at 0.10, maybe/maybe_margin following grid()'s own pairing)")
        print(f"{'named':>6} | {'correct':>8} {'false_accept':>13} {'false_reject':>13} | "
              f"{'unknown_rej':>11} {'unknown_false_match':>19}")
        for th in representative:
            c = tally(trials, th)
            b = breakdown(c)
            print(f"{th.named:>6.2f} | {_fmt_pct(b['correct_rate']):>8} "
                  f"{_fmt_pct(b['false_accept_rate']):>13} {_fmt_pct(b['false_reject_rate']):>13} | "
                  f"{_fmt_pct(b['unknown_rejection_rate']):>11} "
                  f"{_fmt_pct(b['unknown_false_match_rate']):>19}")
        assert len(representative) == len(named_values)


# ----- item 4: latency under increasing concurrent load ----------------------------------------------

def _rival_loop(embedder, audio, stop: threading.Event) -> None:
    dur = audio.size / SAMPLE_RATE
    while not stop.is_set():
        try:
            embedder.embed(audio, [(0.0, dur)], min_seconds=MIN_SPEECH_S, min_piece=1.0, max_seconds=MAX_SPEECH_S)
        except Exception:
            pass


def _measure_under_rivals(real_embedder, n_rivals: int, n: int = 30) -> dict:  # noqa: F811 -- see note above `real_embedder`'s import
    """p50/p95 of ``n`` direct embed() calls on a "subject" voice while ``n_rivals`` other threads
    continuously call embed() on a different voice, all contending for the same single-worker
    process pool (``modwen/diarize.py``'s ``_POOL = ProcessPoolExecutor(max_workers=1, ...)``)."""
    subject_audio = _synthetic_voice_audio(8001, 0, n_utterances=n)
    rival_audio = _synthetic_voice_audio(8002, 0, n_utterances=1)[0]
    stop = threading.Event()
    rivals = []
    for _ in range(n_rivals):
        th = threading.Thread(target=_rival_loop, args=(real_embedder, rival_audio, stop), daemon=True)
        th.start()
        rivals.append(th)
    time.sleep(0.05)  # let rivals actually start contending before the subject's first call
    ms = []
    for a in subject_audio:
        dur = a.size / SAMPLE_RATE
        t0 = time.perf_counter()
        real_embedder.embed(a, [(0.0, dur)], min_seconds=MIN_SPEECH_S, min_piece=1.0, max_seconds=MAX_SPEECH_S)
        ms.append((time.perf_counter() - t0) * 1000)
    stop.set()
    for th in rivals:
        th.join(5)
    return latency(ms)


def test_latency_under_increasing_rival_load(real_embedder):  # noqa: F811 -- see note above `real_embedder`'s import
    """Item 4: extend report 26/27's one-rival data point (p50=103.34ms/p95=117.42ms) to 0/1/2/3
    simultaneous rival embed calls, to see whether latency degrades linearly, superlinearly, or
    plateaus. All contend for the SAME single-worker process pool (max_workers=1), so the a-priori
    expectation is roughly linear growth with queue depth (each extra rival call waits behind the
    others on the one worker) -- this test measures whether that expectation holds."""
    results = {}
    for n_rivals in (0, 1, 2, 3):
        results[n_rivals] = _measure_under_rivals(real_embedder, n_rivals)
        r = results[n_rivals]
        print(f"\n[SYNTHETIC DATA ONLY, Tier F CPU-only, n={r['n']}] {n_rivals} rival embed call(s): "
              f"p50={r['p50_ms']:.2f}ms p95={r['p95_ms']:.2f}ms")
    base = results[0]["p50_ms"]
    for n_rivals in (1, 2, 3):
        r = results[n_rivals]
        ratio = r["p50_ms"] / base if base else None
        print(f"[SYNTHETIC DATA ONLY] {n_rivals} rival(s): p50 is {ratio:.2f}x the idle (0-rival) p50"
              if ratio is not None else f"[SYNTHETIC DATA ONLY] {n_rivals} rival(s): idle p50 was ~0, ratio undefined")
    assert all(results[n]["n"] >= 25 for n in results)  # most calls completed at every load level
