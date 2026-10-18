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
    assert segs[0]["speaker_name"] == "Others"  # single-speaker fallback without pyannote
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
    assert "You" in speakers and "Others" in speakers
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
        r = c.post("/api/meetings/import", files={"file": ("up.wav", fh, "audio/wav")},
                   data={"title": "Uploaded", "meeting_language": "auto"})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    m = _wait(s, mid)
    assert m["status"] == "ready"

    detail = c.get(f"/api/meetings/{mid}").json()
    assert detail["notes"]["data"]["summary"]["en"]
    sid = detail["speakers"][0]["id"]
    old = detail["speakers"][0]["display_name"]
    r = c.post(f"/api/meetings/{mid}/speakers/{sid}/rename", json={"name": "Sara"})
    assert r.json() == {"old": old, "new": "Sara"}
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
    assert full.status_code == 200 and full.content[:4] == b"fLaC"
    part = c.get(f"/api/meetings/{mid}/audio", headers={"range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100

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
