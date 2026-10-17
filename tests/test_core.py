"""Unit tests: egress gate, config, language rule, hallucinations, dates, crypto/chunks, merge, hardware tiers."""

from __future__ import annotations

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
    path = Config().db_path
    con.close()
    assert path.name == "modwen.db" and not legacy.exists()
    assert sqlite3.connect(path).execute("SELECT x FROM t").fetchone() == (42,)


def test_tests_never_write_outside_project(data_dir):
    import os

    from modwen.config import Config, save_config

    target = save_config(Config())
    assert str(target).startswith(str(data_dir)) and os.environ["MODWEN_CONFIG"].startswith(str(data_dir))
