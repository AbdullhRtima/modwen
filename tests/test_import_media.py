"""MP4, MOV and M4A import, refusals and interrupted imports. Every media file is synthesised here with PyAV."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from tests.conftest import speechy

av = pytest.importorskip("av")

ROOT = Path(__file__).resolve().parent.parent
RATE = 48000
NO_AUDIO = "This file has no audio, so there's nothing to transcribe. Choose a file with sound."
UNSUPPORTED = ("This isn't a supported audio or video file. It may be copy-protected or use a format Modwen "
               "can't read. Use WAV, FLAC, MP3, OGG, AIFF, MP4, MOV or M4A.")
DAMAGED = "This file is damaged or incomplete, so nothing was imported. If you can, get a fresh copy and import that."
EMPTY = "This file is empty. Choose the original recording and import it again."
TOO_BIG = "This file is larger than 8 GB, the limit for imports. Use a smaller copy and import that."
GENERAL = ("The import didn't finish, so the file wasn't imported. Check that this computer has free space "
           "and that Modwen is running, then try again.")
SEGMENTS = [(4.0, 2.0), (4.0, 2.0)]
FRAGMENTED = "empty_moov+default_base_moof"


# ----- synthetic media -----
def _encode_audio(out, stream, freq: float, seconds: float, *, wave=None):
    """Yield encoded packets of a tone with silent gaps, so the energy VAD finds speech in it."""
    ctx = stream.codec_context
    resampler = av.AudioResampler(format=ctx.format.name, layout=ctx.layout.name, rate=ctx.sample_rate,
                                  frame_size=ctx.frame_size or 1024)
    n = int(seconds * RATE)
    if wave is None:
        wave = 0.3 * np.sin(2 * np.pi * freq * np.arange(n) / RATE) * (np.arange(n) / RATE % 6 < 4)
    pts = 0
    for i in range(0, n, 4800):
        block = wave[i:i + 4800].astype(np.float32)
        frame = av.AudioFrame.from_ndarray(np.stack([block, block]), format="fltp", layout="stereo")
        frame.sample_rate, frame.pts = RATE, pts
        pts += block.size
        for f in resampler.resample(frame):
            yield from stream.encode(f)
    for f in resampler.resample(None):
        yield from stream.encode(f)
    yield from stream.encode(None)


def make_media(where: Path, *, fmt: str = "mp4", codec: str = "aac", seconds: float = 12.0, freqs=(440.0,),
               video: bool = False, audio: bool = True, movflags: str | None = None) -> bytes:
    """Encode a file on disk (inside the test's folder) and return its bytes."""
    path = where / f"fixture-{os.urandom(4).hex()}.bin"
    options = {"movflags": movflags} if movflags else {}
    if movflags == FRAGMENTED:
        options["frag_duration"] = "2000000"
    with av.open(str(path), "w", format=fmt, options=options) as out:
        streams = [out.add_stream(codec, rate=RATE, layout="stereo") for _ in freqs] if audio else []
        vs = None
        if video:
            vs = out.add_stream("mpeg4", rate=10)
            vs.width, vs.height, vs.pix_fmt = 64, 64, "yuv420p"
        pending = []
        for st, f in zip(streams, freqs):
            pending.extend(_encode_audio(out, st, f, seconds))
        if vs is not None:
            for i in range(int(seconds * 10)):
                image = np.full((64, 64, 3), (i * 5) % 255, dtype=np.uint8)
                frame = av.VideoFrame.from_ndarray(image, format="rgb24")
                frame.pts = i
                pending.extend(vs.encode(frame))
            pending.extend(vs.encode(None))
        pending = [p for p in pending if p.dts is not None]
        pending.sort(key=lambda p: float(p.dts * p.time_base))
        for p in pending:
            out.mux(p)
    data = path.read_bytes()
    path.unlink()
    return data


def set_enabled_tracks(data: bytes, enabled: list[bool]) -> bytes:
    """Rewrite each tkhd's flags: the mov demuxer turns the 'enabled' bit into the default disposition."""
    out, at = bytearray(data), 0
    for on in enabled:
        at = data.index(b"tkhd", at)
        out[at + 7] = (out[at + 7] & 0xFE) | int(on)
        at += 4
    return bytes(out)


def dominant_hz(audio: np.ndarray) -> float:
    return float(np.argmax(np.abs(np.fft.rfft(audio[:32000]))) * 16000 / 32000)


# ----- the app -----
def _services(cfg, fake_llm, **kw):
    from modwen.notify import Notifier
    from modwen.services import Services
    from modwen.stt.engines import FakeEngine

    engine = FakeEngine([("en", "So the release is Thursday, right?"), ("ar", "أيوه، بس محتاجين الـ QA يخلص قبلها.")])
    return Services(cfg, stt_engine_factory=lambda: engine, llm_http_client=fake_llm.client(),
                    notifier=Notifier(cfg), **kw)


@pytest.fixture()
def app(cfg, fake_llm):
    from modwen.api import create_app

    s = _services(cfg, fake_llm)
    c = TestClient(create_app(s, token="t"), raise_server_exceptions=False)
    c.cookies.set("mw_token", "t")
    return s, c


def _post(c, body: bytes, **params):
    params.setdefault("title", "Clip")
    return c.post("/api/meetings/import", params=params, content=body,
                  headers={"content-type": "application/octet-stream"})


def _wait(s, mid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        m = s.store.get_meeting(mid)
        if m["status"] in ("ready", "failed"):
            return m
        time.sleep(0.05)
    raise AssertionError(f"timeout; status={s.store.get_meeting(mid)['status']}")


def _audio(s, mid):
    from modwen.audio.chunks import read_channel

    return read_channel(s.cfg.audio_dir / mid, "system", s.keystore.unwrap(s.store.get_wrapped_key(mid)))


def _nothing_left(s):
    """No row at all (hidden ones count), no audio folder and no temp file."""
    with s.store._lock:
        assert s.store._conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 0
    assert not list(s.cfg.audio_dir.glob("*"))
    assert not list(s.cfg.data_dir.glob("import-*.sealed"))


def _refused(s, c, body, status, text, **params):
    r = _post(c, body, **params)
    assert r.status_code == status, r.text
    assert r.json()["detail"] == text
    _nothing_left(s)


def _record(s, title="Budget review.mp4"):
    from modwen.services import IMPORT_INTERRUPTED

    return s.store.create_meeting(title, status="failed", error=IMPORT_INTERRUPTED, platform="import")["id"]


# ----- AC1, AC2, AC4: successful imports -----
@pytest.mark.parametrize("fmt,codec,movflags", [
    ("mp4", "aac", None),
    ("mp4", "aac", "faststart"),
    ("mp4", "aac", FRAGMENTED),
    ("mov", "aac", None),
    ("ipod", "aac", None),
    ("ipod", "alac", None),
    ("mov", "pcm_s16le", None),
    ("mp4", "flac", None),
])
def test_supported_files_reach_ready_through_the_existing_pipeline(app, data_dir, fmt, codec, movflags):
    s, c = app
    body = make_media(data_dir, fmt=fmt, codec=codec, movflags=movflags)
    r = _post(c, body, title="مراجعة الميزانية", filename="مراجعة الميزانية.MP4")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["status"] == "processing" and m["title"] == "مراجعة الميزانية"  # AC2: the name without its extension
    assert not list(s.cfg.data_dir.glob("import-*.sealed"))
    done = _wait(s, m["id"])
    assert done["status"] == "ready", done["error"]
    assert s.store.get_segments(m["id"]) and s.store.get_speakers(m["id"]) and s.store.latest_notes(m["id"])
    audio = _audio(s, m["id"])
    assert abs(audio.size / 16000 - 12.0) < 0.2  # AC4: the length of the source's audio
    assert done["duration_ms"] == int(audio.size / 16)
    assert abs(dominant_hz(audio) - 440) < 5


def test_metadata_that_is_not_utf8_does_not_matter(app, data_dir):
    s, c = app
    body = make_media(data_dir)
    assert b"Lavf" in body[-40:]
    at = body.rindex(b"Lavf")
    assert _post(c, body[:at] + b"\xff\xfe\xc0\xc1" + body[at + 4:]).status_code == 200


def test_video_files_keep_only_the_audio_and_play(app, data_dir):
    s, c = app
    r = _post(c, make_media(data_dir, video=True, movflags="faststart"))
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    _wait(s, mid)
    assert sorted(p.name.split("-")[0] for p in (s.cfg.audio_dir / mid).iterdir()) == ["system"]
    assert all(p.name.endswith(".flac.enc") for p in (s.cfg.audio_dir / mid).iterdir())
    assert c.get(f"/api/meetings/{mid}/audio").status_code in (200, 206)  # AC4


def test_retry_and_export_work_as_for_an_audio_import(app, data_dir):
    s, c = app
    mid = _post(c, make_media(data_dir)).json()["id"]
    _wait(s, mid)
    assert c.get(f"/api/meetings/{mid}/export.md").status_code == 200
    assert c.post(f"/api/meetings/{mid}/retry").status_code == 200  # AC5
    assert _wait(s, mid)["status"] in ("processing", "ready")


# ----- AC3: which track -----
@pytest.mark.parametrize("enabled,expected", [([False, True], 880), ([False, False], 440), ([True, True], 440)])
def test_track_marked_default_else_the_first(app, data_dir, enabled, expected):
    s, c = app
    body = set_enabled_tracks(make_media(data_dir, freqs=(440.0, 880.0)), enabled)
    r = _post(c, body)
    assert r.status_code == 200, r.text
    assert abs(dominant_hz(_audio(s, r.json()["id"])) - expected) < 5


# ----- AC7, AC8: no audio, not usable -----
def test_video_with_no_audio_track(app, data_dir):
    s, c = app
    _refused(s, c, make_media(data_dir, audio=False, video=True), 400, NO_AUDIO)


@pytest.mark.parametrize("body", [b"definitely not audio" * 100, b"\x00\x00\x00\x08"])
def test_not_media(app, body):
    s, c = app
    _refused(s, c, body, 400, UNSUPPORTED)


def test_unsupported_codec_is_refused(app, data_dir):
    s, c = app
    _refused(s, c, make_media(data_dir, fmt="mov", codec="adpcm_ima_qt"), 400, UNSUPPORTED)


def test_codec_with_no_decoder_is_refused(app, data_dir):
    s, c = app
    body = make_media(data_dir).replace(b"mp4a", b"ac-4", 1).replace(b"esds", b"xxxx", 1)
    _refused(s, c, body, 400, UNSUPPORTED)


@pytest.mark.parametrize("entry", [b"enca", b"drms"])
@pytest.mark.parametrize("movflags", [None, "faststart"])
def test_copy_protected_audio_is_refused(app, data_dir, entry, movflags):
    s, c = app
    clear = make_media(data_dir, movflags=movflags)
    at = clear.index(b"stsd")
    body = clear[:at] + clear[at:].replace(b"mp4a", entry, 1)
    _refused(s, c, body, 400, UNSUPPORTED)


def test_copy_protection_check_does_not_touch_clear_files(data_dir):
    from modwen.audio.importer import SealedTempFile, is_encrypted

    for kw in ({}, {"fmt": "mov"}, {"fmt": "ipod", "codec": "alac"}, {"video": True, "movflags": "faststart"},
               {"movflags": FRAGMENTED}):
        sealed = SealedTempFile(data_dir)
        sealed.append(make_media(data_dir, seconds=2, **kw))
        assert not is_encrypted(sealed)
        sealed.close()


def test_copy_protection_check_survives_broken_boxes(data_dir):
    from modwen.audio.importer import SealedTempFile, is_encrypted

    for junk in (b"\x00\x00\x00\x01moov", b"\x00\x00\x00\x03moov" * 10, b"\x00\x00\x00\x00moov", b"\x00\x00\xff\xffmoov"):
        sealed = SealedTempFile(data_dir)
        sealed.append(junk)
        assert not is_encrypted(sealed)
        sealed.close()


def test_a_large_box_size_uses_the_64_bit_field(data_dir):
    from modwen.audio.importer import SealedTempFile, is_encrypted

    entry = (24).to_bytes(4, "big") + b"enca" + bytes(16)
    stsd = (16 + len(entry)).to_bytes(4, "big") + b"stsd" + bytes(8) + entry
    for kind in (b"stbl", b"minf", b"mdia", b"trak"):
        stsd = (8 + len(stsd)).to_bytes(4, "big") + kind + stsd
    moov = (1).to_bytes(4, "big") + b"moov" + (16 + len(stsd)).to_bytes(8, "big") + stsd
    sealed = SealedTempFile(data_dir)
    sealed.append(moov)
    assert is_encrypted(sealed)
    sealed.close()


# ----- AC9: damaged and cut off -----
@pytest.mark.parametrize("kw,keep", [
    ({}, 0.5),  # moov at the end: it is gone
    ({"movflags": "faststart"}, 0.5),
    ({"movflags": "faststart", "video": True}, 0.5),  # a quiet cut: no error, only a shortfall
    ({"movflags": "faststart", "video": True}, 0.9),
    ({"movflags": "faststart", "fmt": "mov", "codec": "pcm_s16le"}, 0.9),
    ({"movflags": FRAGMENTED}, 0.5),
    ({"movflags": FRAGMENTED}, 0.97),
])
def test_cut_off_files_are_refused_whole(app, data_dir, kw, keep):
    s, c = app
    body = make_media(data_dir, seconds=20, **kw)
    _refused(s, c, body[:int(len(body) * keep)], 400, DAMAGED)


def test_corrupt_bytes_are_refused(app, data_dir):
    s, c = app
    body = bytearray(make_media(data_dir, seconds=20, movflags="faststart"))
    middle = len(body) // 2
    body[middle:middle + 2000] = bytes(b ^ 0xFF for b in body[middle:middle + 2000])
    _refused(s, c, bytes(body), 400, DAMAGED)


def test_damaged_moov_is_damaged_not_no_audio(app, data_dir):
    s, c = app
    _refused(s, c, make_media(data_dir).replace(b"trak", b"xxxx"), 400, DAMAGED)


def test_a_cut_on_a_fragment_boundary_imports_as_shorter(app, data_dir):
    """The limit the user accepted (AC9): such a file is valid, and nothing in it shows that more was expected."""
    s, c = app
    body = make_media(data_dir, seconds=20, movflags=FRAGMENTED)
    starts = [i - 4 for i in range(len(body)) if body[i:i + 4] == b"moof"]
    assert len(starts) >= 3, "the fixture needs several fragments"
    r = _post(c, body[:starts[len(starts) // 2]])
    assert r.status_code == 200, r.text
    assert 1.0 < _audio(s, r.json()["id"]).size / 16000 < 19


def test_a_change_of_format_mid_stream_is_refused(data_dir, monkeypatch):
    from modwen.audio import importer
    from modwen.audio.chunks import ChunkWriter

    def frame(rate):
        data = np.zeros((1, 1024), dtype=np.float32)
        f = av.AudioFrame.from_ndarray(data, format="fltp", layout="mono")
        f.sample_rate = rate
        return f

    packets = [SimpleNamespace(is_corrupt=False, decode=lambda r=r: [frame(r)]) for r in (44100, 22050)]
    container = SimpleNamespace(demux=lambda st: iter(packets))
    writer = ChunkWriter(data_dir / "m", "system", b"k" * 32)
    with pytest.raises(importer.ImportRefused, match="damaged"):
        importer._decode_track(container, None, writer, 3600)


# ----- AC10, AC11, AC12: limits -----
def test_length_message_wording():
    from modwen.audio.importer import too_long_message

    def hours(text):
        return text.split()[3]

    assert hours(too_long_message(6.04)) == "6.1"
    assert hours(too_long_message(6.1)) == "6.1"
    assert hours(too_long_message(7.0)) == "7"
    assert hours(too_long_message(10.5)) == "10.5"
    assert too_long_message(7.0) == ("This recording is 7 hours long. Imports are limited to 6 hours. "
                                     "Trim or split it, then import it again.")


def test_declared_length_over_the_limit_is_refused(app, data_dir, monkeypatch):
    from modwen.audio import importer

    s, c = app
    monkeypatch.setattr(importer, "MAX_IMPORT_HOURS", 5 / 3600)
    body = make_media(data_dir, seconds=360)
    _refused(s, c, body, 400, importer.too_long_message(0.1))


def test_exactly_the_limit_imports(app, data_dir, monkeypatch):
    from modwen.audio import importer

    s, c = app
    body = make_media(data_dir, seconds=12)
    monkeypatch.setattr(importer, "MAX_IMPORT_HOURS", 12 / 3600)
    assert _post(c, body).status_code == 200


def test_length_is_also_stopped_while_decoding(data_dir):
    """A file may state no length, or a false one."""
    from modwen.audio import importer
    from modwen.audio.chunks import ChunkWriter

    sealed = importer.SealedTempFile(data_dir)
    sealed.append(make_media(data_dir, seconds=12))
    sealed.seek(0)
    writer = ChunkWriter(data_dir / "m", "system", b"k" * 32)
    with av.open(sealed, format="mov") as container:
        with pytest.raises(importer.ImportRefused, match="Imports are limited"):
            importer._decode_track(container, container.streams.audio[0], writer, 5)
    assert writer.total_samples < 16000 * 6
    sealed.close()


def test_audio_formats_use_the_same_length_message(app, monkeypatch):
    from modwen.audio import importer

    s, c = app
    buf = io.BytesIO()
    sf.write(buf, np.zeros(16000 * 360, dtype=np.float32), 16000, format="WAV")
    monkeypatch.setattr(importer, "MAX_IMPORT_HOURS", 5 / 3600)
    _refused(s, c, buf.getvalue(), 400, importer.too_long_message(0.1))


def test_upload_over_the_size_limit(app, monkeypatch):
    import modwen.api as api

    s, c = app
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 100)
    _refused(s, c, b"x" * 101, 413, TOO_BIG)


def test_empty_files(app, data_dir):
    s, c = app
    _refused(s, c, b"", 400, EMPTY)
    buf = io.BytesIO()
    sf.write(buf, np.zeros(0, dtype=np.float32), 16000, format="WAV")
    _refused(s, c, buf.getvalue(), 400, EMPTY)


def test_an_audio_track_with_no_frames_is_empty(app, data_dir, monkeypatch):
    """A muxer won't write such a track, so the decoder's answer is stood in for."""
    from modwen.audio import importer

    s, c = app
    body = make_media(data_dir)
    monkeypatch.setattr(importer, "_pick_audio", lambda container: SimpleNamespace(duration=None))
    monkeypatch.setattr(importer, "_decode_track", lambda *a: 0.0)
    _refused(s, c, body, 400, EMPTY)


# ----- AC13: failure while running -----
@pytest.mark.parametrize("media", [True, False])
def test_failure_while_decoding_shows_the_general_message(app, data_dir, monkeypatch, media):
    from modwen.audio.chunks import ChunkWriter

    s, c = app
    buf = io.BytesIO()
    sf.write(buf, speechy([(2.0, 1.0)]), 16000, format="WAV")
    body = make_media(data_dir) if media else buf.getvalue()

    def full(self, frames):
        raise OSError(28, "No space left on device: C:\\secret\\path")

    monkeypatch.setattr(ChunkWriter, "write", full)
    _refused(s, c, body, 400, GENERAL)


def test_failure_while_storing_the_upload_is_json(app, monkeypatch):
    from modwen.audio.importer import SealedTempFile

    s, c = app

    def full(self, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(SealedTempFile, "append", full)
    _refused(s, c, b"x" * 100, 500, GENERAL)


def test_an_import_that_lost_its_row_is_not_a_success(app, data_dir, monkeypatch):
    """Another Modwen process ran recovery meanwhile: the conditional commit stops a false success."""
    s, c = app
    real = s.store.commit_import

    def stolen(mid, title, duration_ms):
        s.store.update_meeting(mid, status="failed")
        return real(mid, title, duration_ms)

    monkeypatch.setattr(s.store, "commit_import", stolen)
    _refused(s, c, make_media(data_dir), 400, GENERAL)


# ----- AC13: the marker and recovery -----
def _seed_interrupted(s, title="Budget review.mp4", key=True):
    from modwen.audio.chunks import ChunkWriter

    data_key, wrapped = s.keystore.new_data_key()
    m = s.store.create_meeting(title, status="importing", platform="import", wrapped_key=wrapped if key else None)
    if key:
        w = ChunkWriter(s.cfg.audio_dir / m["id"], "system", data_key, 1)
        w.write(np.zeros(16000 * 3, dtype=np.float32))
        w.close()
    return m["id"]


def test_importing_rows_are_hidden_from_every_list(app):
    s, c = app
    _seed_interrupted(s)
    assert s.store.list_meetings() == []
    assert c.get("/api/meetings").json()["meetings"] == []
    assert c.get("/api/tray").json()["processing"] == 0
    assert len(s.store.interrupted_imports()) == 1


@pytest.mark.parametrize("key", [True, False])
def test_startup_turns_a_cut_off_import_into_a_named_record(cfg, fake_llm, key):
    from modwen.api import create_app
    from modwen.services import IMPORT_INTERRUPTED

    s = _services(cfg, fake_llm, start_worker=False)
    mid = _seed_interrupted(s, key=key)
    (cfg.data_dir / "import-left.sealed").write_bytes(b"noise")

    s2 = _services(cfg, fake_llm)
    m = s2.store.get_meeting(mid)
    assert m["status"] == "failed" and m["error"] == IMPORT_INTERRUPTED and m["error"].startswith("import:")
    assert m["title"] == "Budget review.mp4"
    assert not (cfg.audio_dir / mid).exists() and s2.store.get_wrapped_key(mid) is None
    assert not list(cfg.data_dir.glob("import-*.sealed"))
    assert not s2.is_queued_or_running(mid) and not s2.store.get_segments(mid) and not s2.store.latest_notes(mid)
    assert [x["id"] for x in s2.store.list_meetings()] == [mid]
    c = TestClient(create_app(s2, token="t"))
    c.cookies.set("mw_token", "t")
    assert c.get(f"/api/meetings/{mid}").json()["meeting"]["error"].startswith("import:")
    s3 = _services(cfg, fake_llm)  # a second start changes nothing
    assert s3.store.get_meeting(mid)["status"] == "failed"


def test_recovery_sends_no_notification_or_event(cfg, fake_llm):
    s = _services(cfg, fake_llm, start_worker=False)
    _seed_interrupted(s)
    s2 = _services(cfg, fake_llm, start_worker=False)
    events = []
    s2.bus.publish = events.append
    s2.notifier.sent.clear()
    s2._recover_imports()
    assert events == [] and s2.notifier.sent == []


def test_the_rest_of_recovery_is_unchanged(cfg, fake_llm):
    from modwen.api import import_audio

    s = _services(cfg, fake_llm, start_worker=False)
    done = import_audio(s, speechy(SEGMENTS), title="Completed import")["id"]  # left in processing
    empty = s.store.create_meeting("Live, nothing saved", status="recording")["id"]
    cut = _seed_interrupted(s)
    s2 = _services(cfg, fake_llm)
    assert _wait(s2, done)["status"] == "ready"
    assert s2.store.get_meeting(empty)["status"] == "failed"
    assert not s2.store.get_meeting(empty)["error"].startswith("import:")
    assert s2.store.get_meeting(cut)["error"].startswith("import:")


CRASH = """
import os, sys
from fastapi.testclient import TestClient
from modwen.api import create_app
from modwen.audio.chunks import ChunkWriter
from modwen.audio.importer import SealedTempFile
from modwen.config import Config
from modwen.services import Services

where, body = sys.argv[1], open(sys.argv[2], "rb").read()
cfg = Config()
s = Services(cfg, start_worker=False)
def die(*a, **k):
    os._exit(9)
if where == "upload":
    SealedTempFile.append = die
elif where == "decode":
    real, n = ChunkWriter.write, [0]
    def write(self, frames):
        n[0] += 1
        if n[0] > 200:
            die()
        real(self, frames)
    ChunkWriter.write = write
elif where == "commit":
    s.store.commit_import = die
elif where == "committed":
    s.enqueue_processing = die
c = TestClient(create_app(s, token="t"))
c.cookies.set("mw_token", "t")
c.post("/api/meetings/import", params={"title": "Clip", "filename": "Clip.mp4"}, content=body,
       headers={"content-type": "application/octet-stream"})
os._exit(0)
"""


@pytest.mark.parametrize("where", ["upload", "decode", "commit", "committed"])
def test_a_real_crash_at_each_stage(cfg, fake_llm, data_dir, where):
    body = make_media(data_dir, seconds=30, video=True)
    path = data_dir / "input.bin"
    path.write_bytes(body)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, "-c", CRASH, where, str(path)], cwd=data_dir, env=env, capture_output=True,
                       timeout=120)
    assert p.returncode == 9, p.stderr.decode(errors="replace")
    path.unlink()

    s = _services(cfg, fake_llm)
    rows = s.store.list_meetings()
    assert len(rows) == 1
    m = rows[0]
    if where == "committed":  # complete: the existing recovery carries on
        assert m["title"] == "Clip" and _wait(s, m["id"])["status"] == "ready"
        assert _audio(s, m["id"]).size / 16000 > 29
    else:
        assert m["title"] == "Clip.mp4" and m["status"] == "failed" and m["error"].startswith("import:")
        assert not (cfg.audio_dir / m["id"]).exists() and s.store.get_wrapped_key(m["id"]) is None
    assert not list(cfg.data_dir.glob("import-*.sealed"))


def test_tab_closed_during_upload_leaves_a_record_at_once(app):
    from modwen.api import create_app

    s, _ = app
    events = []
    s.bus.publish = events.append
    application = create_app(s, token="t")
    incoming = [{"type": "http.request", "body": b"x" * 1000, "more_body": True}, {"type": "http.disconnect"}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/api/meetings/import", "raw_path": b"/api/meetings/import",
             "query_string": b"title=Clip&filename=Clip.mp4", "root_path": "", "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 80),
             "headers": [(b"host", b"127.0.0.1"), (b"x-modwen-token", b"t"), (b"content-type", b"video/mp4")]}
    asyncio.run(application(scope, receive, send))
    rows = s.store.list_meetings()
    assert [(m["title"], m["status"]) for m in rows] == [("Clip.mp4", "failed")]
    assert rows[0]["error"].startswith("import:")
    assert any(e.get("type") == "meetings_changed" and e.get("meeting_id") == rows[0]["id"] for e in events)
    assert not list(s.cfg.data_dir.glob("import-*.sealed"))


# ----- AC13: what a record allows -----
def test_a_record_cannot_be_reprocessed(app):
    s, c = app
    mid = _record(s)
    assert c.post(f"/api/meetings/{mid}/retry").status_code == 409
    assert c.post(f"/api/meetings/{mid}/regenerate", json={}).status_code == 409
    assert c.post(f"/api/meetings/{mid}/speakers/redetect", json={}).status_code == 409
    assert s.store.get_meeting(mid)["status"] == "failed"
    assert not s.is_queued_or_running(mid)


def test_a_running_import_cannot_be_touched(app):
    s, c = app
    mid = _seed_interrupted(s)
    for call in (c.post(f"/api/meetings/{mid}/retry"), c.post(f"/api/meetings/{mid}/regenerate", json={}),
                 c.post(f"/api/meetings/{mid}/speakers/redetect", json={}), c.delete(f"/api/meetings/{mid}"),
                 c.put(f"/api/meetings/{mid}/notes", json={"data": {}})):
        assert call.status_code == 409, call.text
    assert s.store.get_meeting(mid)["status"] == "importing"


def test_ordinary_failures_can_still_be_retried(app):
    from modwen.api import import_audio

    s, c = app
    mid = import_audio(s, speechy(SEGMENTS), title="Flaky")["id"]
    _wait(s, mid)
    s.store.update_meeting(mid, status="failed", error="notes: The AI model did not answer")
    assert c.post(f"/api/meetings/{mid}/retry").status_code == 200


def test_a_record_can_be_deleted(app):
    s, c = app
    mid = _record(s)
    assert c.delete(f"/api/meetings/{mid}").status_code == 200
    assert s.store.get_meeting(mid) is None


# ----- AC13: Import again -----
def _changes(s):
    events = []
    real = s.bus.publish

    def spy(e):
        events.append(e)
        real(e)

    s.bus.publish = spy
    return events


def test_import_again_removes_only_that_record(app, data_dir):
    s, c = app
    old, twin = _record(s), _record(s)
    other = _record(s, "Other.mp4")
    events = _changes(s)
    r = _post(c, make_media(data_dir), title="Budget review", filename="Budget review.mp4", replaces=old)
    assert r.status_code == 200, r.text
    assert s.store.get_meeting(old) is None
    assert s.store.get_meeting(twin) and s.store.get_meeting(other)  # same name, still there: the id decides
    assert {"type": "meetings_changed", "meeting_id": old, "change": "deleted"} in events
    assert r.json()["id"] != old


def test_import_again_with_a_different_file_still_clears_the_record(app, data_dir):
    s, c = app
    old = _record(s, "Budget review.mp4")
    assert _post(c, make_media(data_dir), title="Something else", replaces=old).status_code == 200
    assert s.store.get_meeting(old) is None


def test_a_failed_import_again_keeps_the_record(app, monkeypatch):
    from modwen.audio.importer import SealedTempFile

    s, c = app
    old = _record(s)
    assert _post(c, b"definitely not audio" * 100, replaces=old).status_code == 400
    assert _post(c, b"", replaces=old).status_code == 400
    assert s.store.get_meeting(old)

    def full(self, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(SealedTempFile, "append", full)
    assert _post(c, b"x" * 10, replaces=old).status_code == 500
    assert s.store.get_meeting(old)


@pytest.mark.parametrize("target", ["missing", "ordinary-failed", "ready", "importing"])
def test_replaces_never_removes_anything_else(app, data_dir, target):
    s, c = app
    ids = {
        "missing": "no-such-id",
        "ordinary-failed": s.store.create_meeting("Standup", status="failed", error="notes: no model")["id"],
        "ready": s.store.create_meeting("Standup", status="ready")["id"],
        "importing": _seed_interrupted(s),
    }
    r = _post(c, make_media(data_dir), replaces=ids[target])
    assert r.status_code == 200, r.text
    if target != "missing":
        assert s.store.get_meeting(ids[target])


def test_replaces_failure_is_logged_by_id_and_the_import_succeeds(app, data_dir, monkeypatch, caplog):
    s, c = app
    old = _record(s, "Private name.mp4")
    real = s.store.delete_meeting

    def boom(mid):
        if mid == old:
            raise RuntimeError("database is locked")
        real(mid)

    monkeypatch.setattr(s.store, "delete_meeting", boom)
    r = _post(c, make_media(data_dir), replaces=old)
    assert r.status_code == 200, r.text
    assert old in caplog.text and "Private name" not in caplog.text


# ----- AC15, AC16, AC17: privacy -----
def test_nothing_readable_on_disk_during_or_after_an_import(app, data_dir, monkeypatch):
    from modwen.audio.chunks import ChunkWriter

    s, c = app
    body = make_media(data_dir, video=True, movflags="faststart")
    data_dir_before = set(data_dir.rglob("*"))
    tmp = Path(tempfile.gettempdir())
    tmp_before = set(tmp.iterdir())
    seen = []
    real = ChunkWriter.write

    def spy(self, frames):
        for p in data_dir.rglob("*"):
            if p.is_file() and p.name.endswith((".sealed",)):
                head = p.read_bytes()[:64]
                assert b"ftyp" not in head and head != body[:64]
                seen.append(p.name)
        real(self, frames)

    monkeypatch.setattr(ChunkWriter, "write", spy)
    r = _post(c, body)
    assert r.status_code == 200 and seen
    new = [p for p in set(data_dir.rglob("*")) - data_dir_before if p.is_file()]
    assert all(p.name.endswith(".flac.enc") for p in new if p.parent.parent == s.cfg.audio_dir)
    assert not list(data_dir.glob("import-*.sealed"))
    fragment = body[len(body) // 2:len(body) // 2 + 64]
    for p in new:
        assert fragment not in p.read_bytes()
    assert not [p for p in set(tmp.iterdir()) - tmp_before
                if p.is_file() and p.stat().st_size and body[:64] in p.read_bytes()]


def test_no_connection_is_made(app, data_dir, monkeypatch):
    import socket

    s, c = app
    body = make_media(data_dir)
    calls = []
    real = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: (calls.append(addr), real(self, addr))[1])
    assert _post(c, body).status_code == 200
    assert [a for a in calls if a and a[0] not in ("127.0.0.1", "::1", "localhost")] == []


def test_logs_hold_no_meeting_content(app, data_dir, caplog, capfd):
    import logging

    s, c = app
    caplog.set_level(logging.DEBUG)
    name = "Secret merger.mp4"
    good = make_media(data_dir)
    assert _post(c, good, title="Secret merger", filename=name).status_code == 200
    assert _post(c, good[:len(good) // 2], title="Secret merger", filename=name).status_code == 400
    assert _post(c, b"junk" * 100, title="Secret merger", filename=name).status_code == 400
    _nothing = capfd.readouterr()
    assert "Secret merger" not in caplog.text
    assert "Secret merger" not in _nothing.out + _nothing.err


# ----- AC19, AC20: today's formats and callers -----
@pytest.mark.parametrize("fmt,subtype,ext", [("WAV", "PCM_16", "wav"), ("FLAC", "PCM_16", "flac"),
                                             ("OGG", "VORBIS", "ogg"), ("AIFF", "PCM_16", "aiff"),
                                             ("MP3", "MPEG_LAYER_III", "mp3")])
def test_audio_formats_stay_on_their_path(app, monkeypatch, fmt, subtype, ext):
    from modwen.audio import importer
    from modwen.audio.chunks import ChunkWriter

    s, c = app
    buf = io.BytesIO()
    sf.write(buf, speechy([(3.0, 1.0)]), 16000, format=fmt, subtype=subtype)
    body = buf.getvalue()
    monkeypatch.setattr(importer, "decode_media_into", lambda *a: pytest.fail("took the media path"))
    r = _post(c, body)
    assert r.status_code == 200, r.text

    sealed = importer.SealedTempFile(s.cfg.data_dir)
    sealed.append(body)
    key = b"k" * 32
    importer.decode_into(sealed, ChunkWriter(s.cfg.audio_dir / "same", "system", key, s.cfg.capture.chunk_seconds))
    sealed.close()
    from modwen.audio.chunks import read_channel

    expected = read_channel(s.cfg.audio_dir / "same", "system", key)
    assert np.array_equal(_audio(s, r.json()["id"]), expected)


def test_routing_reads_the_first_bytes_not_a_name(data_dir):
    from modwen.audio.importer import SealedTempFile, is_media_container

    def route(body):
        sealed = SealedTempFile(data_dir)
        sealed.append(body)
        try:
            return is_media_container(sealed)
        finally:
            sealed.close()

    for box in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"styp", b"moof"):
        assert route(b"\x00\x00\x00\x20" + box + bytes(40))
    assert not route(b"RIFF" + b"free" + b"WAVEfmt " + bytes(40))  # a size field that happens to spell a box
    assert not route(b"FORM" + b"wide" + b"AIFF" + bytes(40))
    assert not route(b"short") and not route(b"")


def test_todays_callers_are_unchanged(app, data_dir):
    s, c = app
    buf = io.BytesIO()
    sf.write(buf, speechy([(2.0, 1.0)]), 16000, format="WAV")
    r = c.post("/api/meetings/import?title=Uploaded&meeting_language=auto", content=buf.getvalue())
    assert r.status_code == 200 and r.json()["title"] == "Uploaded"
    assert c.post("/api/meetings/import", files={"file": ("a.wav", buf.getvalue())}).status_code == 415
    assert c.post("/api/meetings/import?channel=nope", content=b"x").status_code == 400
    assert c.post("/api/meetings/import?meeting_language=fr", content=b"x").status_code == 400
    assert len(s.store.list_meetings()) == 1
    assert not s.store.interrupted_imports()
    json.dumps(r.json())  # a plain meeting, as before
