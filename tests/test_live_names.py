"""Live speaker names: privacy gates (zero embeddings), staleness, deadlines, failure isolation, no leaks."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future

import numpy as np
import pytest

from modwen.pipeline.live import LiveTranscriber
from modwen.pipeline.live_names import LiveIdentifier, LiveName
from modwen.voiceprints import LOCAL, REMOTE, VoiceBank
from tests.conftest import speechy, tone

SENTINEL = "ZZ-LiveName-ZZ"  # exists only in the People table and the live path


def _unit(*xs):
    v = np.array(xs, np.float32)
    return v / np.linalg.norm(v)


A, B = _unit(1, 0, 0), _unit(0, 1, 0)


class Emb:
    """A fake voice model: returns ``vec`` (or raises), counts calls, can be held back with ``gate``."""

    def __init__(self, vec=None, exc=None, gate=None, on_call=None):
        self.calls, self.vec, self.exc, self.gate, self.on_call, self.timeouts = 0, vec, exc, gate, on_call, []

    def embed(self, audio, spans, *, max_seconds, min_seconds, min_piece, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.on_call:
            self.on_call()
        if self.gate:
            self.gate.wait(5)
        if self.exc:
            raise self.exc
        return self.vec, 2.0


class ReadOnlyStore:
    """Only the reads live naming needs; any write is a test failure."""

    def __init__(self, store):
        self._s = store

    def __getattr__(self, name):
        if name in ("voice_summary", "list_persons", "list_voiceprints"):
            return getattr(self._s, name)
        raise AssertionError(f"live names touched the store: {name}")


@pytest.fixture()
def world(cfg, data_dir):
    from modwen.crypto import KeyStore
    from modwen.storage import Store

    store = Store(data_dir / "ln.db")
    bank = VoiceBank(store, KeyStore(cfg))

    def person(name, vec, kind=REMOTE, is_self=False):
        p = store.self_person() if is_self else store.upsert_person({"display_name": name})
        if is_self:
            store.upsert_person({"id": p["id"], "display_name": name})
        bank.add(p["id"], kind, vec, 12.0, mid="m0")
        return p["id"]

    return store, bank, person


def _ident(bank, emb, **kw):
    kw.setdefault("online", True)
    kw.setdefault("enabled", lambda: True)
    return LiveIdentifier(bank, emb, **kw)


def _name(ident, channel="system", utt=None, timeout=5):
    fut = ident.submit(channel, tone(2.0) if utt is None else utt)
    assert fut is not None
    return fut.result(timeout)


def test_named_maybe_and_other(world):
    store, bank, person = world
    person("Sara", A)
    person("Omar", B)
    emb = Emb(A)
    ident = _ident(bank, emb)
    assert _name(ident) == LiveName("Sara", "named")
    emb.vec = _unit(0.52, 0, 0.85)
    assert _name(ident) == LiveName("Sara", "maybe")
    emb.vec = _unit(0, 0, 1)
    assert _name(ident) == LiveName(None, "other")
    emb.vec = None  # too little speech to embed
    assert _name(ident) == LiveName(None, "other")
    assert ident.status() == "on" and all(t is not None and t <= 5 for t in emb.timeouts)
    ident.close()


def test_online_mic_and_unwanted_channels_are_never_embedded(world):
    _, bank, person = world
    person("Sara", A)
    emb = Emb(A)
    ident = _ident(bank, emb)
    assert ident.submit("mic", tone(2.0)) is None  # "You" is decided by channel, never by voice
    assert ident.submit("room", tone(2.0)) is None
    assert ident.submit("system", tone(0.5)) is None  # too short
    assert ident.submit("system", np.zeros(32000, np.float32)) is None  # silence
    assert emb.calls == 0
    ident.close()


def test_zero_embeddings_when_only_my_own_voice_is_saved_online(world):
    _, bank, person = world
    person("Me", A, kind=LOCAL, is_self=True)
    emb = Emb(A)
    ident = _ident(bank, emb)
    _wait_for(lambda: ident._state == "off")  # prints loaded, filtered (own print dropped), nothing usable
    assert ident.submit("system", tone(2.0)) is None
    assert emb.calls == 0 and ident.embed_calls == 0 and ident.status() == "off"
    ident.close()


def test_room_uses_my_own_voice_too(world):
    _, bank, person = world
    person("Me", A, kind=LOCAL, is_self=True)
    ident = _ident(bank, Emb(A), online=False)
    assert _name(ident, "room") == LiveName("Me", "named")
    ident.close()


def test_zero_embeddings_when_off_and_off_mid_meeting_wipes_prints(world):
    _, bank, person = world
    person("Sara", A)
    person("Omar", B)
    on = [True]
    emb = Emb(A)
    ident = _ident(bank, emb, enabled=lambda: on[0])
    assert _name(ident).state == "named"
    held = [v for kinds in ident._prints.values() for v in kinds.values()]
    on[0] = False
    assert ident.submit("system", tone(2.0)) is None
    assert emb.calls == 1 and ident.status() == "off"
    assert all(not v.any() for v in held)  # the loaded voiceprints were zeroed in place
    on[0] = True  # turning it back on does not restart naming in the same meeting
    assert ident.submit("system", tone(2.0)) is None and emb.calls == 1
    ident.close()
    off_from_start = _ident(bank, Emb(A), enabled=lambda: False)
    assert off_from_start.submit("system", tone(2.0)) is None
    assert off_from_start.embed_calls == 0
    off_from_start.close()


def test_deleting_a_person_mid_meeting_stops_naming(world):
    store, bank, person = world
    sara, _ = person("Sara", A), person("Omar", B)
    emb = Emb(A)
    ident = _ident(bank, emb)
    assert _name(ident) == LiveName("Sara", "named")
    store.delete_person(sara)
    assert _name(ident).state in ("other", "maybe")  # Sara can no longer be named
    assert _name(ident).name != "Sara"
    store.delete_person(next(p["id"] for p in store.list_persons()))
    calls = emb.calls
    assert _name(ident) == LiveName(None, "other")  # nobody left: no embedding at all
    assert emb.calls == calls and ident.status() == "off"
    ident.close()


def test_close_drops_results_and_zeroes_prints(world):
    _, bank, person = world
    person("Sara", A)
    gate = threading.Event()
    emb = Emb(A, gate=gate)
    ident = _ident(bank, emb)
    fut = ident.submit("system", tone(2.0))
    while emb.calls == 0:
        time.sleep(0.01)
    held = [v for kinds in ident._prints.values() for v in kinds.values()]
    ident.close()
    gate.set()
    assert fut.result(5) == LiveName(None, "other")  # computed after close: dropped
    assert all(not v.any() for v in held)
    assert ident.submit("system", tone(2.0)) is None and ident.status() == "off"


def test_result_after_the_deadline_is_dropped(world):
    _, bank, person = world
    person("Sara", A)
    now = [100.0]
    emb = Emb(A, on_call=lambda: now.__setitem__(0, now[0] + 6))  # the embedding "takes" 6 s
    ident = _ident(bank, emb, clock=lambda: now[0])
    assert _name(ident) == LiveName(None, "other")
    # a job that waited past its deadline in the queue is not even embedded
    gate = threading.Event()
    slow = Emb(A, gate=gate)
    ident2 = _ident(bank, slow, clock=lambda: now[0])
    f1 = ident2.submit("system", tone(2.0))
    while slow.calls == 0:
        time.sleep(0.01)
    assert ident2.submit("system", tone(2.0)) is None  # one embedding at a time: the second is skipped
    gate.set()
    f1.result(5)
    ident.close()
    ident2.close()


def test_failures_stay_inside_and_three_in_a_row_switch_naming_off(world, caplog):
    _, bank, person = world
    person("Sara", A)
    person("Omar", B)
    secret = f"{SENTINEL} array([0.1, 0.2]) 0.97"
    emb = Emb(A, exc=RuntimeError(secret))
    ident = _ident(bank, emb)
    caplog.set_level(logging.DEBUG)
    assert _name(ident) == LiveName(None, "other")  # a failure is an unnamed line, not an exception
    assert _name(ident) == LiveName(None, "other")
    emb.exc = None  # one success resets the count
    assert _name(ident) == LiveName("Sara", "named")
    emb.exc = TimeoutError(secret)
    for _ in range(3):
        assert _name(ident) == LiveName(None, "other")
    assert ident.status() == "paused" and ident.submit("system", tone(2.0)) is None
    assert emb.calls == 6
    ident.close()
    assert "RuntimeError" in caplog.text and "TimeoutError" in caplog.text  # the class is logged...
    for leak in (SENTINEL, "array(", "0.97", "0.1, 0.2", "Sara", "Omar"):
        assert leak not in caplog.text  # ...and never the details, a name or a score


def test_logs_never_hold_names_ids_or_scores(world, caplog):
    store, bank, person = world
    sara = person(SENTINEL, A)
    person("Omar", B)
    caplog.set_level(logging.DEBUG)
    ident = _ident(bank, Emb(A))
    assert _name(ident) == LiveName(SENTINEL, "named")
    ident.close()
    assert SENTINEL not in caplog.text and sara not in caplog.text


def test_identifier_never_writes_to_the_store(world):
    store, bank, person = world
    person("Sara", A)
    person("Omar", B)
    ro = VoiceBank(ReadOnlyStore(store), bank.keystore)
    ident = _ident(ro, Emb(A))
    assert _name(ident).name == "Sara"
    ident.close()


# ----- wiring into the live captions ---------------------------------------------------------------
class StubId:
    def __init__(self, ready=None):
        self.futs, self.ready, self.closed, self.state = [], ready, False, "on"

    def submit(self, channel, utt):
        f = Future()
        if self.ready is not None:
            f.set_result(self.ready)
        self.futs.append(f)
        return f

    def status(self):
        return self.state

    def close(self):
        self.closed = True


def _live(ident):
    from modwen.stt.engines import FakeEngine

    events: list[dict] = []
    lt = LiveTranscriber(lambda: FakeEngine([("en", "Hello there everyone.")]), events.append, "m1",
                         identifier=ident)
    return lt, events


def _wait_for(pred, timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("timeout")


def _lines(events):
    return [e["line"] for e in events if e["type"] == "live_line"]


def test_a_ready_name_goes_on_the_line_and_late_one_follows_as_an_event():
    stub = StubId(ready=LiveName("Sara", "named"))
    lt, events = _live(stub)
    lt._q.put(("system", 0, tone(2.0)))
    _wait_for(lambda: _lines(events))
    line = _lines(events)[0]
    assert (line["id"], line["speaker"], line["speaker_name"], line["speaker_state"]) == (1, "Others", "Sara", "named")
    assert not [e for e in events if e["type"] == "live_speaker"]  # nothing to follow up
    stub2 = StubId()
    lt2, events2 = _live(stub2)
    lt2._q.put(("system", 0, tone(2.0)))
    _wait_for(lambda: _lines(events2))
    line2 = _lines(events2)[0]
    assert line2["speaker_name"] is None and line2["speaker"] == "Others"  # shown as today, name pending
    stub2.futs[0].set_result(LiveName("Omar", "maybe"))
    _wait_for(lambda: any(e["type"] == "live_speaker" for e in events2))
    ev = next(e for e in events2 if e["type"] == "live_speaker")
    assert ev == {"type": "live_speaker", "meeting_id": "m1", "id": 1, "speaker_name": "Omar",
                  "speaker_state": "maybe"}  # a name only: no person id, no score
    assert list(lt2.lines)[0]["speaker_name"] == "Omar"
    lt.stop()
    lt2.stop()
    assert stub.closed and stub2.closed


def test_late_results_after_stop_or_without_a_match_publish_nothing():
    stub = StubId()
    lt, events = _live(stub)
    for _ in range(2):
        lt._q.put(("system", 0, tone(2.0)))
    _wait_for(lambda: len(_lines(events)) == 2)
    stub.futs[0].set_result(LiveName(None, "other"))  # no match, or dropped by the deadline
    lt.stop()
    stub.futs[1].set_result(LiveName("Sara", "named"))  # arrives after the meeting ended
    time.sleep(0.1)
    assert not [e for e in events if e["type"] == "live_speaker"]
    assert all(line["speaker_name"] is None for line in lt.lines)


def test_a_broken_identifier_never_stops_the_captions():
    class Boom(StubId):
        def submit(self, channel, utt):
            raise RuntimeError(f"{SENTINEL} array([1])")

    lt, events = _live(Boom())
    lt._q.put(("system", 0, tone(2.0)))
    _wait_for(lambda: _lines(events))
    assert _lines(events)[0]["text"] == "Hello there everyone."
    lt.stop()


def test_names_status_follows_identifier_and_pause():
    stub = StubId()
    lt, _ = _live(stub)
    assert lt.names_status() == "on"
    lt.paused = True  # captions are catching up: naming gives way
    assert lt.names_status() == "paused"
    lt.paused = False
    stub.state = "off"
    assert lt.names_status() == "off"
    lt.stop()
    plain, _ = _live(None)
    assert plain.names_status() == "off"
    plain.stop()


def test_naming_is_skipped_while_captions_are_paused():
    stub = StubId(ready=LiveName("Sara", "named"))
    lt, events = _live(stub)
    lt.paused = True
    lt._q.put(("system", 0, tone(2.0)))
    _wait_for(lambda: _lines(events))
    assert stub.futs == [] and _lines(events)[0]["speaker_name"] is None
    lt.stop()


# ----- Services / Recorder ---------------------------------------------------------------------------
def _services(cfg, fake_llm, embedder, **kw):
    from modwen.notify import Notifier
    from modwen.services import Services
    from modwen.stt.engines import FakeEngine

    engine = FakeEngine([("en", f"A line heard live number {i}.") for i in range(6)])
    return Services(cfg, stt_engine_factory=lambda: engine, live_engine_factory=lambda: FakeEngine(
        [("en", "A line heard live.")]), llm_http_client=fake_llm.client(), notifier=Notifier(cfg),
        embedder_factory=(lambda: embedder), **kw)


def test_services_builds_no_identifier_for_nobody_enrolled_or_toggles_off(cfg, fake_llm, monkeypatch):
    built = []
    s = _services(cfg, fake_llm, None)
    s._embedder_factory_override = lambda: built.append(1) or Emb(A)
    assert s.live_identifier("online", "m") is None and built == []  # nobody saved: not even the model
    p = s.store.upsert_person({"display_name": "Sara"})["id"]
    VoiceBank(s.store, s.keystore).add(p, REMOTE, A, 12.0, mid="m0")
    cfg.diarization.live_names = False
    assert s.live_identifier("online", "m") is None and s.live_identifier("in_person", "m") is None
    cfg.diarization.live_names = True
    cfg.diarization.live_names_room = False
    assert s.live_identifier("in_person", "m") is None and built == []
    ident = s.live_identifier("online", "m")
    assert ident is not None and built == [1]
    ident.close()
    cfg.diarization.live_names_room = True
    room = s.live_identifier("in_person", "m")
    assert room is not None
    room.close()


def test_services_never_downloads_the_voice_model(cfg, fake_llm, monkeypatch):
    s = _services(cfg, fake_llm, None)
    s._embedder_factory_override = None
    cfg.diarization.provider = "sherpa"
    p = s.store.upsert_person({"display_name": "Sara"})["id"]
    VoiceBank(s.store, s.keystore).add(p, REMOTE, A, 12.0, mid="m0")

    def no_network(*a, **k):
        raise AssertionError("a model download was attempted")

    monkeypatch.setattr("modwen.diarize.download_sherpa_models", no_network)
    assert s.live_identifier("online", "m") is None  # models are not on disk: simply no live names


def test_recorder_survives_a_failing_identifier_factory(cfg, data_dir):
    from modwen.crypto import KeyStore
    from modwen.pipeline.recorder import Recorder
    from modwen.storage import Store

    def boom(mode, mid):
        raise RuntimeError(SENTINEL)

    rec = Recorder(cfg, Store(data_dir / "r.db"), KeyStore(cfg), lambda e: None, on_stopped=lambda m: None,
                   identifier_factory=boom)
    assert rec._identifier("online", "m") is None


def test_live_names_reach_neither_saved_segments_nor_prompt_nor_notes(cfg, fake_llm, data_dir):
    import soundfile as sf

    from modwen.audio.capture import FileSource

    cfg.stt.live.provider = "fake"
    cfg.diarization.provider = "none"
    s = _services(cfg, fake_llm, Emb(A))
    p = s.store.upsert_person({"display_name": SENTINEL})["id"]
    VoiceBank(s.store, s.keystore).add(p, REMOTE, A, 12.0, mid="m0")
    s.processor.embedder_factory = None  # the final names would legitimately come from post-meeting processing
    wav = data_dir / "system.wav"
    sf.write(wav, speechy([(2.0, 1.5), (2.0, 1.5), (2.0, 1.5)]), 16000)
    src = FileSource(wav, "system", speed=10)
    m = s.recorder.start("Live names", consent={"confirmed": True}, extra_sources=[src])
    while not src.finished.is_set():
        time.sleep(0.05)
    time.sleep(0.5)
    assert s.recorder.status()["live"]["names"] in ("on", "paused", "off")
    names = [e["line"]["speaker_name"] for e in s.bus.recent if e["type"] == "live_line"]
    names += [e["speaker_name"] for e in s.bus.recent if e["type"] == "live_speaker"]
    s.recorder.stop()
    s.jobs.join()
    assert SENTINEL in names  # the live path did name someone (so the checks below mean something)
    mid = m["id"]
    assert s.store.get_meeting(mid)["status"] == "ready", s.store.get_meeting(mid).get("error")
    stored = repr(s.store.get_segments(mid)) + repr(s.store.get_speakers(mid)) + repr(s.store.latest_notes(mid))
    sent = repr(s.recorder.live_lines(mid)) + repr(fake_llm.requests)
    assert SENTINEL not in stored and SENTINEL not in sent
    assert fake_llm.requests  # a prompt was built
