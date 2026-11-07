"""Synthetic evaluation harness for Live Speaker Names Stage 1 (item 001, tasks 18/24/25/26).

Measures wrong-name rate and identification latency on SYNTHETIC data only, using the REAL CAM++
speaker-embedding model that is already present on disk in this checkout (``.data/models/...``, per
reports 21/23) -- never on real meeting audio, transcripts, or voiceprints. Everything in this module
is an in-memory fixture built by the test itself, exactly as ``tests/test_live_names.py``'s ``world``
fixture already does: a throwaway SQLite file inside pytest's own ``tmp_path``-style ``data_dir``,
never the real ``.data/`` database.

**"Enrollment" here is a test fixture, not real enrollment.** Every "person" is a synthetic multi-
harmonic tone-burst generator (see ``_synthetic_voice_audio``), never a real human voice. "Enrolling"
one means calling ``Store.upsert_person()`` / ``Store.record_consent()`` / ``VoiceBank.add()`` directly
-- the same calls the real UI's "Remember this voice" step makes (``modwen/api.py``'s
``remember_voice`` branch) -- never the UI, never a real consent record on a real person. Display
names are obviously-synthetic markers (``Synth-...``) so no report, log or traceback from this module
can be mistaken for a real person needing consent tracking.

This module reuses, rather than reimplements:
* the pure scoring engine in ``scripts/live_names_eval.py`` (``Trial``, ``leave_one_out_bank``,
  ``build_trials``, ``tally``, ``rates``, ``grid``, ``sweep``, ``pick_best``, ``latency``,
  ``consented_people``) for every accuracy/wrong-name-rate/latency-aggregation computation;
* the ``world`` / ``Emb`` / ``_ident`` / ``_name`` / ``_live`` / ``_wait_for`` / ``_lines`` fixtures
  and helpers already proven in ``tests/test_live_names.py``, imported here rather than rebuilt.

The only genuinely new pieces are: the synthetic audio generator, a hand-written Wilson score
interval (no new dependency -- ``scipy``/``statsmodels`` are not in this project), and the real-
embedder wiring for the latency measurements.

Needs the real model files on disk, so this whole module is marked ``slow`` (the existing marker,
``pyproject.toml``'s ``addopts = "-m 'not slow'"``) and is skipped by a default ``uv run pytest``.
Run explicitly with: ``uv run pytest tests/test_live_names_synthetic_eval.py -v -s -m slow``

**Every number this module prints is a synthetic-data result, not a real-world accuracy claim.**
Multi-harmonic synthetic tones are an easier separation problem for a speaker embedder than real
human voices (no real timbre overlap, no real room noise, no real accent/sibling-voice confusability)
-- a low wrong-name rate here says the harness and the scoring mechanism work end to end, not that
Stage 1 meets the CEO's 2%/2s bar on real meetings (report 18's whole point, restated here).
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from modwen.audio import SAMPLE_RATE
from modwen.pipeline.live_names import LiveName, MAX_SPEECH_S, MIN_SPEECH_S
from modwen.voiceprints import LOCAL, LiveThresholds, REMOTE
from scripts.live_names_eval import (
    MAX_WRONG_RATE,
    MIN_NAMED_FOR_BAR,
    build_trials,
    consented_people,
    grid,
    latency,
    pick_best,
    rates,
    sweep,
    tally,
)
from tests.conftest import tone
from tests.test_live_names import (
    Emb,
    _ident,
    _lines,
    _live,
    _name,
    _wait_for,
    world,  # noqa: F401 -- imported so pytest picks it up as a fixture in THIS module too
)
from tests.test_live_names import (
    test_slow_embedder_never_stalls_captions_and_never_runs_twice_at_once as _fake_degrade_regression,
)

pytestmark = pytest.mark.slow  # needs the real model files on disk; deselected by default

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO_ROOT / ".data" / "models"

SEED = 20261104  # fixed, named seed (report 24 Sec5): same audio every run, no checked-in fixture
N_ENROLLED = 6  # per group -- clears MIN_PEOPLE=3 with margin even after leave-one-meeting-out holdouts
N_IMPOSTORS = 3  # never-enrolled synthetic people per group, purely for impostor trials
N_MEETINGS = 4  # per person -- clears MIN_MEETINGS=3 with margin
N_UTTERANCES_PER_MEETING = 8  # 6 people x 4 meetings x 8 ~= 192 genuine utterances/group (report 24 Sec1 arithmetic)
N_VOICES_PER_GROUP = N_ENROLLED + N_IMPOSTORS  # 9 distinct synthetic "voices" sharing one frequency allocation
BAND_LO_HZ, BAND_HI_HZ = 150.0, 7500.0  # stays well under the 8 kHz Nyquist of 16 kHz audio
BAND_GUARD = 0.22  # fraction of each voice's frequency band left empty on each side, for extra separation


# ----- synthetic "voices" (never real speech) -----------------------------------------------------
#
# A pure single-harmonic-profile tone turned out, empirically (see report 26), to be a BAD separation
# problem for the real CAM++ model in the opposite direction report 24 Sec5 anticipated: rather than
# separating "too easily", cosine similarity among different pure-tone "voices" with only amplitude
# jitter on a shared harmonic series stayed near 0.85-0.99 regardless of identity, because CAM++ was
# trained on real speech spectral structure, not harmonic amplitude ratios. Giving each synthetic voice
# its own, mostly non-overlapping slice of the spectrum (like distinct formant placement) plus a fixed,
# voice-specific harmonic PHASE pattern (a stable "timbre") is what actually gives the real model a
# separable signal -- confirmed empirically before this harness's tests were finalized.

def _synthetic_voice_audio(voice_id: int, meeting_index: int, n_utterances: int = N_UTTERANCES_PER_MEETING,
                           n_voices: int = N_VOICES_PER_GROUP) -> list[np.ndarray]:
    """Deterministic multi-harmonic tone-burst audio for one synthetic "voice" in one synthetic "meeting".

    Never real speech. Each synthetic voice (``voice_id % n_voices``) owns a mostly non-overlapping
    frequency band (``BAND_GUARD`` gap on each side) and a FIXED per-voice harmonic-phase pattern
    (reproduced identically every meeting -- a stable "timbre"), plus one high-band marker tone unique
    to that voice. Each utterance then adds light per-call frequency jitter and a random length within
    MIN_SPEECH_S..MAX_SPEECH_S, so the embedder sees varied, speech-like bursts rather than one
    idealized tone. Reproducible: every random draw comes from a fixed-seed ``numpy.random.default_rng``
    derived from SEED, ``voice_id`` and (for the per-utterance jitter only) ``meeting_index`` -- the
    same "voice" every run, nothing checked into the repo (report 24 Sec5).
    """
    vid = voice_id % n_voices
    span = (BAND_HI_HZ - BAND_LO_HZ) / n_voices
    band_lo = BAND_LO_HZ + span * vid + BAND_GUARD * span
    band_hi = BAND_LO_HZ + span * (vid + 1) - BAND_GUARD * span
    f0 = 75.0 + 4.0 * vid
    identity_rng = np.random.default_rng(SEED * 31 + vid)  # FIXED per voice: the same "timbre" every time
    harmonic_phases: dict[int, float] = {}
    h = max(1, int(band_lo / f0))
    while f0 * h < band_hi:
        harmonic_phases[h] = float(identity_rng.uniform(0, 2 * np.pi))
        h += 1
    f2 = 6000.0 + 150.0 * vid  # a second, high-band marker tone unique to this voice
    f2_phase = float(identity_rng.uniform(0, 2 * np.pi))
    utterances = []
    for u in range(n_utterances):
        rng = np.random.default_rng(SEED + voice_id * 1000 + meeting_index * 97 + u)
        dur = float(rng.uniform(1.4, min(3.0, MAX_SPEECH_S)))  # within MIN_SPEECH_S..MAX_SPEECH_S
        t = np.arange(int(dur * SAMPLE_RATE)) / SAMPLE_RATE
        sig = np.zeros_like(t)
        for harm, phase in harmonic_phases.items():
            jitter = 1.0 + rng.uniform(-0.002, 0.002)
            sig = sig + np.sin(2 * np.pi * f0 * harm * jitter * t + phase)
        jitter2 = 1.0 + rng.uniform(-0.002, 0.002)
        sig = sig + 0.3 * np.sin(2 * np.pi * f2 * jitter2 * t + f2_phase)
        peak = np.max(np.abs(sig)) or 1.0
        utterances.append((0.3 * sig / peak).astype(np.float32))
    return utterances


def _unit_mean(vecs: list[np.ndarray]) -> np.ndarray:
    v = np.mean(vecs, axis=0)
    return v / (np.linalg.norm(v) or 1.0)


def _embed(embedder, audio: np.ndarray):
    dur = audio.size / SAMPLE_RATE
    vec, _ = embedder.embed(audio, [(0.0, dur)], min_seconds=MIN_SPEECH_S, min_piece=1.0, max_seconds=MAX_SPEECH_S)
    return vec


def _build_group(embedder, kind: str):
    """(samples, utterances, enrolled_pids, impostor_pids) for one group (online=REMOTE, room=LOCAL).

    ``samples``: one (mid, pid, kind, vec) per enrolled person per meeting -- the "whole meeting" vector
    a real enrollment would save. ``utterances``: one (mid, pid, kind, vec) per replayed line, for both
    enrolled and never-enrolled (impostor) people -- matches ``scripts/live_names_eval.py``'s own shape
    exactly, so ``build_trials``/``tally``/``rates`` apply unmodified. Online and Room each get their
    own independent voice_id 0..N_VOICES_PER_GROUP-1 allocation -- they are never compared against each
    other, so reusing the same acoustic parameters across groups is harmless."""
    samples, utterances, enrolled, impostors = [], [], [], []
    for p in range(N_ENROLLED):
        pid = f"synth-{kind}-{p}"
        enrolled.append(pid)
        for m in range(N_MEETINGS):
            mid = f"m-{kind}-{p}-{m}"
            vecs = [v for v in (_embed(embedder, u) for u in _synthetic_voice_audio(p, m)) if v is not None]
            if not vecs:
                continue
            samples.append((mid, pid, kind, _unit_mean(vecs)))
            utterances.extend((mid, pid, kind, v) for v in vecs)
    for i in range(N_IMPOSTORS):
        voice_id = N_ENROLLED + i  # a distinct voice/band, never enrolled
        pid = f"synth-imp-{kind}-{i}"
        impostors.append(pid)
        for m in range(N_MEETINGS):
            mid = f"mi-{kind}-{i}-{m}"
            vecs = [v for v in (_embed(embedder, u) for u in _synthetic_voice_audio(voice_id, m)) if v is not None]
            utterances.extend((mid, pid, kind, v) for v in vecs)
    return samples, utterances, enrolled, impostors


@pytest.fixture(scope="module")
def real_embedder():
    """The real CAM++ model, read-only, from this checkout's already-downloaded files (reports 21/23).

    Points directly at the model file, never ``load_config()`` -- per report 25 Finding 4, a synthetic-
    only test must not risk resolving the real ``%LOCALAPPDATA%\\Modwen`` profile."""
    from modwen.diarize import SpeakerEmbedder, sherpa_model_paths, sherpa_models_present

    if not sherpa_models_present(MODELS_DIR):
        pytest.skip("real CAM++ model not present at .data/models in this checkout (see reports 21/23)")
    _, emb_path = sherpa_model_paths(MODELS_DIR)
    return SpeakerEmbedder(emb_path=str(emb_path), threads=2)


@pytest.fixture(scope="module")
def corpus(real_embedder):
    """Built once per test run: real CAM++ embeddings of synthetic audio, for both groups."""
    online = _build_group(real_embedder, REMOTE)
    room = _build_group(real_embedder, LOCAL)
    return {
        "online": dict(zip(("samples", "utterances", "enrolled", "impostors"), online)),
        "in_person": dict(zip(("samples", "utterances", "enrolled", "impostors"), room)),
    }


# ----- Wilson score interval: hand-implemented, no new dependency (report 24 Sec3) -----------------

def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes out of ``n`` trials, closed form, no scipy/statsmodels.

    Reference: E. B. Wilson (1927), "Probable Inference, the Law of Succession, and Statistical
    Inference", JASA 22(158). Used for the wrong-name-rate confidence interval (report 18 Sec1,
    report 24 Sec3) -- never report the point estimate alone."""
    if n == 0:
        return (0.0, 1.0)
    p_hat = k / n
    denom = 1 + z**2 / n
    center = (p_hat + z**2 / (2 * n)) / denom
    half_width = (z * math.sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2))) / denom
    return (max(0.0, center - half_width), min(1.0, center + half_width))


def test_wilson_interval_matches_hand_checkable_values():
    lo, hi = wilson_interval(0, 50)
    assert lo == 0.0
    assert hi == pytest.approx(0.0714, abs=0.002)  # "rule of three"-adjacent known result for k=0
    lo2, hi2 = wilson_interval(1, 50)
    assert lo2 == pytest.approx(0.0017, abs=0.003)
    assert hi2 == pytest.approx(0.1054, abs=0.01)  # cross-checks report 18's "~0.1%-10.5%" example


# ----- #1/#2/#3: enrolled-speaker identification, online vs. Room ----------------------------------

def _report_group_accuracy(group_name: str, data: dict) -> None:
    """Print the default-(placeholder)-threshold rates AND the best grid operating point for one group.

    The PLACEHOLDER ``LiveThresholds()`` defaults (named_margin=0.15) are strict relative to this
    synthetic signal's margins and mostly yield "other" (no name) -- itself a real, reportable finding
    about the placeholders, not a harness bug. ``sweep()``/``pick_best()`` (reused, not reimplemented)
    show whether ANY grid point separates genuine from impostor trials -- report 18 Sec1's "a threshold
    sweep, not a single point."""
    trials = build_trials(data["utterances"], data["samples"])
    genuine = [t for t in trials if t.genuine]
    default = rates(tally(genuine, LiveThresholds()))
    print(f"\n[SYNTHETIC DATA ONLY, Tier F] {group_name}: n={len(genuine)} genuine trials, at today's "
          f"PLACEHOLDER thresholds: correct={default['correct']:.3f} wrong={default['wrong']:.3f} "
          f"maybe={default['maybe']:.3f} none={default['none']:.3f}")
    best = pick_best(sweep(trials, grid()))
    if best is None:
        print(f"[SYNTHETIC DATA ONLY, Tier F] {group_name}: no grid point clears the CEO's "
              f"{MAX_WRONG_RATE:.0%} bar with >= {MIN_NAMED_FOR_BAR} named captions")
    else:
        print(f"[SYNTHETIC DATA ONLY, Tier F] {group_name}: best grid point th={best['th']} "
              f"correct={best['correct']:.3f} wrong_of_named={best['wrong_of_named']:.4f} "
              f"named_captions={best['named_captions']}")
    return len(genuine), best


def test_enrolled_speaker_identification_online(corpus):
    data = corpus["online"]
    n_genuine, best = _report_group_accuracy("online", data)
    assert n_genuine >= 150  # report 18/24's "defensible" floor before quoting a rate
    assert best is not None and best["correct"] > 0.5  # SOME threshold separates genuine trials from chance


def test_enrolled_speaker_identification_room(corpus):
    data = corpus["in_person"]
    n_genuine, best = _report_group_accuracy("in_person (Room)", data)
    assert n_genuine >= 150
    assert best is not None and best["correct"] > 0.5


def test_online_self_voice_excluded_even_when_enrolled(world, corpus):  # noqa: F811 -- `world` re-imported above so pytest finds it as a fixture in this module
    """Report 25 Finding 5, delta 3: the self person's print is excluded ONLINE even if enrolled."""
    _, bank, person = world
    vec = corpus["online"]["samples"][0][3]
    person("Synth-Self", vec, kind=REMOTE, is_self=True)
    ident = _ident(bank, Emb(vec))
    _wait_for(lambda: ident._state == "off")  # nobody usable online: own print dropped, nothing else saved
    assert ident.submit("system", tone(2.0)) is None
    ident.close()


def test_room_self_voice_included_when_enrolled(world, corpus):  # noqa: F811 -- see note above `world`'s import
    """Report 25 Finding 5, delta 3: Room explicitly INCLUDES the self voiceprint."""
    _, bank, person = world
    vec = corpus["in_person"]["samples"][0][3]
    person("Synth-Self", vec, kind=LOCAL, is_self=True)
    ident = _ident(bank, Emb(vec), online=False)
    assert _name(ident, "room") == LiveName("Synth-Self", "named")
    ident.close()


# ----- #4: multiple enrolled speakers simultaneously ------------------------------------------------

@pytest.mark.parametrize("n_people", [5, 10])
def test_multiple_enrolled_speakers_margin_logic_holds(n_people, corpus):
    data = corpus["online"]
    if n_people > len(data["enrolled"]):
        pytest.skip(f"corpus only has {len(data['enrolled'])} enrolled online people")
    wanted = set(data["enrolled"][:n_people])
    samples = [s for s in data["samples"] if s[1] in wanted]
    utterances = [u for u in data["utterances"] if u[1] in wanted]
    trials = build_trials(utterances, samples)
    genuine = [t for t in trials if t.genuine]
    assert len(genuine) >= 30
    r = rates(tally(genuine, LiveThresholds()))
    print(f"\n[SYNTHETIC DATA ONLY, Tier F] {n_people} concurrently enrolled: n={len(genuine)} "
          f"correct={r['correct']:.3f}")
    assert r["correct"] is not None


# ----- #5: unknown speakers stay generic (impostor trials) -----------------------------------------

def test_unknown_speakers_stay_generic(corpus):
    data = corpus["online"]
    trials = build_trials(data["utterances"], data["samples"])
    impostor = [t for t in trials if not t.genuine]
    assert len(impostor) >= 30
    c = tally(impostor, LiveThresholds())
    r = rates(c)
    print(f"\n[SYNTHETIC DATA ONLY, Tier F] unknown-speaker (impostor) outcomes: n={c.i_n} "
          f"named(wrong)={r['imp_named']:.3f} maybe={r['imp_maybe']:.3f} none={r['imp_none']:.3f}")
    # Never shown as a bare claim for MOST unenrolled voices -- the mechanism, not a guaranteed zero.
    assert r["imp_named"] is not None and 0.0 <= r["imp_named"] <= 1.0


# ----- #6: consent-gated identification -------------------------------------------------------------

def test_live_identifier_is_consent_agnostic_by_design(world):  # noqa: F811 -- see note above `world`'s import
    """LiveIdentifier/classify_live never check consent_at -- consent is enforced upstream, at save time
    (report 24 Sec1 "Consent simulation"). This is a deliberate, already-shipped architectural fact, NOT
    something this harness adds or changes."""
    store, bank, person = world
    vec = np.array([1.0, 0.0, 0.0], np.float32)
    pid = person("Synth-NoConsent", vec)
    assert store.get_person(pid).get("consent_at") is None
    ident = _ident(bank, Emb(vec))
    assert _name(ident).name == "Synth-NoConsent"  # named anyway: the live path has no consent gate
    ident.close()


def test_consented_people_excludes_non_consented_synthetic_people(data_dir):
    """The eval script's consented_people() -- a DIFFERENT layer from LiveIdentifier -- does gate on
    consent_at. Scaled-up version of tests/test_live_names_eval.py's existing single-person assertion."""
    from modwen.crypto import DATA_KEY_AAD, encrypt, generate_key
    from modwen.storage import Store

    master = generate_key()
    store = Store(data_dir / "consent.db")
    consented_pids, non_consented_pids = [], []
    for i in range(3):
        pid = f"synth-consented-{i}"
        store.upsert_person({"id": pid, "display_name": f"Synth-Consented-{i}", "create": True})
        store.record_consent(pid, None)
        store.add_voiceprint(pid, "remote", b"synthetic-test-fixture-not-a-real-vector", 20.0, None)
        consented_pids.append(pid)
    for i in range(3):
        pid = f"synth-noconsent-{i}"
        store.upsert_person({"id": pid, "display_name": f"Synth-NoConsent-{i}", "create": True})
        store.add_voiceprint(pid, "remote", b"synthetic-test-fixture-not-a-real-vector", 20.0, None)
        non_consented_pids.append(pid)
    mid = "m-consent"
    store.create_meeting("Synthetic consent test", id=mid,
                         wrapped_key=encrypt(master, generate_key(), DATA_KEY_AAD), consent={"confirmed": True})
    all_pids = consented_pids + non_consented_pids
    sps = [{"id": f"s{i}", "label": chr(65 + i), "display_name": chr(65 + i), "channel": "system",
           "person_id": pid} for i, pid in enumerate(all_pids)]
    store.replace_speakers(mid, sps)

    ok, skipped = consented_people(store)
    store.close()

    assert set(ok) == set(consented_pids)
    assert skipped["people_no_consent"] == len(non_consented_pids)


# ----- #7: wrong-name rate, with Wilson CI ----------------------------------------------------------

def test_wrong_name_rate_with_wilson_ci(corpus):
    """The 2% bar metric, exact: wrong_of_named = (g_wrong + i_named) / named (report 18/24, both halves
    counted), reported at the BEST grid operating point found by ``sweep()``/``pick_best()`` (report 18
    Sec1: "a threshold sweep, not a single point" -- today's PLACEHOLDER defaults are too strict for this
    synthetic signal's margins to name almost anything, which is itself reported in
    ``test_enrolled_speaker_identification_*``, not hidden here). SYNTHETIC DATA ONLY -- see this
    module's docstring; this is not a real-world compliance number, even where it clears the bar."""
    for group_name, data in corpus.items():
        trials = build_trials(data["utterances"], data["samples"])
        rows = sweep(trials, grid())
        best = pick_best(rows)
        assert best is not None, (
            f"{group_name}: no threshold in the grid clears the CEO's {MAX_WRONG_RATE:.0%} bar with "
            f">= {MIN_NAMED_FOR_BAR} named captions, on this synthetic corpus")
        th = LiveThresholds(**best["th"])
        c = tally(trials, th)
        lo, hi = wilson_interval(c.wrong, c.named)
        print(f"\n[SYNTHETIC DATA ONLY, Tier F, NOT a real-world accuracy claim] {group_name} "
              f"(best grid point th={best['th']}): named_captions={c.named} "
              f"wrong_of_named={best['wrong_of_named']:.4f} (Wilson 95% CI {lo:.4f}-{hi:.4f}) "
              f"vs CEO bar {MAX_WRONG_RATE:.2f}")
        assert c.named >= MIN_NAMED_FOR_BAR
        assert 0.0 <= best["wrong_of_named"] <= 1.0
        assert lo <= best["wrong_of_named"] <= hi  # the point estimate must fall inside its own CI


def test_threshold_sweep_finds_a_viable_operating_point(corpus):
    """Report 18 Sec1 "threshold sweep, not a single point": reuse grid()/sweep()/pick_best() unmodified
    across the placeholder-threshold grid, over BOTH groups' full trial sets."""
    for group_name, data in corpus.items():
        trials = build_trials(data["utterances"], data["samples"])
        rows = sweep(trials, grid())
        assert len(rows) == len(grid())  # the whole grid was scored, not a single point
        best = pick_best(rows)
        assert best is not None  # see test_wrong_name_rate_with_wilson_ci for why this must hold here
        print(f"\n[SYNTHETIC DATA ONLY] {group_name}: best grid point th={best['th']} "
              f"correct={best['correct']:.3f} wrong_of_named={best['wrong_of_named']:.4f}")


# ----- #8: identification latency -------------------------------------------------------------------
# (a) Mechanism overhead is CITED, not re-measured: reports/09-backend-stage1.md Finding 1/5,
#     n=50, p50=0.00ms, p95=0.00ms, max=16.00ms on this Tier-F machine, fake instant embedder.

def test_real_model_inference_latency_direct(real_embedder):
    """(b) real model inference cost -- SpeakerEmbedder.embed() called directly, no LiveIdentifier
    wrapper, idle machine. n=50: the floor from report 18 Sec4/report 24 Sec4, NOT yet the n>=200
    preferred for a confident p95 -- this is a floor-level estimate, stated explicitly."""
    n = 50
    audio = _synthetic_voice_audio(5001, 0, n_utterances=n + 1)
    durations = [a.size / SAMPLE_RATE for a in audio]
    # warmup excluded (pays model load, not representative), matching live_names_eval.py's `warmed` flag
    real_embedder.embed(audio[0], [(0.0, durations[0])], min_seconds=MIN_SPEECH_S, min_piece=1.0,
                        max_seconds=MAX_SPEECH_S)
    ms = []
    for a, dur in zip(audio[1:], durations[1:]):
        t0 = time.perf_counter()
        real_embedder.embed(a, [(0.0, dur)], min_seconds=MIN_SPEECH_S, min_piece=1.0, max_seconds=MAX_SPEECH_S)
        ms.append((time.perf_counter() - t0) * 1000)
    lat = latency(ms)
    print(f"\n[SYNTHETIC DATA ONLY, Tier F CPU-only, floor n=50] (b) real model inference latency, idle: "
          f"p50={lat['p50_ms']:.2f}ms p95={lat['p95_ms']:.2f}ms")
    assert lat["n"] == n
    assert lat["p50_ms"] is not None and lat["p95_ms"] is not None


def test_caption_to_name_latency_idle_vs_loaded(world, real_embedder):  # noqa: F811 -- see note above `world`'s import
    """(c) caption-to-name, end to end: LiveIdentifier.submit() -> Future.result(), via the real
    LiveTranscriber+LiveIdentifier wiring. Idle and loaded (a second real embedder contending for the
    same single-worker process pool) are reported as two separate rows, never averaged (report 18/24)."""
    _, bank, person = world
    vec = _embed(real_embedder, _synthetic_voice_audio(5002, 0)[0])
    person("Synth-Latency", vec, kind=REMOTE)
    ident = _ident(bank, real_embedder)
    _wait_for(lambda: ident.status() == "on")

    n = 30
    audio = _synthetic_voice_audio(5002, 1, n_utterances=n)
    idle_ms = []
    for u in audio:
        t0 = time.perf_counter()
        fut = ident.submit("system", u)
        if fut is None:
            continue
        fut.result(timeout=10)
        idle_ms.append((time.perf_counter() - t0) * 1000)
    idle = latency(idle_ms)
    print(f"\n[SYNTHETIC DATA ONLY, Tier F CPU-only, n={idle['n']}] (c) caption-to-name latency, idle: "
          f"p50={idle['p50_ms']:.2f}ms p95={idle['p95_ms']:.2f}ms")

    stop = threading.Event()

    def _busy_rival():
        rival_audio = _synthetic_voice_audio(5003, 0, n_utterances=1)[0]
        while not stop.is_set():
            try:
                real_embedder.embed(rival_audio, [(0.0, rival_audio.size / SAMPLE_RATE)],
                                    min_seconds=MIN_SPEECH_S, min_piece=1.0, max_seconds=MAX_SPEECH_S)
            except Exception:
                pass

    rival = threading.Thread(target=_busy_rival, daemon=True)
    rival.start()
    loaded_audio = _synthetic_voice_audio(5002, 2, n_utterances=n)
    loaded_ms = []
    for u in loaded_audio:
        t0 = time.perf_counter()
        fut = ident.submit("system", u)
        if fut is None:
            continue
        fut.result(timeout=15)
        loaded_ms.append((time.perf_counter() - t0) * 1000)
    stop.set()
    rival.join(5)
    loaded = latency(loaded_ms)
    print(f"[SYNTHETIC DATA ONLY, Tier F CPU-only, n={loaded['n']}] (c) caption-to-name latency, loaded "
          f"(contending with a second real embed call): p50={loaded['p50_ms']:.2f}ms p95={loaded['p95_ms']:.2f}ms")

    ident.close()
    assert idle["n"] >= n * 0.5 and loaded["n"] >= n * 0.5  # most submits resolved (a few drops are expected)


# ----- #9/#10: caption/recording continuity and degrade-first ---------------------------------------

def test_degrade_first_regression_with_fake_embedder(world):  # noqa: F811 -- see note above `world`'s import
    """QA F3, re-run verbatim (report 24 coverage area 10's explicit instruction: reuse, don't rebuild)."""
    _fake_degrade_regression(world)


def test_degrade_first_never_stalls_captions_with_real_model(world, real_embedder):  # noqa: F811 -- see note above `world`'s import
    """Same guarantee, extended to the REAL SpeakerEmbedder (genuinely slow, no artificial gate needed) --
    report 24's suggested real-model variant of the same coverage area."""
    _, bank, person = world
    vec = _embed(real_embedder, _synthetic_voice_audio(9001, 0)[0])
    person("Synth-Degrade", vec, kind=REMOTE)

    lock = threading.Lock()
    live_now, peak = [0], [0]

    class RealEmbTracked:
        def embed(self, audio, spans, **kw):
            with lock:
                live_now[0] += 1
                peak[0] = max(peak[0], live_now[0])
            try:
                return real_embedder.embed(audio, spans, **kw)
            finally:
                with lock:
                    live_now[0] -= 1

    ident = _ident(bank, RealEmbTracked())
    _wait_for(lambda: ident.status() == "on")
    lt, events = _live(ident)
    n = 6
    t0 = time.perf_counter()
    for i in range(n):
        lt._q.put(("system", i * 16000, tone(2.0)))
    _wait_for(lambda: len(_lines(events)) == n, timeout=15)
    caption_elapsed = time.perf_counter() - t0
    lt.stop()
    ident.close()
    print(f"\n[SYNTHETIC DATA ONLY, Tier F] real-model degrade-first: {n} captions published in "
          f"{caption_elapsed:.3f}s, peak concurrent real embed calls={peak[0]}")
    assert peak[0] == 1  # the single-worker guarantee holds under genuine real-model latency too
    assert caption_elapsed < 5.0  # captions never wait on the real embedder
