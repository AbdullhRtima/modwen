"""People <-> meeting speakers stay in sync: one place to rename or delete someone, merges that keep links,
and no edits lost to processing."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_voiceprints import FakeEmbedder, _audio, _services, _wait


def _client(cfg, fake_llm):
    from modwen.api import create_app

    s = _services(cfg, fake_llm, FakeEmbedder())
    c = TestClient(create_app(s, token="t"))
    c.cookies.set("mw_token", "t")
    return s, c


def _meeting(s, title="M", channel="system"):
    from modwen.api import import_audio

    mid = import_audio(s, _audio(), title=title, channel=channel)["id"]
    _wait(s, mid)
    return mid


def _names(c, mid):
    return sorted(x["display_name"] for x in c.get(f"/api/meetings/{mid}").json()["speakers"])


def _speaker(c, mid, name):
    return next(x for x in c.get(f"/api/meetings/{mid}").json()["speakers"] if x["display_name"] == name)


def _notes_with(s, mid, owner, text):
    n = s.store.latest_notes(mid)
    data = {**n["data"], "action_items": [{"task": "Ship it", "owner": owner, "evidence_segment_ids": []}],
            "key_points": [{"text": text, "evidence_segment_ids": []}]}
    s.store.update_notes_data(n["id"], data)


def test_rename_and_delete_person_reach_every_meeting(cfg, fake_llm):
    s, c = _client(cfg, fake_llm)
    m1, m2 = _meeting(s, "One"), _meeting(s, "Two")
    for mid in (m1, m2):
        c.post(f"/api/meetings/{mid}/speakers/{_speaker(c, mid, 'Person 1')['id']}/rename", json={"name": "Sara"})
    _notes_with(s, m1, "Sara", "Sara: finished the export")
    sara = next(p for p in c.get("/api/people").json()["people"] if p["display_name"] == "Sara")

    r = c.put(f"/api/people/{sara['id']}", json={"display_name": "Sara Haddad"}).json()
    assert r["meetings"] == 2
    assert "Sara Haddad" in _names(c, m1) and "Sara Haddad" in _names(c, m2)
    notes = s.store.latest_notes(m1)["data"]
    assert notes["action_items"][0]["owner"] == "Sara Haddad"
    assert notes["key_points"][0]["text"] == "Sara Haddad: finished the export"
    lines = c.get(f"/api/meetings/{m1}").json()["segments"]
    assert lines[0]["speaker_name"] == "Sara Haddad"

    r = c.delete(f"/api/people/{sara['id']}").json()
    assert r["meetings"] == 2
    for mid in (m1, m2):
        assert "Sara Haddad" not in _names(c, mid) and all(n.startswith("Person ") for n in _names(c, mid))
    assert s.store.latest_notes(m1)["data"]["action_items"][0]["owner"].startswith("Person ")
    # Back to unnamed: voice matching and re-detect treat it as a fresh speaker, not a name you typed.
    assert all(x["name_source"] is None for x in c.get(f"/api/meetings/{m1}").json()["speakers"])


def test_people_validation(cfg, fake_llm):
    s, c = _client(cfg, fake_llm)
    assert c.post("/api/people", json={"display_name": "Omar"}).status_code == 200
    assert c.post("/api/people", json={"display_name": "omar "}).status_code == 409  # same name, any case
    assert c.post("/api/people", json={"display_name": "  "}).status_code == 400
    assert c.put("/api/people/nope", json={"display_name": "X"}).status_code == 404  # no silent create
    assert c.delete("/api/people/nope").status_code == 404
    me = s.store.self_person()
    assert c.delete(f"/api/people/{me['id']}").status_code == 400  # you can't delete yourself


def test_your_name_and_this_is_me(cfg, fake_llm):
    s, c = _client(cfg, fake_llm)
    mid = _meeting(s, "Call", channel="mic")  # online, mic only: the main voice is you
    assert "You" in _names(c, mid)
    me = s.store.self_person()
    c.put(f"/api/people/{me['id']}", json={"display_name": "Abdullah"})
    assert "Abdullah" in _names(c, mid) and _speaker(c, mid, "Abdullah")["is_self"] == 1

    # An older entry for yourself, "This is me": one entry, your voiceprints kept, your name everywhere.
    from modwen.voiceprints import VoiceBank
    from tests.test_voiceprints import VOICES

    VoiceBank(s.store, s.keystore).add(me["id"], "local", VOICES["A"], 12.0, mid=mid)
    other = c.post("/api/people", json={"display_name": "Abdullah Ahmed"}).json()
    r = c.post(f"/api/people/{other['id']}/self").json()
    assert r["is_self"] == 1
    people = c.get("/api/people").json()["people"]
    assert [p["display_name"] for p in people if p["is_self"]] == ["Abdullah Ahmed"] and len(people) == 1
    assert people[0]["voice"]["samples"] == 1
    assert "Abdullah Ahmed" in _names(c, mid)
    _meeting(s, "Next", channel="mic")
    assert all("Abdullah Ahmed" in _names(c, m["id"]) for m in c.get("/api/meetings").json()["meetings"])


def test_old_names_get_linked(cfg, fake_llm, data_dir):
    """Names typed before People links existed are linked on start, so People edits reach them."""
    from modwen.storage import Store

    s, c = _client(cfg, fake_llm)
    mid = _meeting(s)
    sid = _speaker(c, mid, "Person 1")["id"]
    s.store.update_speaker(mid, sid, display_name="Momin", name_source="manual")  # as an old version stored it
    pid = s.store.upsert_person({"display_name": "Momin"})["id"]
    Store(cfg.db_path).sync_people()
    assert _speaker(c, mid, "Momin")["person_id"] == pid
    c.delete(f"/api/people/{pid}")
    assert "Momin" not in _names(c, mid)


def test_redetect_keeps_people_links(cfg, fake_llm):
    s, c = _client(cfg, fake_llm)
    mid = _meeting(s)
    c.post(f"/api/meetings/{mid}/speakers/{_speaker(c, mid, 'Person 1')['id']}/rename", json={"name": "Lina"})
    c.post(f"/api/meetings/{mid}/speakers/redetect", json={})
    _wait(s, mid)
    lina = _speaker(c, mid, "Lina")
    assert lina["person_id"]
    pid = lina["person_id"]
    c.put(f"/api/people/{pid}", json={"display_name": "Lina K"})
    assert "Lina K" in _names(c, mid)


def test_merge_rules(cfg, fake_llm):
    s, c = _client(cfg, fake_llm)
    mid = _meeting(s, channel="mic")  # "You" + "Person 1"
    you, p1 = _speaker(c, mid, "You")["id"], _speaker(c, mid, "Person 1")["id"]
    assert c.post(f"/api/meetings/{mid}/speakers/{p1}/merge", json={"into": p1}).status_code == 400
    assert c.post(f"/api/meetings/{mid}/speakers/{p1}/merge", json={"into": "ghost"}).status_code == 400
    lines_before = len(c.get(f"/api/meetings/{mid}").json()["segments"])
    _notes_with(s, mid, "Person 1", "Person 1: will test")
    r = c.post(f"/api/meetings/{mid}/speakers/{you}/merge", json={"into": p1}).json()
    assert r["into"] == you  # your own speaker always stays
    d = c.get(f"/api/meetings/{mid}").json()
    assert [x["id"] for x in d["speakers"]] == [you] and len(d["segments"]) == lines_before
    assert {g["speaker_id"] for g in d["segments"]} == {you}
    assert s.store.latest_notes(mid)["data"]["action_items"][0]["owner"] == "You"  # notes follow the merge

    mid = _meeting(s)  # Person 1 + Person 2; a link survives merging the named one away
    c.post(f"/api/meetings/{mid}/speakers/{_speaker(c, mid, 'Person 1')['id']}/rename", json={"name": "Omar"})
    omar, p2 = _speaker(c, mid, "Omar"), _speaker(c, mid, "Person 2")
    c.post(f"/api/meetings/{mid}/speakers/{omar['id']}/merge", json={"into": p2["id"]})
    assert _speaker(c, mid, "Person 2")["person_id"] == omar["person_id"]


def test_no_edits_while_processing(cfg, fake_llm):
    s, c = _client(cfg, fake_llm)
    mid = _meeting(s)
    sp = _speaker(c, mid, "Person 1")
    seg = c.get(f"/api/meetings/{mid}").json()["segments"][0]
    s.store.update_meeting(mid, status="processing")
    for r in (c.post(f"/api/meetings/{mid}/speakers/{sp['id']}/rename", json={"name": "X"}),
              c.post(f"/api/meetings/{mid}/segments/{seg['id']}/speaker", json={"speaker_id": "new"}),
              c.put(f"/api/meetings/{mid}/segments/{seg['id']}", json={"text": "edited"})):
        assert r.status_code == 409 and "still working" in r.json()["detail"]
    s.store.update_meeting(mid, status="ready")
    assert c.post(f"/api/meetings/{mid}/speakers/{sp['id']}/rename", json={"name": "X"}).status_code == 200


def test_undo_voice_match_keeps_speaker_matchable(cfg, fake_llm):
    s, c = _client(cfg, fake_llm)
    m1 = _meeting(s, "One")
    c.post(f"/api/meetings/{m1}/speakers/{_speaker(c, m1, 'Person 1')['id']}/person",
           json={"name": "Sara", "remember_voice": True})
    c.post(f"/api/meetings/{m1}/speakers/{_speaker(c, m1, 'Person 2')['id']}/person",
           json={"name": "Omar", "remember_voice": True})
    m2 = _meeting(s, "Two")
    assert sorted(_names(c, m2)) == ["Omar", "Sara"]
    c.post(f"/api/meetings/{m2}/speakers/{_speaker(c, m2, 'Sara')['id']}/reject")
    undone = next(x for x in c.get(f"/api/meetings/{m2}").json()["speakers"] if x["display_name"] != "Omar")
    assert undone["name_source"] is None and undone["person_id"] is None and undone["display_name"].startswith("Person")
