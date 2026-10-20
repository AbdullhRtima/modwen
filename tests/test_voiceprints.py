"""Voiceprints (§5.1): matching rules, encrypted storage, and the name-once / recognised-next-time flow."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from modwen.diarize import Turn
from tests.conftest import speechy

# Two voices: whatever is said in the first half of a recording is "A", the second half "B".
VOICES = {"A": np.array([1.0, 0.0, 0.0], np.float32), "B": np.array([0.0, 1.0, 0.0], np.float32)}
HALF = 9.0  # seconds; the test audio below is ~18 s


class FakeDiarizer:
    name = "fake"

    def diarize(self, audio, lo=None, hi=None, num=None):
        end = audio.size / 16000
        return [Turn(0, HALF, "A"), Turn(HALF, end, "B")]


class FakeEmbedder:
    """Embeds by where the speech is: lines before HALF sound like A, after it like B."""

    def __init__(self):
        self.calls = 0

    def embed(self, audio, spans, *, max_seconds=60.0, min_seconds=10.0, min_piece=1.0):
        self.calls += 1
        if not spans:
            return None, 0.0
        mid = sum((a + b) / 2 for a, b in spans) / len(spans)
        return VOICES["A" if mid < HALF else "B"], 12.0


def _audio():
    return speechy([(3.0, 1.0), (3.0, 2.5), (3.0, 1.0), (3.0, 2.0)])  # 2 lines per half


def _services(cfg, fake_llm, embedder):
    from modwen.notify import Notifier
    from modwen.services import Services
    from modwen.stt.engines import FakeEngine

    engine = FakeEngine([("en", "Line one from here."), ("en", "Line two from here."),
                         ("en", "Line three from there."), ("en", "Line four from there.")])
    return Services(cfg, stt_engine_factory=lambda: engine, llm_http_client=fake_llm.client(),
                    notifier=Notifier(cfg), diarizer_factory=FakeDiarizer, embedder_factory=lambda: embedder)


def _wait(s, mid):
    s.jobs.join()
    m = s.store.get_meeting(mid)
    assert m["status"] == "ready", m.get("error")
    return m


def test_match_rules():
    from modwen.voiceprints import match

    people = {"sara": {"remote": np.array([1.0, 0, 0], np.float32)},
              "omar": {"remote": np.array([0.0, 1, 0], np.float32)}}

    def v(*x):
        x = np.array(x, np.float32)
        return x / np.linalg.norm(x), "remote"

    kw = {"threshold": 0.7, "margin": 0.05, "suggest": 0.6}
    found = match({"s1": v(1, 0.1, 0), "s2": v(0.1, 1, 0), "s3": v(0, 0, 1)}, people, **kw)
    assert found["s1"][0] == "sara" and found["s1"][2] == "named"
    assert found["s2"][0] == "omar" and "s3" not in found  # nobody sounds like s3
    # Two speakers both closest to Sara: only the better one gets her.
    found = match({"s1": v(1, 0.05, 0), "s2": v(1, 0.3, 0)}, people, **kw)
    assert found["s1"][0] == "sara" and "s2" not in found  # Sara is taken, and s2 is not Omar
    # Too close to call between two people: left unnamed (margin), and a near miss is only a suggestion.
    assert "s1" not in match({"s1": v(1, 1, 0)}, people, **kw)
    assert match({"s1": v(1, 1, 0.3)}, {"sara": people["sara"]}, **kw)["s1"] == ("sara", 0.692, "suggested")
    assert match({"s1": v(1, 0, 0)}, people, banned={("s1", "sara")}, **kw).get("s1", ("",))[0] != "sara"


def test_voicebank_encrypts_and_keeps_recent(cfg, data_dir):
    from modwen.crypto import KeyStore
    from modwen.storage import Store
    from modwen.voiceprints import VoiceBank

    st = Store(data_dir / "v.db")
    pid = st.upsert_person({"display_name": "Sara"})["id"]
    bank = VoiceBank(st, KeyStore(cfg), keep=3)
    for i in range(5):
        bank.add(pid, "remote", VOICES["A"] * (1 + i), 12.0, mid=f"m{i}")
    rows = st.list_voiceprints()
    assert len(rows) == 3  # oldest samples roll off
    assert VOICES["A"].tobytes() not in rows[0]["vector"]  # stored encrypted
    c = bank.centroids()[pid]["remote"]
    assert np.allclose(c, VOICES["A"])
    bank.add(pid, "remote", VOICES["B"], 12.0, mid="m4")  # same meeting again: replaces its sample
    assert len(st.list_voiceprints()) == 3
    st.delete_person(pid)
    assert st.list_voiceprints() == []


def test_name_once_recognised_next_meeting(cfg, fake_llm):
    from modwen.api import create_app, import_audio

    emb = FakeEmbedder()
    s = _services(cfg, fake_llm, emb)
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")

    m1 = import_audio(s, _audio(), title="Monday")["id"]
    _wait(s, m1)
    assert emb.calls == 0  # nobody enrolled yet: no voice work at all
    sp = {x["display_name"]: x for x in c.get(f"/api/meetings/{m1}").json()["speakers"]}
    assert set(sp) == {"Person 1", "Person 2"}

    r = c.post(f"/api/meetings/{m1}/speakers/{sp['Person 1']['id']}/person",
               json={"name": "Sara", "remember_voice": True}).json()
    assert r["voice"]["saved"] and r["person"]["consent_at"]
    r = c.post(f"/api/meetings/{m1}/speakers/{sp['Person 2']['id']}/person", json={"name": "Omar"}).json()
    assert r["voice"] is None  # named for this meeting only: no voiceprint without consent
    people = {p["display_name"]: p for p in c.get("/api/people").json()["people"]}
    assert people["Sara"]["voice"]["samples"] == 1 and people["Omar"]["voice"] is None

    m2 = import_audio(s, _audio(), title="Tuesday")["id"]
    _wait(s, m2)
    sp2 = {x["display_name"]: x for x in c.get(f"/api/meetings/{m2}").json()["speakers"]}
    assert sp2["Sara"]["name_source"] == "voiceprint" and sp2["Sara"]["confidence"] == pytest.approx(1.0)
    assert "Person 2" in sp2  # Omar has no voiceprint
    notes = c.get(f"/api/meetings/{m2}").json()["notes"]
    assert notes  # notes were written after recognition, with the name in the transcript
    lines = c.get(f"/api/meetings/{m2}").json()["segments"]
    assert lines[0]["speaker_name"] == "Sara"

    # "Not Sara": back to a Person label, and re-detect won't pair them again in this meeting.
    r = c.post(f"/api/meetings/{m2}/speakers/{sp2['Sara']['id']}/reject").json()
    assert "Sara" not in {x["display_name"] for x in r["speakers"]}
    assert c.post(f"/api/meetings/{m2}/speakers/redetect", json={}).json()["queued"]
    _wait(s, m2)
    assert "Sara" not in {x["display_name"] for x in c.get(f"/api/meetings/{m2}").json()["speakers"]}

    # Deleting the voiceprint stops recognition.
    assert c.delete(f"/api/people/{people['Sara']['id']}/voiceprint").json()
    m3 = import_audio(s, _audio(), title="Wednesday")["id"]
    _wait(s, m3)
    assert "Sara" not in {x["display_name"] for x in c.get(f"/api/meetings/{m3}").json()["speakers"]}


def test_confirm_learns_and_names_win(cfg, fake_llm):
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm, FakeEmbedder())
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    m1 = import_audio(s, _audio(), title="One")["id"]
    _wait(s, m1)
    first = c.get(f"/api/meetings/{m1}").json()["speakers"][0]
    c.post(f"/api/meetings/{m1}/speakers/{first['id']}/person", json={"name": "Sara", "remember_voice": True})

    m2 = import_audio(s, _audio(), title="Two")["id"]
    _wait(s, m2)
    sara = next(x for x in c.get(f"/api/meetings/{m2}").json()["speakers"] if x["display_name"] == "Sara")
    r = c.post(f"/api/meetings/{m2}/speakers/{sara['id']}/confirm").json()
    assert r["voice"]["saved"]
    me = next(x for x in r["speakers"] if x["id"] == sara["id"])
    assert me["name_source"] == "manual"  # confirmed: now a name the user stands behind
    sid = next(p["id"] for p in c.get("/api/people").json()["people"] if p["display_name"] == "Sara")
    assert c.get("/api/people").json()["people"][0]["voice"]["samples"] >= 1
    assert any(p["id"] == sid and p["voice"]["samples"] == 2 for p in c.get("/api/people").json()["people"])

    # A name typed for another speaker in this meeting is never overridden by a voice match.
    other = next(x for x in r["speakers"] if x["id"] != sara["id"])
    c.post(f"/api/meetings/{m2}/speakers/{sara['id']}/person", json={"name": "Lina"})
    c.post(f"/api/meetings/{m2}/speakers/{other['id']}/person", json={"name": "Sara"})
    c.post(f"/api/meetings/{m2}/speakers/redetect", json={})
    _wait(s, m2)
    names = sorted(x["display_name"] for x in c.get(f"/api/meetings/{m2}").json()["speakers"])
    assert names == ["Lina", "Sara"]


def test_remember_my_voice_finds_you_in_person(cfg, fake_llm):
    from modwen.api import import_audio

    s = _services(cfg, fake_llm, FakeEmbedder())
    m1 = import_audio(s, _audio(), title="Call", channel="mic")["id"]  # online: the mic's main voice is you
    _wait(s, m1)
    assert s.store.voice_summary() == {}  # off by default: your voice is not kept

    cfg.diarization.remember_my_voice = True
    s.enqueue_redetect(m1)
    _wait(s, m1)
    me = s.store.self_person()
    assert s.store.voice_summary()[me["id"]]["samples"] == 1

    m2 = import_audio(s, _audio(), title="Room", channel="room")["id"]  # in person: no "You" from a channel
    _wait(s, m2)
    sp = {x["display_name"]: x for x in s.store.get_speakers(m2)}
    assert sp["You"]["is_self"] == 1 and sp["You"]["name_source"] == "voiceprint"
    assert sp["You"]["label"] == "B" and "Person 1" in sp  # B spoke most on the call, so B is you


def test_short_and_split_speaker_is_still_recognised():
    """Real case: Taymaa spoke ~20 s, the diarizer split her in two (8 s + 10 s); neither was compared before."""
    from modwen.voiceprints import recognise

    taymaa = np.array([1.0, 0.0, 0.0], np.float32)

    class Bank:
        def centroids(self):
            return {"t": {"remote": taymaa}, "o": {"remote": np.array([0.0, 1.0, 0.0], np.float32)}}

    class Emb:
        def embed(self, audio, spans, *, max_seconds=60, min_seconds=10, min_piece=1.0):
            secs = sum(b - a for a, b in spans)
            if secs < min_seconds:
                return None, secs
            v = np.array([0.95, 0.1, 0.1] if spans[0][0] < 20 else [0.8, 0.2, 0.3], np.float32)
            return v / np.linalg.norm(v), secs

    speakers = [{"id": "spk1", "channel": "system", "display_name": "Person 1"},
                {"id": "spk2", "channel": "system", "display_name": "Person 2"}]
    segs = [{"id": "s1", "speaker_id": "spk1", "start_ms": 3000, "end_ms": 11300},     # 8.3 s
            {"id": "s2", "speaker_id": "spk2", "start_ms": 41000, "end_ms": 47000}]    # 6 s, the other half
    people = {"t": {"display_name": "Taymaa", "is_self": 0}, "o": {"display_name": "Omar", "is_self": 0}}
    recognise(Bank(), Emb(), {"system": np.zeros(16000 * 60, np.float32)}, speakers, segs, people, online=True,
              threshold=0.7, margin=0.05, suggest=0.6)
    assert [sp["display_name"] for sp in speakers] == ["Taymaa"]  # named, and the split half merged back in
    assert {s["speaker_id"] for s in segs} == {"spk1"}


def test_lines_go_to_the_person_they_sound_like():
    """Real case: a 23 s room meeting, you and Taymaa in turn, detected as ONE voice and named "You"."""
    from modwen.voiceprints import recognise

    me, taymaa = np.array([1.0, 0, 0], np.float32), np.array([0, 1.0, 0], np.float32)

    class Bank:
        def centroids(self):
            return {"self": {"local": me}, "t": {"remote": taymaa}}

    voice_at = {1: [0.9, 0.2, 0.1], 4: [0.3, 0.6, 0.2], 7: [0.8, 0.25, 0.2], 10: [0.25, 0.55, 0.3],
                13: [0.75, 0.2, 0.3], 18: [0.3, 0.32, 0.6]}  # per-line voices, like the real scores

    class Emb:
        def embed(self, audio, spans, *, max_seconds=60, min_seconds=10, min_piece=1.0):
            secs = sum(b - a for a, b in spans)
            if secs < min_seconds:
                return None, secs
            if len(spans) > 1:  # the whole cluster: a blend of both voices, closer to you
                v = np.array([0.75, 0.5, 0.2], np.float32)
            else:
                v = np.array(voice_at[int(spans[0][0])], np.float32)
            return v / np.linalg.norm(v), secs

    speakers = [{"id": "spk1", "channel": "room", "display_name": "Person 1"}]
    segs = [{"id": f"s{i}", "speaker_id": "spk1", "start_ms": t * 1000, "end_ms": t * 1000 + 2500}
            for i, t in enumerate(voice_at)]
    people = {"self": {"display_name": "You", "is_self": 1}, "t": {"display_name": "Taymaa", "is_self": 0}}
    recognise(Bank(), Emb(), {"room": np.zeros(16000 * 30, np.float32)}, speakers, segs, people, online=False,
              threshold=0.7, margin=0.05, suggest=0.6)
    names = {sp["id"]: sp["display_name"] for sp in speakers}
    said = [names[s["speaker_id"]] for s in segs]
    # The unclear last line is not guessed as "You": it goes to "Room", flagged for the user to set.
    assert said == ["You", "Taymaa", "You", "Taymaa", "You", "Room"]
    assert [s.get("uncertain", 0) for s in segs] == [0, 0, 0, 0, 0, 1]
