"""End-to-end with fakes: recorder (file sources) -> processing -> notes; plus the HTTP API and its guards."""

from __future__ import annotations

import time

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from tests.conftest import speechy


def _services(cfg, fake_llm, script=None, **kw):
    from modwen.notify import Notifier
    from modwen.services import Services
    from modwen.stt.engines import FakeEngine

    script = script or [("en", "So the release is Thursday, right?"),
                        ("ar", "أيوه، بس محتاجين الـ QA يخلص قبلها."),
                        ("en", "I'll take the regression tests.")]
    engine = FakeEngine(script)
    return Services(cfg, stt_engine_factory=lambda: engine, llm_http_client=fake_llm.client(),
                    notifier=Notifier(cfg), **kw)


def _wait(services, mid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        m = services.store.get_meeting(mid)
        if m["status"] in ("ready", "failed"):
            return m
        time.sleep(0.1)
    raise AssertionError(f"timeout; status={services.store.get_meeting(mid)['status']}")


def test_import_and_process(cfg, fake_llm):
    from modwen.api import import_audio

    s = _services(cfg, fake_llm)
    audio = speechy([(2.0, 2.5), (2.5, 2.5), (1.5, 1.0)])  # pauses > 1.5 s -> three separate windows
    m = import_audio(s, audio, title="Imported", channel="system")
    m = _wait(s, m["id"])
    assert m["status"] == "ready", m.get("error")
    segs = s.store.get_segments(m["id"])
    assert len(segs) == 3
    assert {sg["language"] for sg in segs} == {"en", "mixed"}
    assert segs[0]["speaker_name"] == "Person 1"  # one speaker per channel without a diarizer
    notes = s.store.latest_notes(m["id"])
    assert notes["data"]["action_items"]
    assert s.notifier.sent and s.notifier.sent[-1][0] == "Notes ready: Imported"
    assert m["progress"]["steps"]["notes"] == "done"


def test_two_meetings_in_one_database(cfg, fake_llm):
    """Regression: speaker ids ('you', 'spk1') repeat across meetings and must not collide."""
    from modwen.api import import_audio

    s = _services(cfg, fake_llm)
    ids = [import_audio(s, speechy([(2.0, 1.0), (2.0, 1.0)]), title=f"M{i}")["id"] for i in range(2)]
    for mid in ids:
        assert _wait(s, mid)["status"] == "ready"
    assert all(s.store.get_speakers(mid) for mid in ids)


def test_storage_migrates_old_speakers_table(data_dir):
    import sqlite3

    from modwen.storage import Store

    db = data_dir / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE meetings (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, "
                      "created_at TEXT NOT NULL);"
                      "CREATE TABLE speakers (id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, label TEXT NOT NULL, "
                      "display_name TEXT NOT NULL, channel TEXT, is_self INTEGER DEFAULT 0, name_source TEXT, "
                      "confidence REAL DEFAULT 0);"
                      "INSERT INTO meetings VALUES ('m1','t','ready','x');"
                      "INSERT INTO speakers VALUES ('you','m1','SELF','You','mic',1,NULL,1.0);")
    con.commit()
    con.close()
    st = Store(db)
    assert st.get_speakers("m1")[0]["display_name"] == "You"
    pk = [r["name"] for r in st._conn.execute("PRAGMA table_info(speakers)").fetchall() if r["pk"]]
    assert sorted(pk) == ["id", "meeting_id"]


def test_recorder_with_file_sources_and_echo(cfg, fake_llm, data_dir):
    from modwen.audio.capture import FileSource

    sysw = data_dir / "system.wav"
    micw = data_dir / "mic.wav"
    sf.write(sysw, speechy([(2.0, 1.0), (2.0, 1.0)]), 16000)
    sf.write(micw, np.concatenate([np.zeros(16000 * 6, dtype=np.float32), speechy([(1.5, 0.5)])]), 16000)
    s = _services(cfg, fake_llm, script=[("en", "Hello from the remote side."), ("en", "Second remote line."),
                                         ("en", "My own reply.")])
    srcs = [FileSource(micw, "mic", speed=20), FileSource(sysw, "system", speed=20)]
    m = s.recorder.start("Sim", consent={"confirmed": True}, extra_sources=srcs)
    assert s.recorder.status()["recording"]
    s.recorder.mark()
    while not all(x.finished.is_set() for x in srcs):
        time.sleep(0.05)
    s.recorder.stop()
    m = _wait(s, m["id"])
    assert m["status"] == "ready", m.get("error")
    speakers = {sp["display_name"] for sp in s.store.get_speakers(m["id"])}
    assert "You" in speakers and "Person 1" in speakers
    assert m["bookmarks"]
    assert len(list((cfg.audio_dir / m["id"]).glob("*.flac.enc"))) >= 2


def test_api_security_and_flow(cfg, fake_llm):
    from modwen.api import create_app

    s = _services(cfg, fake_llm)
    app = create_app(s, token="secret-token")
    c = TestClient(app)

    assert c.get("/api/meetings").status_code == 401  # no token
    assert c.get("/api/meetings", headers={"x-modwen-token": "nope"}).status_code == 401
    assert c.get("/api/health", headers={"host": "evil.example:8765"}).status_code == 403
    assert c.get("/api/health", headers={"x-modwen-token": "secret-token",
                                         "origin": "http://evil.example"}).status_code == 403

    r = c.get("/?token=secret-token", follow_redirects=False)
    assert r.status_code == 303 and "mw_token" in r.headers.get("set-cookie", "")
    c.cookies.set("mw_token", "secret-token")

    st = c.get("/api/status").json()
    assert st["local_only"] is True and st["recorder"]["recording"] is False

    # Start without consent is refused
    assert c.post("/api/meetings/start", json={"title": "x"}).status_code == 400

    # Upload a recording
    buf = cfg.data_dir / "up.wav"
    sf.write(buf, speechy([(2.0, 1.0), (2.0, 1.0)]), 16000)
    with open(buf, "rb") as fh:
        assert c.post("/api/meetings/import", files={"file": ("up.wav", fh, "audio/wav")}).status_code == 415
    r = c.post("/api/meetings/import?title=Uploaded&meeting_language=auto", content=buf.read_bytes(),
               headers={"content-type": "audio/wav"})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    m = _wait(s, mid)
    assert m["status"] == "ready"

    detail = c.get(f"/api/meetings/{mid}").json()
    assert detail["notes"]["data"]["summary"]["en"]
    sid = detail["speakers"][0]["id"]
    old = detail["speakers"][0]["display_name"]
    r = c.post(f"/api/meetings/{mid}/speakers/{sid}/rename", json={"name": "Sara"})
    assert r.json()["old"] == old and r.json()["new"] == "Sara"
    segs = c.get(f"/api/meetings/{mid}").json()["segments"]
    assert segs[0]["speaker_name"] == "Sara"

    md = c.get(f"/api/meetings/{mid}/export.md").text
    assert md.startswith("# Meeting notes: Uploaded")
    assert "attachment" in c.get(f"/api/meetings/{mid}/export.md?download=true").headers["content-disposition"]

    # Regenerate with Arabic summary
    assert c.post(f"/api/meetings/{mid}/regenerate", json={"summary_language": "ar"}).json() == {"queued": True}
    time.sleep(0.3)
    m = _wait(s, mid)
    notes = c.get(f"/api/meetings/{mid}").json()["notes"]
    assert notes["version"] == 2 and "ar" in notes["data"]["summary"]

    # LLM profile listing never leaks api keys; cloud URL test refused
    profs = c.get("/api/llm/profiles").json()
    assert "api_key" not in profs["profiles"]["local_qwen"]
    s.cfg.llm.profiles["cloud"] = s.cfg.llm.profiles["local_qwen"].model_copy(update={"base_url": "https://x.ai/v1"})
    assert c.post("/api/llm/test", json={"profile": "cloud"}).status_code == 400

    assert c.delete(f"/api/meetings/{mid}").json() == {"deleted": mid}
    assert not (cfg.audio_dir / mid).exists()
    assert c.get(f"/api/meetings/{mid}").status_code == 404


def test_failure_is_reported_and_retry_works(cfg, fake_llm):
    import httpx

    from modwen.api import import_audio

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(500, json={"error": "server down"})
        return fake_llm.handler(request)

    from modwen.notify import Notifier
    from modwen.services import Services
    from modwen.stt.engines import FakeEngine

    s = Services(cfg, stt_engine_factory=lambda: FakeEngine(), llm_http_client=httpx.Client(
        transport=httpx.MockTransport(flaky)), notifier=Notifier(cfg))
    m = import_audio(s, speechy([(2.0, 1.0)]), title="Flaky")
    m = _wait(s, m["id"])
    assert m["status"] == "failed" and "notes" in m["error"]
    assert s.notifier.sent[-1][0].startswith("Notes failed")
    assert s.store.get_segments(m["id"])  # transcript kept
    s.enqueue_regenerate(m["id"])
    time.sleep(0.2)
    m = _wait(s, m["id"])
    assert m["status"] == "ready"


def test_summary_only_regeneration_keeps_edits(cfg, fake_llm):
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    mid = import_audio(s, speechy([(2.0, 2.5), (2.0, 1.0)]), title="Edits")["id"]
    assert _wait(s, mid)["status"] == "ready"
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    notes = c.get(f"/api/meetings/{mid}").json()["notes"]
    items = notes["data"]["action_items"]
    items[1]["owner"] = "Lina"
    items[0]["task"] = "Edited task"
    edited = c.put(f"/api/meetings/{mid}/notes", json={"data": {"action_items": items}}).json()
    assert not any("without an owner" in w for w in edited["data"]["warnings"])
    before = len(fake_llm.requests)
    assert c.post(f"/api/meetings/{mid}/regenerate", json={"scope": "summary", "summary_language": "both"}).json()
    s.jobs.join()
    after = c.get(f"/api/meetings/{mid}").json()["notes"]
    assert after["version"] == 2
    assert after["data"]["action_items"][0]["task"] == "Edited task"  # edits kept
    assert set(after["data"]["summary"]) == {"en", "ar"}
    new_reqs = fake_llm.requests[before:]
    assert new_reqs and all("Transcript lines" not in r["messages"][-1]["content"] for r in new_reqs)  # no re-extract


def test_move_line_to_new_person_and_redetect(cfg, fake_llm):
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    mid = import_audio(s, speechy([(2.0, 2.5), (2.0, 1.0)]), title="Who")["id"]
    assert _wait(s, mid)["status"] == "ready"
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    seg = c.get(f"/api/meetings/{mid}").json()["segments"][-1]
    r = c.post(f"/api/meetings/{mid}/segments/{seg['id']}/speaker", json={"speaker_id": "new"}).json()
    assert {sp["display_name"] for sp in r["speakers"]} == {"Person 1", "Person 2"}
    after = {x["id"]: x["speaker_name"] for x in c.get(f"/api/meetings/{mid}").json()["segments"]}
    assert after[seg["id"]] == "Person 2"
    assert c.post(f"/api/meetings/{mid}/segments/{seg['id']}/speaker", json={"speaker_id": "zz"}).status_code == 404
    assert c.post(f"/api/meetings/{mid}/speakers/redetect", json={"num_speakers": 99}).status_code == 400
    texts = [x["text"] for x in c.get(f"/api/meetings/{mid}").json()["segments"]]
    assert c.post(f"/api/meetings/{mid}/speakers/redetect", json={"num_speakers": 2}).json()["queued"]
    s.jobs.join()
    d = c.get(f"/api/meetings/{mid}").json()
    assert d["meeting"]["status"] == "ready" and [x["text"] for x in d["segments"]] == texts  # transcript kept
    assert d["meeting"]["stt_info"]["num_speakers"] == 2


def test_setup_settings_people_and_audio(cfg, fake_llm):
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")

    st = c.get("/api/setup/status").json()
    assert st["setup_complete"] is False and st["plan"]["tier"] and st["llm"]["choices"]
    assert {m["role"] for m in st["whisper"]["models"]} == {"final", "live"}

    # bring-your-own LLM: cloud refused, localhost accepted (fake transport answers /models)
    assert c.post("/api/setup/llm/external", json={"base_url": "https://api.openai.com/v1"}).status_code == 400

    # settings round-trip, unknown keys refused
    r = c.put("/api/settings", json={"notes.summary_language": "both", "stt.glossary": ["Lina", "MVP"]})
    assert r.status_code == 200 and r.json()["settings"]["notes.summary_language"] == "both"
    assert c.put("/api/settings", json={"app.ui_host": "0.0.0.0"}).status_code == 400
    assert c.put("/api/settings", json={"notes.summary_language": "fr"}).status_code == 400
    assert s.cfg.notes.summary_language == "both"

    # profile edit: thinking off adds enable_thinking false; cloud URL refused
    assert c.put("/api/llm/profiles/local_qwen", json={"base_url": "http://localhost:8080/v1", "model": "qwen3-8b",
                                                         "temperature": 0.2}).status_code == 200
    assert s.cfg.llm.profiles["local_qwen"].extra_body["chat_template_kwargs"] == {"enable_thinking": False}
    assert c.put("/api/llm/profiles/x", json={"base_url": "https://x.ai/v1", "model": "m"}).status_code == 400

    # people
    p = c.post("/api/people", json={"display_name": "Lina", "display_name_ar": "لينا", "email": "lina@example.com"}).json()
    assert [x["display_name"] for x in c.get("/api/people").json()["people"]] == ["Lina"]
    c.delete(f"/api/people/{p['id']}")
    assert c.get("/api/people").json()["people"] == []

    # audio player: full file and a byte range
    mid = import_audio(s, speechy([(2.0, 2.5), (2.0, 1.0)]), title="Audio")["id"]
    assert _wait(s, mid)["status"] == "ready"
    full = c.get(f"/api/meetings/{mid}/audio")
    assert full.status_code == 200 and full.content[:4] == b"RIFF"
    import io

    wav, rate = sf.read(io.BytesIO(full.content), dtype="float32")
    assert rate == 16000 and wav.size == int(7.5 * 16000)  # the whole meeting (2+2.5+2+1 s), from the chunks
    for a, b in ((0, 99), (44, 1000), (30_001, 90_000), (len(full.content) - 50, len(full.content) - 1)):
        part = c.get(f"/api/meetings/{mid}/audio", headers={"range": f"bytes={a}-{b}"})
        assert part.status_code == 206 and part.content == full.content[a:b + 1]
        assert part.headers["content-range"] == f"bytes {a}-{b}/{len(full.content)}"
    open_ended = c.get(f"/api/meetings/{mid}/audio", headers={"range": "bytes=100-"})
    assert open_ended.status_code == 206 and open_ended.content == full.content[100:100 + len(open_ended.content)]
    assert c.get(f"/api/meetings/{mid}/audio", headers={"range": f"bytes={len(full.content)}-"}).status_code == 416

    assert c.post("/api/setup/complete").status_code == 200 and s.cfg.app.setup_complete


def test_lan_access_is_opt_in_and_still_token_protected(cfg, fake_llm, monkeypatch):
    from modwen import network
    from modwen.api import create_app

    monkeypatch.setattr(network, "local_ipv4", lambda: ["192.168.100.30"])
    s = _services(cfg, fake_llm)
    lan_host = {"host": "192.168.100.30:8765"}

    # Off (default): requests addressed to the LAN IP are refused
    off = TestClient(create_app(s, token="t"))
    assert off.get("/api/health", headers={**lan_host, "x-modwen-token": "t"}).status_code == 403

    # On: the LAN IP is accepted, but only with the token, and cross-site origins stay blocked
    on = TestClient(create_app(s, token="t", lan=True))
    assert on.get("/api/health", headers=lan_host).status_code == 401
    assert on.get("/api/health", headers={**lan_host, "x-modwen-token": "t"}).status_code == 200
    assert on.get("/api/health", headers={**lan_host, "x-modwen-token": "t",
                                          "origin": "http://192.168.100.30:8765"}).status_code == 200
    assert on.get("/api/health", headers={**lan_host, "x-modwen-token": "t",
                                          "origin": "http://evil.example"}).status_code == 403
    assert on.get("/api/health", headers={"host": "10.9.9.9:8765", "x-modwen-token": "t"}).status_code == 403
    net = on.get("/api/network", headers={**lan_host, "x-modwen-token": "t"}).json()
    assert net["lan_active"] and net["urls"][0].startswith("http://192.168.100.30:8765/?token=t")


def test_host_parsing():
    from modwen.network import host_of, origin_allowed

    assert host_of("192.168.100.30:8765") == "192.168.100.30"
    assert host_of("[::1]:8765") == "[::1]"
    assert host_of("localhost") == "localhost"
    assert origin_allowed("http://localhost:5173", {"localhost"})
    assert not origin_allowed("http://localhost.evil.com", {"localhost"})
    assert not origin_allowed("file://x", {"localhost"})


def test_import_never_writes_plaintext_and_streams(cfg, fake_llm, data_dir):
    import io
    import os

    from modwen.api import create_app
    from modwen.audio.chunks import read_channel
    from modwen.audio.importer import SealedTempFile, remove_leftovers

    # The sealed temp file: random-access reads give the plaintext back; the bytes on disk are not it.
    payload = os.urandom(70_001)
    st = SealedTempFile(data_dir)
    for i in range(0, len(payload), 4096):
        st.append(payload[i:i + 4096])
    disk = st.path.read_bytes()
    assert len(disk) == len(payload) and disk != payload and payload[:64] not in disk
    for off, n in ((0, 10), (17, 33), (4095, 3), (69_990, 50)):
        st.seek(off)
        assert st.read(n) == payload[off:off + n]
    path = st.path
    st.close()
    assert not path.exists()

    # An upload: 48 kHz stereo WAV becomes one 16 kHz mono channel of the right length.
    s = _services(cfg, fake_llm)
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    t = np.arange(48000 * 7) / 48000
    stereo = np.stack([np.sin(2 * np.pi * 300 * t), np.sin(2 * np.pi * 300 * t)], axis=1).astype(np.float32) * 0.3
    buf = io.BytesIO()
    sf.write(buf, stereo, 48000, format="WAV")
    r = c.post("/api/meetings/import?title=Stereo", content=buf.getvalue(), headers={"content-type": "audio/wav"})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert not list(cfg.data_dir.glob("import-*.sealed"))  # the temp file is gone
    _wait(s, mid)
    audio = read_channel(cfg.audio_dir / mid, "system", s.keystore.unwrap(s.store.get_wrapped_key(mid)))
    assert abs(audio.size - 7 * 16000) < 200

    # Not audio: a clear error and no half-made meeting.
    before = len(s.store.list_meetings())
    r = c.post("/api/meetings/import?title=Junk", content=b"definitely not audio" * 100)
    assert r.status_code == 400 and "supported audio" in r.json()["detail"]
    assert len(s.store.list_meetings()) == before
    (cfg.data_dir / "import-x.sealed").write_bytes(b"x")
    assert remove_leftovers(cfg.data_dir) == 1


def test_notes_use_a_model_that_answers(cfg, fake_llm, monkeypatch):
    from modwen.notes.llm_client import LLMError

    s = _services(cfg, fake_llm)
    profiles = list(cfg.llm.profiles)
    active, other = cfg.llm.active, next(p for p in profiles if p != cfg.llm.active)
    cfg.llm.fallback = [other]
    up = {active: True, other: True}
    monkeypatch.setattr(s, "llm_status", lambda n=None: {"ok": up[n], "error": "connection refused"})
    assert s.pick_llm("deleted_profile") == active  # the meeting's profile is gone: use the active one
    up[active] = False
    assert s.pick_llm() == other  # active is down: the fallback writes the notes
    up[other] = False
    try:
        s.pick_llm()
        raise AssertionError("expected an error")
    except LLMError as exc:
        assert "No AI model answered" in str(exc) and "Start your local model" in str(exc)


def test_api_edge_cases_found_in_the_audit(cfg, fake_llm):
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    mid = import_audio(s, speechy([(2.0, 1.0), (2.0, 1.0)]), title="اجتماع الفريق")["id"]
    assert _wait(s, mid)["status"] == "ready"

    r = c.get(f"/api/meetings/{mid}/export.md?download=true")  # Arabic title used to crash (Latin-1 header)
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith('attachment; filename="meeting-') and "filename*=UTF-8''" in cd and "%D8%A7" in cd

    assert c.put(f"/api/meetings/{mid}/notes", json={"data": {"action_items": "oops"}}).status_code == 400
    assert c.put(f"/api/meetings/{mid}/notes", json={"data": {"bogus": 1}}).status_code == 400
    ok = c.put(f"/api/meetings/{mid}/notes", json={"data": {"summary": {"en": "Edited summary."}}})
    assert ok.status_code == 200 and ok.json()["data"]["summary"]["en"] == "Edited summary."

    for path in ("/api/recorder/pause", "/api/recorder/resume", "/api/recorder/mark"):
        assert c.post(path).status_code == 409  # nothing is recording: a clear 409, not a 500

    before = len(s.store.list_meetings())
    bad = c.post("/api/meetings/start", json={"title": "x", "mode": "banana", "consent_confirmed": True})
    assert bad.status_code == 422
    no_src = c.post("/api/meetings/start", json={"title": "x", "consent_confirmed": True, "sources": []})
    assert no_src.status_code == 400
    assert len(s.store.list_meetings()) == before  # no "failed" leftovers from bad starts

    s.store.update_meeting(mid, status="processing")
    s._pending[mid] = 1  # a job for it is queued or running
    assert c.post(f"/api/meetings/{mid}/retry").status_code == 409
    assert c.delete(f"/api/meetings/{mid}").status_code == 409
    s._pending.pop(mid)
    s.store.update_meeting(mid, status="ready")
    assert c.delete(f"/api/meetings/{mid}").status_code == 200

    assert c.get("/").headers.get("cache-control", "no-cache") == "no-cache"


def test_retention_deletes_old_data_and_keeps_pinned(cfg, fake_llm):
    from datetime import datetime, timedelta, timezone

    from modwen.api import import_audio

    s = _services(cfg, fake_llm)
    ids = [import_audio(s, speechy([(2.0, 1.0)]), title=f"M{i}")["id"] for i in range(3)]
    for mid in ids:
        _wait(s, mid)
    old, pinned, fresh = ids
    s.store.update_meeting(pinned, keep=1)
    now = datetime.now(timezone.utc)
    cfg.privacy.retention.audio_days, cfg.privacy.retention.transcript_days = 7, 0  # transcripts kept forever
    for mid in (old, pinned):
        s.store.update_meeting(mid, ended_at=(now - timedelta(days=10)).isoformat(timespec="seconds"))
    done = s.apply_retention(now)
    assert done["audio"] == 1
    assert not (cfg.audio_dir / old).exists() and s.store.get_wrapped_key(old) is None  # crypto-shredded
    assert s.store.get_segments(old)  # transcript kept (0 = forever)
    assert (cfg.audio_dir / pinned).exists() and (cfg.audio_dir / fresh).exists()
    assert s.store.get_meeting(old)["audio_deleted_at"]

    cfg.privacy.retention.transcript_days, cfg.privacy.retention.notes_days = 7, 9
    done = s.apply_retention(now)
    assert done == {"audio": 0, "transcript": 0, "meeting": 1}  # older than notes_days: the meeting goes
    assert s.store.get_meeting(old) is None and s.store.get_meeting(pinned) and s.store.get_meeting(fresh)


def test_diagnostics_has_no_meeting_content(cfg, fake_llm):
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    mid = import_audio(s, speechy([(2.0, 1.0)]), title="Secret merger talks")["id"]
    _wait(s, mid)
    r = c.get("/api/diagnostics")
    assert r.status_code == 200 and "Modwen" in r.text and "Tier" in r.text
    assert "Secret merger" not in r.text and "regression" not in r.text.lower()


def test_settings_are_validated_and_typed(cfg, fake_llm):
    from modwen.api import create_app

    s = _services(cfg, fake_llm)
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    r = c.put("/api/settings", json={"notes.chunk_minutes": "12", "privacy.retention.audio_days": 3})
    assert r.status_code == 200 and cfg.notes.chunk_minutes == 12.0 and isinstance(cfg.notes.chunk_minutes, float)
    bad = c.put("/api/settings", json={"notes.summary_language": "klingon", "privacy.retention.audio_days": 9})
    assert bad.status_code == 400 and "summary_language" in bad.json()["detail"]
    assert cfg.privacy.retention.audio_days == 3  # an invalid patch changes nothing


def test_notes_recover_from_a_gpu_out_of_memory_error(cfg, fake_llm):
    from modwen.api import import_audio
    from modwen.notes.llm_client import LLMError

    s = _services(cfg, fake_llm)
    real = s.processor.notes_engine_factory
    calls = {"n": 0, "restarts": 0}

    def flaky(profile=None):
        engine = real(profile)
        calls["n"] += 1
        if calls["n"] == 1:
            def boom(**kw):
                raise LLMError("Error code: 500 - {'error': {'message': 'Compute error.'}}")
            engine.generate = boom
        return engine

    def restart():
        calls["restarts"] += 1
        return True

    s.processor.notes_engine_factory = flaky
    s.processor.recover_llm = restart
    mid = import_audio(s, speechy([(2.0, 1.0)]), title="OOM")["id"]
    assert _wait(s, mid)["status"] == "ready" and calls["restarts"] == 1  # restarted once, then notes written

    calls["n"] = 0
    s.processor.notes_engine_factory = lambda p=None: (_ for _ in ()).throw(LLMError("Compute error."))
    s.processor.retry = None
    s.enqueue_processing(mid)
    m = _wait_any(s, mid)
    assert m["status"] == "failed" and "ran out of GPU memory" in m["error"]


def _wait_any(services, mid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        services.jobs.join()
        m = services.store.get_meeting(mid)
        if m["status"] in ("ready", "failed"):
            return m
    raise AssertionError("timeout")


def test_meeting_list_changes_are_announced(cfg, fake_llm):
    """Open windows refresh their meeting list from these events (a deleted meeting must leave the sidebar)."""
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    mid = import_audio(s, speechy([(2.0, 2.5)]), title="Gone soon")["id"]
    assert _wait(s, mid)["status"] == "ready"
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    changes = lambda: [(e["meeting_id"], e["change"]) for e in s.bus.recent if e["type"] == "meetings_changed"]
    assert (mid, "imported") in changes()
    c.get(f"/api/meetings/{mid}")
    assert (mid, "opened") in changes()
    assert c.delete(f"/api/meetings/{mid}").json() == {"deleted": mid}
    assert changes()[-1] == (mid, "deleted")
    assert all(m["id"] != mid for m in c.get("/api/meetings").json()["meetings"])


def test_audio_test_reports_levels_from_timestamped_sources(cfg, fake_llm, monkeypatch):
    """Sources call back with (channel, frames, rate, captured_at) since timestamped capture; the Settings / setup
    audio test must accept that shape (it crashed in every source thread and showed no levels)."""
    import numpy as np

    import modwen.audio.capture as capture
    from modwen.api import create_app

    class Fake:
        def __init__(self, channel, level):
            self.channel, self.level, self.description = channel, level, f"fake {channel}"

        def start(self, cb):
            cb(self.channel, np.full((1600, 1), self.level, dtype=np.float32), 16000, 12.5)

        def stop(self):
            pass

    monkeypatch.setattr(capture, "MicSource", lambda device=None: Fake("mic", 0.3))
    monkeypatch.setattr(capture, "system_source", lambda cfg: Fake("system", 0.2))
    s = _services(cfg, fake_llm)
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    r = c.post("/api/setup/audio-test", params={"seconds": 1}).json()
    assert r["errors"] == {} and r["levels"]["mic"] > 0.2 and r["levels"]["system"] > 0.1


def test_notes_edits_must_keep_each_items_text(cfg, fake_llm):
    """An action item saved without its task used to be accepted and then break the Markdown export (500)."""
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    mid = import_audio(s, speechy([(2.0, 2.5), (2.0, 1.0)]), title="Items")["id"]
    assert _wait(s, mid)["status"] == "ready"
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    for bad in ({"action_items": [{"task": None}]}, {"action_items": [{}]}, {"decisions": [{"text": 3}]},
                {"open_questions": [{}]}):
        assert c.put(f"/api/meetings/{mid}/notes", json={"data": bad}).status_code == 400, bad
    items = c.get(f"/api/meetings/{mid}").json()["notes"]["data"]["action_items"]
    items[0]["task"] = ""  # an emptied field is fine
    assert c.put(f"/api/meetings/{mid}/notes", json={"data": {"action_items": items}}).status_code == 200
    assert c.get(f"/api/meetings/{mid}/export.md").status_code == 200


def test_restart_recovers_or_explains_interrupted_meetings(cfg, fake_llm):
    """After a crash: a meeting with saved audio is processed again; one closed before its first audio chunk is
    marked failed with a plain reason instead of being queued and failing with "No audio was recorded"."""
    from modwen.api import import_audio

    s = _services(cfg, fake_llm)
    with_audio = import_audio(s, speechy([(2.0, 2.5)]), title="Saved")["id"]
    assert _wait(s, with_audio)["status"] == "ready"
    empty = s.store.create_meeting("Too short", status="recording")["id"]
    s.store.update_meeting(with_audio, status="recording")  # as if it crashed mid-recording
    s2 = _services(cfg, fake_llm)  # a fresh start runs the recovery
    assert _wait(s2, with_audio)["status"] == "ready"
    gone = s2.store.get_meeting(empty)
    assert gone["status"] == "failed" and "before any audio was saved" in gone["error"]


def test_meeting_cannot_be_deleted_during_a_summary_rewrite(cfg, fake_llm):
    """A summary-only rewrite keeps the meeting "ready"; deleting it then used to crash the job when it saved
    (FOREIGN KEY constraint failed). Delete now waits for any queued or running job."""
    from modwen.api import create_app, import_audio

    s = _services(cfg, fake_llm)
    mid = import_audio(s, speechy([(2.0, 2.5), (2.0, 1.0)]), title="Busy")["id"]
    assert _wait(s, mid)["status"] == "ready"
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    with s._pending_lock:  # as if the rewrite were still queued or running
        s._pending[mid] = 1
    assert c.get(f"/api/meetings/{mid}").json()["meeting"]["status"] == "ready"
    assert c.delete(f"/api/meetings/{mid}").status_code == 409
    with s._pending_lock:
        s._pending.pop(mid)
    assert c.delete(f"/api/meetings/{mid}").status_code == 200
