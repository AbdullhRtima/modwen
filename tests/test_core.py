"""Unit tests: egress gate, config, language rule, hallucinations, dates, crypto/chunks, merge, hardware tiers."""

from __future__ import annotations

import sys
from datetime import date

import numpy as np
import pytest

from tests.conftest import speechy, tone


# ----- egress -------------------------------------------------------------------------------
@pytest.mark.parametrize("url,ok", [
    ("http://localhost:8080/v1", True),
    ("http://127.0.0.1:11434/v1", True),
    ("http://[::1]:8000/v1", True),
    ("http://127.0.0.5:8080", True),
    ("https://api.openai.com/v1", False),
    ("https://api.anthropic.com/v1/", False),
    ("http://192.168.1.10:8080/v1", False),
    ("http://localhost.evil.com/v1", False),
    ("not a url", False),
])
def test_egress(url, ok):
    from modwen.egress import EgressBlocked, ensure_local, is_loopback_url

    assert is_loopback_url(url) is ok
    if not ok:
        with pytest.raises(EgressBlocked):
            ensure_local(url)


def test_llm_client_refuses_cloud(cfg):
    from modwen.config import LLMProfile
    from modwen.egress import EgressBlocked
    from modwen.notes.llm_client import LLMClient

    with pytest.raises(EgressBlocked):
        LLMClient(LLMProfile(base_url="https://api.openai.com/v1", model="gpt"))


# ----- config -------------------------------------------------------------------------------
def test_config_defaults_and_cloud_is_forced_off(data_dir, monkeypatch):
    from modwen.config import load_config

    (data_dir / "config.yaml").write_text(
        "privacy:\n  allow_cloud: true\nnotes:\n  summary_language: ar\nllm:\n  active: ollama_qwen\n",
        encoding="utf-8")
    cfg = load_config()
    assert cfg.privacy.allow_cloud is False
    assert cfg.notes.summary_language == "ar"
    assert cfg.llm.profile().base_url.startswith("http://localhost:11434")
    assert cfg.notes.timezone == "system"


def test_ui_host_must_be_loopback():
    from pydantic import ValidationError

    from modwen.config import AppConfig

    with pytest.raises(ValidationError):
        AppConfig(ui_host="0.0.0.0")


def test_resolve_secret_env(monkeypatch, cfg):
    from modwen.config import resolve_secret

    monkeypatch.setenv("BB_TEST_KEY", "abc")
    assert resolve_secret("env:BB_TEST_KEY") == "abc"
    assert resolve_secret("plain") == "plain"


def test_secret_store_file_backend(cfg):
    from modwen.config import resolve_secret
    from modwen.crypto import SecretStore

    SecretStore(cfg).set("huggingface", "hf_x")
    assert resolve_secret("keychain:huggingface", cfg) == "hf_x"


# ----- language rule ------------------------------------------------------------------------
def test_choose_language():
    from modwen.stt.language import choose_language

    assert choose_language({"en": 0.9, "ar": 0.1}, "en") == "en"
    assert choose_language({"en": 0.9, "ar": 0.1}, "ar") == "ar"
    assert choose_language({"en": 0.75, "ar": 0.25}) == "ar"  # code-switched -> Arabic with bilingual prompt
    assert choose_language({"en": 0.95, "ar": 0.03, "fr": 0.02}) == "en"
    # Egyptian misdetected as Persian/Urdu folds into Arabic
    assert choose_language({"fa": 0.5, "ur": 0.2, "en": 0.3}) == "ar"
    assert choose_language(None) == "ar"


def test_script_language():
    from modwen.stt.language import has_cjk, script_language

    assert script_language("So the release is Thursday") == "en"
    assert script_language("أيوه، بس محتاجين نخلص قبلها") == "ar"
    assert script_language("الـ checklist جاهزة، فاضل الـ sign-off") == "mixed"
    assert has_cjk("summary 你好")


def test_hallucination_filter():
    from modwen.stt.hallucination import is_hallucination

    assert is_hallucination("ترجمة نانسي قنقر")
    assert is_hallucination("Thanks for watching!")
    assert is_hallucination("you you you you you you you")
    assert is_hallucination("[Music]")
    assert not is_hallucination("Thanks for joining, let's review the release plan.")
    assert not is_hallucination("الـ release يوم الخميس")


# ----- dates --------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("tomorrow", date(2026, 10, 17)),
    ("today", date(2026, 10, 16)),
    ("Thursday", date(2026, 10, 22)),          # meeting on Friday -> the coming Thursday
    ("next Tuesday", date(2026, 10, 20)),
    ("in 3 days", date(2026, 10, 19)),
    ("بكرة", date(2026, 10, 17)),
    ("بعد بكرة", date(2026, 10, 18)),
    ("الخميس الجاي", date(2026, 10, 22)),
    ("يوم الأحد", date(2026, 10, 18)),
    ("التلات", date(2026, 10, 20)),
    ("خلال يومين", date(2026, 10, 18)),
    ("23 Oct", date(2026, 10, 23)),
    ("23 أكتوبر", date(2026, 10, 23)),
    ("2026-11-02", date(2026, 11, 2)),
    ("next week", None),
    ("soon", None),
    ("ثلاثة أشخاص", None),  # "three" must not become Tuesday
])
def test_resolve_dates(raw, expected):
    from modwen.notes.dates import resolve

    assert resolve(raw, date(2026, 10, 16)) == expected


def test_meeting_weekday_assumption():
    assert date(2026, 10, 16).strftime("%A") == "Friday"


# ----- crypto + chunks ----------------------------------------------------------------------
def test_chunks_roundtrip(cfg):
    from modwen.audio.chunks import list_channels, read_channel, write_channel
    from modwen.crypto import KeyStore

    ks = KeyStore(cfg)
    key, wrapped = ks.new_data_key()
    assert ks.unwrap(wrapped) == key
    audio = tone(65.0)
    mdir = cfg.audio_dir / "m1"
    write_channel(mdir, "system", key, audio, chunk_seconds=30)
    files = sorted(mdir.glob("system-*.flac.enc"))
    assert len(files) == 3
    assert b"fLaC" not in files[0].read_bytes()[:64]  # encrypted, not plain FLAC
    back = read_channel(mdir, "system", key)
    assert back.size == audio.size
    assert np.max(np.abs(back - audio)) < 1e-3  # 16-bit FLAC quantisation
    assert list_channels(mdir) == ["system"]
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        read_channel(mdir, "system", ks.new_data_key()[0])  # wrong key fails authentication


# ----- VAD ----------------------------------------------------------------------------------
def test_energy_vad_regions():
    from modwen.audio.vad import energy_regions

    audio = speechy([(2.0, 1.0), (3.0, 1.0), (1.0, 0.5)])
    regions = energy_regions(audio, max_seconds=20, min_silence_ms=500)
    assert len(regions) == 3
    long = energy_regions(tone(45), max_seconds=20)
    assert all((b - a) / 16000 <= 20.5 for a, b in long) and len(long) >= 3


# ----- merge --------------------------------------------------------------------------------
def test_assign_and_dedupe():
    from modwen.diarize import Turn
    from modwen.pipeline.merge import assign_speakers, dedupe_echo, mark_overlaps

    segs = [{"start_ms": 0, "end_ms": 2000, "text": "a"}, {"start_ms": 5000, "end_ms": 7000, "text": "b"}]
    assign_speakers(segs, [Turn(0, 3, "S0"), Turn(4, 8, "S1")])
    assert [s["speaker_label"] for s in segs] == ["S0", "S1"]

    system = [{"start_ms": 1000, "end_ms": 4000, "text": "I'll take the regression tests."}]
    mic = [{"start_ms": 1100, "end_ms": 3900, "text": "I'll take the regression test"},
           {"start_ms": 9000, "end_ms": 10000, "text": "Great, thanks"}]
    kept = dedupe_echo(mic, system)
    assert [m["text"] for m in kept] == ["Great, thanks"]

    ov = mark_overlaps([{"start_ms": 0, "end_ms": 3000, "speaker_id": "a"},
                        {"start_ms": 2000, "end_ms": 4000, "speaker_id": "b"}])
    assert all(s.get("overlap") == 1 for s in ov)


def test_gate_to_lines_mutes_mic_echo():
    from modwen.pipeline.merge import gate_to_lines

    mic = np.ones(16000 * 10, dtype=np.float32)
    out = gate_to_lines(mic, [{"start_ms": 2000, "end_ms": 4000}])
    assert out[16000 * 3] == pytest.approx(1.0)  # inside your line: kept
    assert out[16000 * 1] == 0.0 and out[16000 * 7] == 0.0  # echo elsewhere: muted
    assert not gate_to_lines(mic, []).any()


class _FakeDiarizer:
    """Returns fixed turns per channel; the audio array's length tells the channels apart."""
    name = "fake"

    def __init__(self, by_len, prints=None):
        self.by_len, self.prints, self.calls = by_len, prints or {}, []

    def diarize(self, audio, min_speakers=None, max_speakers=None, num_speakers=None):
        self.calls.append((audio.size, num_speakers))
        return self.by_len[audio.size]

    def voiceprints(self, audio, turns):
        return {lab: v for lab, v in self.prints.items() if any(t.label == lab for t in turns)}


def _seg(a, b, text="x"):
    return {"start_ms": int(a * 1000), "end_ms": int(b * 1000), "text": text, "language": "en"}


def test_label_speakers_online_echo_room_and_numbering():
    from modwen.diarize import Turn
    from modwen.pipeline.speakers import label_speakers

    mic_audio, sys_audio = np.zeros(16000 * 100, np.float32), np.zeros(16000 * 101, np.float32)
    # Remote: R0 talks 0-30 s, R1 talks 60-90 s. Mic: echo of the call (E) while they talk, you (M) 30-50,
    # a colleague in the room (C) 50-60, and a tiny fragment (F) that belongs to nobody.
    sys_turns = [Turn(0, 30, "R0"), Turn(60, 90, "R1")]
    mic_turns = [Turn(0, 30, "E"), Turn(30, 50, "M"), Turn(50, 60, "C"), Turn(60, 90, "E"), Turn(49.5, 49.9, "F")]
    d = _FakeDiarizer({mic_audio.size: mic_turns, sys_audio.size: sys_turns},
                      prints={"M": np.array([1.0, 0.0]), "C": np.array([0.0, 1.0])})
    per = {"mic": [_seg(1, 9, "echo one"), _seg(31, 40, "my update"), _seg(51, 58, "colleague"),
                   _seg(61, 70, "echo two")],
           "system": [_seg(0, 10, "remote A"), _seg(10, 30, "remote A again"), _seg(60, 90, "remote B")]}
    speakers, segs = label_speakers({"mic": mic_audio, "system": sys_audio}, per, d, online=True)
    names = {s["id"]: s["display_name"] for s in speakers}
    said = [(names[s["speaker_id"]], s["text"]) for s in segs]
    assert "echo one" not in {t for _, t in said} and "echo two" not in {t for _, t in said}
    assert said == [("Person 1", "remote A"), ("Person 1", "remote A again"), ("You", "my update"),
                    ("Person 2", "colleague"), ("Person 3", "remote B")]
    assert next(s for s in speakers if s["is_self"])["display_name"] == "You"


def test_label_speakers_split_voice_and_count_hint():
    from modwen.diarize import Turn
    from modwen.pipeline.speakers import label_speakers

    mic_audio, sys_audio = np.zeros(16000 * 50, np.float32), np.zeros(16000 * 51, np.float32)
    mic_turns = [Turn(0, 20, "M"), Turn(20, 40, "M2")]  # one person split in two by the diarizer
    d = _FakeDiarizer({mic_audio.size: mic_turns, sys_audio.size: [Turn(40, 50, "R0")]},
                      prints={"M": np.array([1.0, 0.0]), "M2": np.array([0.95, 0.31])})
    per = {"mic": [_seg(1, 9, "first"), _seg(21, 30, "second")], "system": [_seg(41, 49, "third")]}
    speakers, segs = label_speakers({"mic": mic_audio, "system": sys_audio}, per, d, online=True, num_speakers=4)
    assert [s["speaker_id"] for s in segs] == ["you", "you", "spk1"]
    assert (sys_audio.size, 3) in d.calls  # 4 people, 1 of them on the mic -> 3 remote voices

    room = np.zeros(16000 * 30, np.float32)
    d = _FakeDiarizer({room.size: [Turn(0, 15, "A"), Turn(15, 30, "B")]})
    speakers, segs = label_speakers({"room": room}, {"room": [_seg(1, 10), _seg(16, 25)]}, d, online=False)
    assert [s["display_name"] for s in speakers] == ["Person 1", "Person 2"] and not any(s["is_self"] for s in speakers)


def test_rename_you_and_rename_to_existing_name(data_dir):
    from modwen.storage import Store

    st = Store(data_dir / "r.db")
    mid = st.create_meeting("t")["id"]
    st.replace_speakers(mid, [{"id": "you", "label": "SELF", "display_name": "You", "channel": "mic", "is_self": 1},
                              {"id": "spk1", "label": "A", "display_name": "Person 1", "channel": "system"},
                              {"id": "spk2", "label": "B", "display_name": "Person 2", "channel": "system"}])
    st.replace_segments(mid, [{"id": f"s{i}", "speaker_id": sid, "start_ms": i, "end_ms": i + 1, "text": "x"}
                              for i, sid in enumerate(["you", "spk1", "spk2"])])
    st.save_notes(mid, {"key_points": [{"text": "You: finished the demo. You should test it."}],
                        "action_items": [{"task": "Ask if you can ship", "owner": "You"}]},
                  llm_profile="p", model="m", prompt_version="v")
    st.rename_speaker(mid, "you", "Abdullah")
    data = st.latest_notes(mid)["data"]
    assert data["key_points"][0]["text"] == "Abdullah: finished the demo. You should test it."
    assert data["action_items"][0] == {"task": "Ask if you can ship", "owner": "Abdullah"}

    st.rename_speaker(mid, "spk1", "Ahmed")
    st.rename_speaker(mid, "spk2", "ahmed")  # same person, split in two by the diarizer
    assert [sp["id"] for sp in st.get_speakers(mid)] == ["you", "spk1"]
    assert {g["speaker_id"] for g in st.get_segments(mid)} == {"you", "spk1"}


def test_carry_names_after_redetect():
    from modwen.pipeline.speakers import carry_names

    speakers = [{"id": "spk1", "display_name": "Person 1"}, {"id": "spk2", "display_name": "Person 2"}]
    segs = [{"id": "a", "speaker_id": "spk1", "start_ms": 0, "end_ms": 9000},
            {"id": "b", "speaker_id": "spk1", "start_ms": 9000, "end_ms": 10000},
            {"id": "c", "speaker_id": "spk2", "start_ms": 10000, "end_ms": 12000}]
    carry_names(speakers, segs, {"a": ("Sara", 9000), "b": ("Omar", 1000), "c": ("Sara", 2000)})
    assert [s["display_name"] for s in speakers] == ["Sara", "Person 2"]  # a name goes to one speaker only


def test_line_speaker_edits(data_dir):
    from modwen.storage import Store

    st = Store(data_dir / "x.db")
    m = st.create_meeting("t")
    st.replace_speakers(m["id"], [{"id": "you", "label": "SELF", "display_name": "You", "channel": "mic", "is_self": 1},
                                  {"id": "spk1", "label": "A", "display_name": "Person 1", "channel": "system"}])
    st.replace_segments(m["id"], [{"id": "s0001", "speaker_id": "you", "start_ms": 0, "end_ms": 1, "text": "hi"},
                                  {"id": "s0002", "speaker_id": "spk1", "start_ms": 2, "end_ms": 3, "text": "yo"}])
    new = st.add_speaker(m["id"], "mic")
    assert new["display_name"] == "Person 2"
    assert st.set_segment_speaker(m["id"], "s0002", new["id"])
    assert {s["id"] for s in st.get_speakers(m["id"])} == {"you", new["id"]}  # Person 1 had no lines left
    assert not st.set_segment_speaker(m["id"], "s0001", "nobody")


# ----- hardware tiers -----------------------------------------------------------------------
def test_tiers():
    from modwen.hardware import HardwareInfo, choose_tier

    mac16 = HardwareInfo("macOS", "14.4", "arm64", "Apple M2 Pro", 10, 16.0, True, "apple", "M2 Pro", 16.0)
    assert choose_tier(mac16).tier == "D"
    mac32 = HardwareInfo("macOS", "14.4", "arm64", "Apple M3 Max", 14, 36.0, True, "apple", "M3", 36.0)
    assert choose_tier(mac32).tier == "C"
    win12 = HardwareInfo("Windows", "10", "amd64", "Intel", 8, 32.0, False, "nvidia", "RTX 4070", 12.0)
    plan = choose_tier(win12)
    assert plan.tier == "A" and plan.final_model == "large-v3"
    assert "--port 8080" in plan.llama_server_command()
    win8 = HardwareInfo("Windows", "10", "amd64", "Intel", 8, 16.0, False, "nvidia", "RTX 3060 Ti", 8.0)
    assert choose_tier(win8).tier == "B"
    cpu32 = HardwareInfo("Windows", "10", "amd64", "AMD", 8, 32.0, False)
    assert choose_tier(cpu32).tier == "E"
    cpu16 = HardwareInfo("Windows", "10", "amd64", "AMD", 4, 16.0, False)
    assert choose_tier(cpu16).tier == "F"
    small = HardwareInfo("Windows", "10", "amd64", "AMD", 4, 8.0, False)
    assert choose_tier(small).warnings


def test_merge_short_regions_into_windows():
    from modwen.audio.vad import merge_regions

    sr = 16000
    # five 1 s fragments separated by 0.5 s pauses -> one 7 s window; a 3 s pause starts a new window
    frags = [(int(i * 1.5 * sr), int((i * 1.5 + 1) * sr)) for i in range(5)] + [(int(13 * sr), int(14 * sr))]
    merged = merge_regions(frags, max_gap_s=1.5, max_len_s=20)
    assert len(merged) == 2 and merged[0] == (0, 7 * sr)
    # never longer than the limit
    many = [(i * sr, i * sr + sr // 2) for i in range(60)]
    assert all((b - a) <= 20 * sr for a, b in merge_regions(many, max_len_s=20))


def test_clean_foreign_characters_and_repetition():
    from modwen.stt.hallucination import clean_text, is_hallucination

    assert clean_text("منظЗд مع معلي الع組؟") == ("منظ مع معلي الع؟", 3)
    assert clean_text("We agreed، تمام.") == ("We agreed، تمام.", 0)
    assert is_hallucination("this this this this this")
    assert not is_hallucination("this is this")


def test_echo_inside_long_mic_line_is_removed():
    from modwen.pipeline.merge import dedupe_echo

    mic = [{"start_ms": 0, "end_ms": 16000,
            "text": "السلام عليكم. كيف حالك لينا؟ كيف حالك عبد الله؟ شو الأخبار؟ الحمد لله. اليوم عنا stand up meeting."}]
    system = [{"start_ms": 7000, "end_ms": 10000, "text": "كيف حالك عبد الله؟ شو الأخبار؟"}]
    out = dedupe_echo(mic, system)
    assert "عبد الله" not in out[0]["text"] and "stand up meeting" in out[0]["text"]


def test_refresh_warnings_after_edit():
    from modwen.notes.engine import refresh_warnings

    data = {"warnings": ["2 action item(s) without an owner", "Summary (ar) failed: x"],
            "action_items": [{"owner": "You"}, {"owner": "Lina"}]}
    assert refresh_warnings(data) == ["Summary (ar) failed: x"]


def test_encryption_format_is_stable():
    """Golden vector: recordings made by earlier versions must stay decryptable. If this fails, you changed
    the on-disk format (e.g. DATA_KEY_AAD) and existing users would lose access to their meetings."""
    from modwen.crypto import DATA_KEY_AAD, decrypt

    blob = bytes.fromhex("4ba8d691e2bf6936e65fb38bee0296e8b5118af5c978f840b99adf84a749eb10c9db00e7ec7fbe9d"
                         "6dbf91ee8edc01b6a82ce504e8da07eb9963345d")
    assert DATA_KEY_AAD == b"bluebird-data-key"
    assert decrypt(bytes(range(32)), blob, DATA_KEY_AAD) == bytes(range(32, 64))


def test_legacy_database_is_migrated(data_dir):
    import sqlite3

    from modwen.config import Config

    legacy = data_dir / "blue-bird.db"
    con = sqlite3.connect(legacy)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()  # leaves data in the -wal file until checkpoint
    path = Config().db_path  # the old database is still open here (Windows can't rename it): must still work
    con.close()
    assert path.name == "modwen.db"
    assert sqlite3.connect(path).execute("SELECT x FROM t").fetchone() == (42,)  # journal pages included
    if sys.platform != "win32":
        assert not legacy.exists()  # removed (on Windows it may stay while open; harmless)


def test_tests_never_write_outside_project(data_dir):
    import os

    from modwen.config import Config, save_config

    target = save_config(Config())
    assert str(target).startswith(str(data_dir)) and os.environ["MODWEN_CONFIG"].startswith(str(data_dir))


def test_recorder_pads_audio_a_device_dropped():
    import time

    from modwen.pipeline.recorder import Recorder, Session

    class Writer:
        def __init__(self):
            self.n = 0

        def write(self, frames):
            self.n += frames.size

    class Src:
        def __init__(self, channel, realtime=True):
            self.channel, self.realtime = channel, realtime

    w_mic, w_sys = Writer(), Writer()
    rec = Recorder.__new__(Recorder)
    s = Session("m", [Src("mic"), Src("system", realtime=False)], {"mic": w_mic, "system": w_sys}, None)
    s.started = time.monotonic() - 2.0  # 2 s into the meeting
    now = time.monotonic()
    rec._handle(s, "mic", np.zeros(8000, np.float32), 16000, now)  # only 0.5 s of mic arrived: 1.5 s were lost
    rec._handle(s, "system", np.zeros(8000, np.float32), 16000, now)  # accelerated file replay: never padded
    assert 1.85 * 16000 <= w_mic.n <= 2.0 * 16000
    assert w_sys.n == 8000
    rec._handle(s, "mic", np.zeros(1600, np.float32), 16000, now)  # on time now: nothing added
    assert s.gap_seconds["mic"] < 1.6
    # A block that waited in the queue is judged by when it arrived, not when it was written.
    late = Writer()
    s2 = Session("m2", [Src("mic")], {"mic": late}, None)
    t0 = s2.started
    for i in range(10):
        rec._handle(s2, "mic", np.zeros(1600, np.float32), 16000, t0 + (i + 1) * 0.1)
    assert late.n == 16000 and s2.gap_seconds["mic"] == 0


def test_recorder_resamples_as_one_stream_and_stops_cleanly(cfg, data_dir):
    """48 kHz stereo device blocks become one continuous 16 kHz mono track (no restarts between blocks)."""
    import threading

    from modwen.audio.chunks import list_channels, read_channel
    from modwen.crypto import KeyStore
    from modwen.pipeline.recorder import Recorder
    from modwen.storage import Store

    class Device:
        channel, realtime, overflows, description = "mic", False, 0, "test device @ 48000 Hz"

        def start(self, cb):
            def run():
                t = np.arange(4800) / 48000
                for i in range(30):  # 3 s in 100 ms blocks, a 440 Hz tone in both channels
                    x = np.sin(2 * np.pi * 440 * (t + i * 0.1)).astype(np.float32) * 0.5
                    cb("mic", np.stack([x, x], axis=1), 48000)
                done.set()

            threading.Thread(target=run, daemon=True).start()

        def stop(self):
            pass

    done = threading.Event()
    store, keys = Store(data_dir / "r.db"), KeyStore(cfg)
    rec = Recorder(cfg, store, keys, lambda e: None, on_stopped=lambda mid: None)
    m = rec.start("t", extra_sources=[Device()])
    done.wait(5)
    mid = rec.stop()
    try:
        rec.stop()  # a second Stop (watchdog + user) is refused, not a crash in the writer
    except RuntimeError:
        pass
    audio = read_channel(cfg.audio_dir / mid, "mic", keys.unwrap(store.get_wrapped_key(m["id"])))
    assert list_channels(cfg.audio_dir / mid) == ["mic"]
    assert abs(audio.size - 3 * 16000) < 400  # all 3 s kept, nothing dropped at the start
    spec = np.abs(np.fft.rfft(audio[2000:-2000]))
    freqs = np.fft.rfftfreq(audio[2000:-2000].size, 1 / 16000)
    assert abs(freqs[spec.argmax()] - 440) < 5
    clicks = np.abs(np.diff(audio[2000:-2000])).max()
    assert clicks < 0.2  # a smooth tone: no jumps where blocks meet


def test_live_skips_mic_echo_of_the_call():
    import queue

    from modwen.pipeline.live import LiveTranscriber
    from modwen.stt.engines import FakeEngine

    lt = LiveTranscriber(lambda: FakeEngine([("en", "hello")]), lambda e: None, "m")
    lt.stop()
    lt._worker.join(2)
    lt._q = queue.Queue()  # inspect what would be transcribed, without the worker taking it
    block = 1600
    talk, quiet = tone(3.0), np.zeros(16000 * 2, np.float32)
    for sysa, mica in [(talk, talk * 0.3), (quiet, quiet), (quiet, talk), (quiet, quiet)]:
        for i in range(0, sysa.size, block):  # both channels arrive together, 100 ms at a time
            lt.feed("system", sysa[i:i + block])
            lt.feed("mic", mica[i:i + block])
    queued = [ch for ch, _, _ in list(lt._q.queue)]
    assert lt.echo_skipped == 1  # the mic copy of the remote speech
    assert queued.count("mic") == 1 and queued.count("system") == 1  # your own words still go through


def test_playback_gates_mic_across_chunk_boundaries(data_dir):
    from modwen.audio.chunks import ChunkWriter
    from modwen.audio.playback import MeetingAudio
    from modwen.crypto import generate_key

    key = generate_key()
    for ch, value in (("mic", 0.5), ("system", 0.0)):
        w = ChunkWriter(data_dir / "m", ch, key, chunk_seconds=1)  # 1 s chunks: the line crosses a boundary
        for _ in range(40):  # 100 ms blocks, like a recording
            w.write(np.full(1600, value, np.float32))
        w.close()
    player = MeetingAudio(data_dir / "m", key, [{"start_ms": 1500, "end_ms": 2500}])
    assert player.total == 4 * 16000 and len(player.channels["mic"]) == 4
    out = player.render(0, player.total)
    assert out[int(2.0 * 16000)] == pytest.approx(0.5, abs=1e-3)  # inside your line (2nd/3rd chunk boundary)
    assert out[int(0.5 * 16000)] == 0 and out[int(3.5 * 16000)] == 0  # the call's echo elsewhere: muted
    whole = player.wav_bytes(0, player.size - 1)
    assert whole[:4] == b"RIFF" and len(whole) == player.size
    assert player.wav_bytes(1000, 5000) == whole[1000:5001]


def test_recorder_reopens_a_source_that_stopped_sending(cfg, data_dir):
    import time

    from modwen.crypto import KeyStore
    from modwen.pipeline.recorder import Recorder
    from modwen.storage import Store

    class Flaky:
        channel, realtime, overflows, description = "mic", True, 0, "USB mic"

        def __init__(self):
            self.starts = 0

        def start(self, cb):
            self.starts += 1

        def stop(self):
            pass

        def restart(self, cb):
            self.stop()
            self.start(cb)

    src = Flaky()
    rec = Recorder(cfg, Store(data_dir / "d.db"), KeyStore(cfg), lambda e: None, on_stopped=lambda mid: None)
    rec.start("t", extra_sources=[src])
    s = rec.session
    s.last_frame["mic"] = time.monotonic() - 10  # the device vanished 10 s ago: no frames at all
    rec._revive_dead_sources(s, time.monotonic())
    assert src.starts == 2 and "device_mic" in s.warnings
    rec._revive_dead_sources(s, time.monotonic())  # not again right away
    assert src.starts == 2
    rec.stop()


def test_recorder_uses_capture_times_late_is_not_missing():
    from modwen.pipeline.recorder import Recorder, Session

    class Writer:
        def __init__(self):
            self.parts = []

        def write(self, frames):
            self.parts.append(frames.copy())

        @property
        def n(self):
            return sum(p.size for p in self.parts)

    class Src:
        channel, realtime = "system", True

    rec = Recorder.__new__(Recorder)
    w = Writer()
    s = Session("m", [Src()], {"system": w}, None)
    t0 = s.started
    block = np.ones(1600, np.float32)  # 100 ms
    # 2 s of audio captured on time, but all of it delivered 3 s late in one burst (a busy reader).
    for i in range(20):
        rec._handle(s, "system", block, 16000, t=t0 + 5.0, captured_at=t0 + i * 0.1)
    assert w.n == 20 * 1600 and s.gap_seconds["system"] == 0  # nothing invented
    # Then nothing for 3 s (no sound playing): a real hole, filled exactly.
    rec._handle(s, "system", block, 16000, t=t0 + 5.1, captured_at=t0 + 5.0)
    assert abs(s.gap_seconds["system"] - 3.0) < 0.01 and w.n == 5 * 16000 + 1600
    # A device delivering more than real time: blocks that start before the written end are trimmed.
    for i in range(10):
        rec._handle(s, "system", np.ones(3200, np.float32), 16000, t=t0 + 6, captured_at=t0 + 5.1 + i * 0.1)
    assert abs(w.n / 16000 - 6.2) < 0.06 and s.trimmed_seconds["system"] == pytest.approx(0.9)


def test_quiet_system_audio_is_not_a_lost_device(cfg, data_dir):
    import time

    from modwen.crypto import KeyStore
    from modwen.pipeline.recorder import Recorder
    from modwen.storage import Store

    class Tap:
        channel, realtime, overflows, description, quiet_when_idle = "system", True, 0, "tap", True

        def __init__(self):
            self.starts, self.running = 0, True

        def start(self, cb):
            self.starts += 1

        def stop(self):
            pass

        def alive(self):
            return self.running

        def restart(self, cb):
            self.start(cb)

    tap = Tap()
    rec = Recorder(cfg, Store(data_dir / "q.db"), KeyStore(cfg), lambda e: None, on_stopped=lambda mid: None)
    rec.start("t", extra_sources=[tap])
    s = rec.session
    s.last_frame["system"] = time.monotonic() - 60  # a quiet call: no data for a minute
    rec._revive_dead_sources(s, time.monotonic())
    assert tap.starts == 1  # not "reopened" just because nothing played
    tap.running = False  # the helper process died
    rec._revive_dead_sources(s, time.monotonic())
    assert tap.starts == 2
    rec.stop()


def test_ai_server_is_killed_outright_even_if_it_ignores_sigterm(tmp_path):
    """Regression (2026-10-20): a politely stopped llama-server hung in its GPU clean-up, held GPU memory and
    the port, and every later note failed. Modwen must kill it outright (and find orphans by path)."""
    import subprocess
    import sys
    import time

    from modwen.runtime.llm_manager import ManagedLLM

    stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    p = subprocess.Popen([sys.executable, "-c", stubborn])
    time.sleep(0.5)
    t0 = time.monotonic()
    ManagedLLM._kill_tree(p.pid)
    assert p.wait(timeout=5) is not None and time.monotonic() - t0 < 3  # gone at once, SIGTERM not needed


def test_server_messages_have_an_arabic_translation_in_the_ui():
    """ui/src/serverText.ts translates notes / recording warnings by pattern; a reworded message must not slip past."""
    import re
    from pathlib import Path

    src = (Path(__file__).parent.parent / "ui" / "src" / "serverText.ts").read_text(encoding="utf-8")
    patterns = [re.compile(p) for p in re.findall(r"\[/(\^.*?\$)/,", src)]
    assert len(patterns) >= 12
    samples = [  # the exact f-strings of modwen/notes/engine.py and modwen/pipeline/recorder.py
        f"Dropped a deadline not found in the transcript ({'بكرة'!r})", "3 item(s) without transcript evidence (low confidence)",
        "2 action item(s) without an owner", "Summary (ar) failed: timeout",
        "Bluetooth mic lowers accuracy. Use built-in or wired.",
        "The microphone device changed; recording continues on MacBook Pro Microphone.",
        "Lost the system device and could not reopen it: busy", "No system audio for 75 s. Is the meeting muted?",
        "Recording for 3.0 h; safety stop at 4 h.", "Low disk space (1.5 GB); stopping to protect the recording.",
        "This computer is busy; live captions are paused. The recording is safe.",
        "The audio device dropped sound 2 time(s); timing was kept. Close heavy apps if this keeps happening.",
        "Modwen closed before any audio was saved (audio is saved every 30 s). Nothing to process.",
    ]
    for msg in samples:
        assert any(p.match(msg) for p in patterns), msg
    engine = (Path(__file__).parent.parent / "modwen" / "notes" / "engine.py").read_text(encoding="utf-8")
    recorder = (Path(__file__).parent.parent / "modwen" / "pipeline" / "recorder.py").read_text(encoding="utf-8")
    for fragment in ("Dropped a deadline not found in the transcript", "without transcript evidence (low confidence)",
                     "action item(s) without an owner"):
        assert fragment in engine, fragment
    for fragment in ("Bluetooth mic lowers accuracy", "Is the meeting muted?", "safety stop at", "Low disk space",
                     "live captions are paused", "dropped sound"):
        assert fragment in recorder, fragment
