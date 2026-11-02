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
_encoded: dict[tuple, bytes] = {}
_empty_db: bytes | None = None


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
    """The bytes of a synthetic file. Each is encoded once per run: that is slow, and bytes can't be changed."""
    key = (fmt, codec, seconds, tuple(freqs), video, audio, movflags)
    if key not in _encoded:
        _encoded[key] = _encode(where, *key)
    return _encoded[key]


def _encode(where, fmt, codec, seconds, freqs, video, audio, movflags) -> bytes:
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


def top_boxes(data: bytes) -> list[tuple[int, int, bytes]]:
    """(offset, size, type) of each top-level box."""
    at, out = 0, []
    while at + 8 <= len(data):
        size = int.from_bytes(data[at:at + 4], "big")
        out.append((at, size, data[at + 4:at + 8]))
        at += size
    return out


def box_of(data: bytes, kind: bytes, nth: int = 0) -> tuple[int, int, bytes]:
    return [b for b in top_boxes(data) if b[2] == kind][nth]


def packet_ends(data: bytes) -> list[int]:
    with av.open(io.BytesIO(data)) as container:
        return sorted(p.pos + p.size for p in container.demux(container.streams.audio[0]) if p.size)


def open_ended_mdat(data: bytes) -> bytes:
    """A moov-first file whose mdat says "to the end of the file" (size 0): its size can't show a cut."""
    at = box_of(data, b"mdat")[0]
    return data[:at] + bytes(4) + data[at + 4:]


def two_mdats(data: bytes, last: int) -> tuple[bytes, int]:
    """Split a moov-first file's audio into two mdats, the second holding the ``last`` packets. Returns the file and
    the offset where the second mdat starts: a cut there ends on a clean box boundary."""
    with av.open(io.BytesIO(data)) as container:
        packets = [p.pos for p in container.demux(container.streams.audio[0]) if p.size]
    mdat_at, mdat_size, _ = box_of(data, b"mdat")
    first, second = data[mdat_at + 8:packets[-last]], data[packets[-last]:mdat_at + mdat_size]
    moov_at, moov_size, _ = box_of(data, b"moov")
    moov = data[moov_at:moov_at + moov_size]

    def words(*values: int) -> bytes:
        return b"".join(v.to_bytes(4, "big") for v in values)

    for kind, body in ((b"stsc", words(0, 2, 1, len(packets) - last, 1, 2, last, 1)), (b"stco", words(0, 2, 0, 0))):
        at = moov.index(kind) - 4
        moov = moov[:at] + (8 + len(body)).to_bytes(4, "big") + kind + body + moov[at + int.from_bytes(moov[at:at + 4], "big"):]
    grown = bytearray(moov)
    for kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
        at = moov.index(kind) - 4
        grown[at:at + 4] = (int.from_bytes(moov[at:at + 4], "big") + len(moov) - moov_size).to_bytes(4, "big")
    head = moov_at + len(grown)
    offsets = moov.index(b"stco") + 12
    grown[offsets:offsets + 8] = words(head + 8, head + 8 + len(first) + 8)
    mdats = (8 + len(first)).to_bytes(4, "big") + b"mdat" + first + (8 + len(second)).to_bytes(4, "big") + b"mdat" + second
    return data[:moov_at] + bytes(grown) + mdats, head + 8 + len(first)


def make_gapped(where: Path, movflags: str | None = None) -> bytes:
    """A valid file with a 3 s hole in its audio timeline, 10 s in."""
    path = where / f"fixture-{os.urandom(4).hex()}.mp4"
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(1024) / RATE)).astype(np.float32)
    with av.open(str(path), "w", format="mp4", options={"movflags": movflags} if movflags else {}) as out:
        st = out.add_stream("aac", rate=RATE, layout="stereo")
        for i in range(int(20 * RATE / 1024)):
            frame = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
            frame.sample_rate, frame.pts = RATE, i * 1024 + (3 * RATE if i * 1024 >= 10 * RATE else 0)
            for packet in st.encode(frame):
                out.mux(packet)
        for packet in st.encode(None):
            out.mux(packet)
    data = path.read_bytes()
    path.unlink()
    return data


# ----- the app -----
def _start_from_empty_db(cfg) -> None:
    """Creating the database takes seconds on some disks, and nearly every test here needs a new one."""
    global _empty_db
    from modwen.storage import Store

    if cfg.db_path.exists():
        return
    if _empty_db is None:
        Store(cfg.db_path).close()
        _empty_db = cfg.db_path.read_bytes()
    cfg.db_path.write_bytes(_empty_db)


def _services(cfg, fake_llm, **kw):
    from modwen.notify import Notifier
    from modwen.services import Services
    from modwen.stt.engines import FakeEngine

    _start_from_empty_db(cfg)
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
    ("mov", "pcm_s24be", None),
    ("mov", "pcm_f32be", None),
    ("mov", "pcm_s32le", None),
    ("mov", "pcm_s32be", None),
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


@pytest.mark.parametrize("past", [3, 8, 40])
def test_a_cut_just_past_a_fragment_boundary_is_refused(app, data_dir, past):
    s, c = app
    body = make_media(data_dir, seconds=20, movflags=FRAGMENTED)
    _refused(s, c, body[:box_of(body, b"moof", 1)[0] + past], 400, DAMAGED)


def test_a_cut_between_a_moof_and_its_mdat_is_refused(app, data_dir):
    s, c = app
    body = make_media(data_dir, seconds=20, movflags=FRAGMENTED)
    moof = box_of(body, b"moof", 1)
    _refused(s, c, body[:moof[0] + moof[1]], 400, DAMAGED)


@pytest.mark.parametrize("kw", [{"fmt": "ipod"}, {"fmt": "mov", "codec": "pcm_s16le"}, {"video": True}])
@pytest.mark.parametrize("lost", [1, 10, 47])
def test_a_cut_on_a_packet_boundary_is_refused(app, data_dir, kw, lost):
    s, c = app
    body = make_media(data_dir, seconds=20, movflags="faststart", **kw)
    _refused(s, c, body[:packet_ends(body)[-lost - 1]], 400, DAMAGED)


@pytest.mark.parametrize("video", [False, True])
def test_a_cut_before_the_mdat_is_refused(app, data_dir, video):
    """Every box ends cleanly and nothing errors: only the file's own timeline shows what is missing."""
    s, c = app
    body = make_media(data_dir, seconds=20, movflags="faststart", video=video)
    _refused(s, c, body[:box_of(body, b"mdat")[0]], 400, DAMAGED)


def test_a_very_short_file_cut_before_its_mdat_is_damaged_not_empty(app, data_dir):
    s, c = app
    body = make_media(data_dir, seconds=0.5, movflags="faststart")
    _refused(s, c, body[:box_of(body, b"mdat")[0]], 400, DAMAGED)


@pytest.mark.parametrize("movflags", [None, FRAGMENTED])
def test_a_cut_that_loses_no_audio_is_still_refused(app, data_dir, movflags):
    """The end of a moov-last moov, or of the trailing mfra. The file is incomplete, so it is refused whole."""
    s, c = app
    body = make_media(data_dir, seconds=20, movflags=movflags)
    _refused(s, c, body[:-1], 400, DAMAGED)


def test_a_cut_before_a_fragmented_files_first_fragment_is_damaged_not_empty(app, data_dir):
    s, c = app
    body = make_media(data_dir, seconds=20, movflags=FRAGMENTED)
    _refused(s, c, body[:box_of(body, b"moof")[0]], 400, DAMAGED)


@pytest.mark.parametrize("kw", [{}, {"fmt": "mov", "codec": "pcm_s16le"}])
@pytest.mark.parametrize("lost", [1, 2, 5])
def test_an_open_ended_mdat_shows_every_missing_packet(app, data_dir, kw, lost):
    """No time margin: each sample the table lists must arrive."""
    s, c = app
    good = make_media(data_dir, seconds=20, movflags="faststart", **kw)
    _refused(s, c, open_ended_mdat(good)[:packet_ends(good)[-lost - 1]], 400, DAMAGED)


@pytest.mark.parametrize("last", [1, 5, 100])
def test_several_mdats_import_whole_and_show_a_cut_between_them(app, data_dir, last):
    s, c = app
    body, second = two_mdats(make_media(data_dir, seconds=20, movflags="faststart"), last)
    _refused(s, c, body[:second], 400, DAMAGED)
    _refused(s, c, body[:second + 3], 400, DAMAGED)  # part of a header: ignored as a remainder, but packets are lost
    r = _post(c, body)
    assert r.status_code == 200, r.text
    assert abs(_audio(s, r.json()["id"]).size / 16000 - 20) < 0.2


@pytest.mark.parametrize("edit", [{"trim_end": 5 * RATE}, {"skip": 5 * RATE}])
def test_an_edit_list_can_drop_samples_from_what_is_expected(data_dir, edit):
    """Why the check counts FFmpeg's index and not ``st.frames``: this intact file demuxes fewer samples than it lists."""
    with av.open(io.BytesIO(_patch_elst(make_media(data_dir, seconds=20), **edit)), format="mov") as container:
        track = container.streams.audio[0]
        expected = len(track.index_entries)
        assert expected == sum(1 for p in container.demux(track) if p.dts is not None)
        assert expected < track.frames


def test_pcm_groups_its_samples_and_the_index_counts_the_groups(data_dir):
    with av.open(io.BytesIO(make_media(data_dir, seconds=20, fmt="mov", codec="pcm_s16le")), format="mov") as container:
        track = container.streams.audio[0]
        expected = len(track.index_entries)
        assert expected == sum(1 for p in container.demux(track) if p.dts is not None)
        assert track.frames == 20 * RATE > expected


TRAILING = [bytes(1), bytes(7), b"END", b"TAG" + bytes(125), b"\xff" * 32, b"recorded by device firmware 1.2.3\n......"]


@pytest.mark.parametrize("tail", TRAILING, ids=lambda t: f"{len(t)}b")
def test_bytes_after_the_last_box_do_not_refuse_a_whole_file(app, data_dir, tail):
    s, c = app
    r = _post(c, make_media(data_dir, seconds=20) + tail)
    assert r.status_code == 200, r.text
    assert abs(_audio(s, r.json()["id"]).size / 16000 - 20) < 0.2


@pytest.mark.parametrize("kw", [{"fmt": "mov"}, {"fmt": "ipod"}, {"video": True}, {"movflags": "faststart"}])
@pytest.mark.parametrize("tail", [bytes(3), b"TAG" + bytes(125)], ids=lambda t: f"{len(t)}b")
def test_trailing_bytes_are_ignored_in_each_container(app, data_dir, kw, tail):
    s, c = app
    r = _post(c, make_media(data_dir, seconds=20, **kw) + tail)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("kind", [b"moov", b"mdat", b"moof", b"mfra", b"free", b"skip", b"uuid", b"udta", b"ftyp",
                                  b"wide", b"meta"])
def test_a_known_box_running_past_the_end_is_still_damage(app, data_dir, kind):
    s, c = app
    tail = (1000).to_bytes(4, "big") + kind + bytes(20)
    _refused(s, c, make_media(data_dir, seconds=20) + tail, 400, DAMAGED)


@pytest.mark.parametrize("movflags,kind", [("faststart", b"mdat"), (None, b"moov")])
def test_a_remainder_is_only_ignored_once_the_audio_and_moov_are_whole(app, data_dir, movflags, kind):
    s, c = app
    body = make_media(data_dir, seconds=20, movflags=movflags)
    _refused(s, c, body[:box_of(body, kind)[0] + 3], 400, DAMAGED)


@pytest.mark.parametrize("tail", [bytes(3), b"END", b"TAG" + bytes(125), b"\xff" * 32], ids=lambda t: f"{len(t)}b")
def test_fragmented_files_stay_strict_about_a_remainder(app, data_dir, tail):
    """A short remainder there can be a lost fragment."""
    s, c = app
    _refused(s, c, make_media(data_dir, seconds=20, movflags=FRAGMENTED) + tail, 400, DAMAGED)


def test_a_gap_in_the_timeline_is_not_damage(app, data_dir):
    s, c = app
    for body in (make_gapped(data_dir), make_gapped(data_dir, "faststart"),
                 open_ended_mdat(make_gapped(data_dir, "faststart"))):
        r = _post(c, body)
        assert r.status_code == 200, r.text
        assert abs(_audio(s, r.json()["id"]).size / 16000 - 20) < 0.2


def test_an_open_ended_mdat_imports_when_whole(app, data_dir):
    s, c = app
    r = _post(c, open_ended_mdat(make_media(data_dir, seconds=20, movflags="faststart")))
    assert r.status_code == 200, r.text
    assert abs(_audio(s, r.json()["id"]).size / 16000 - 20) < 0.2


@pytest.mark.parametrize("cut", ["half", "packet", "last-packets"])
def test_an_open_ended_mdat_is_still_checked_for_cuts(app, data_dir, cut):
    s, c = app
    good = make_media(data_dir, seconds=20, movflags="faststart")
    body, ends = open_ended_mdat(good), packet_ends(good)
    at = {"half": len(body) // 2, "packet": ends[len(ends) // 2], "last-packets": ends[-11]}[cut]
    _refused(s, c, body[:at], 400, DAMAGED)


def _with_large_mdat(data: bytes) -> bytes:
    """Turn ``free`` + ``mdat`` into one mdat with a 64-bit size. Nothing moves, so the sample offsets still hold."""
    free, mdat = box_of(data, b"free"), box_of(data, b"mdat")
    assert free[0] + 8 == mdat[0]
    header = (1).to_bytes(4, "big") + b"mdat" + (8 + mdat[1]).to_bytes(8, "big")
    return data[:free[0]] + header + data[mdat[0] + 8:]


def test_a_64_bit_box_size_imports_when_whole(app, data_dir):
    s, c = app
    r = _post(c, _with_large_mdat(make_media(data_dir, seconds=20)))
    assert r.status_code == 200, r.text
    assert abs(_audio(s, r.json()["id"]).size / 16000 - 20) < 0.2


@pytest.mark.parametrize("cut", ["body", "header"])
def test_a_64_bit_box_size_shows_a_cut(app, data_dir, cut):
    s, c = app
    body = _with_large_mdat(make_media(data_dir, seconds=20))
    at = {"body": len(body) // 2, "header": box_of(body, b"ftyp")[1] + 12}[cut]
    _refused(s, c, body[:at], 400, DAMAGED)


@pytest.mark.parametrize("body", [
    b"\x00\x00\x00\x08ftyp" + b"\x00\x00\x00\x04free",  # smaller than its own header
    b"\x00\x00\x00\x08ftyp" + b"\x00\x00\x00\x01free" + (8).to_bytes(8, "big"),  # the same, in 64 bits
    b"\x00\x00\x00\x08ftyp" + b"\xff\xff\xff\xffjunk",
])
def test_impossible_box_sizes_are_damage(data_dir, body):
    from modwen.audio import importer

    sealed = importer.SealedTempFile(data_dir)
    sealed.append(body)
    with pytest.raises(importer.ImportRefused, match="damaged"):
        importer.check_boxes(sealed)
    sealed.close()


def test_a_file_of_too_many_boxes_stops_the_walk(data_dir, monkeypatch):
    from modwen.audio import importer

    monkeypatch.setattr(importer, "MAX_TOP_BOXES", 10)
    sealed = importer.SealedTempFile(data_dir)
    sealed.append(b"\x00\x00\x00\x08free" * 11)
    with pytest.raises(importer.ImportRefused, match="damaged"):
        importer.check_boxes(sealed)
    sealed.close()


def test_a_long_fragmented_file_passes_the_walk(data_dir):
    """The search for copy protection stops at 10,000 boxes; a long fragmented recording has more at the top."""
    from modwen.audio import importer

    sealed = importer.SealedTempFile(data_dir)
    sealed.append(b"\x00\x00\x00\x08free" * 50_000)
    importer.check_boxes(sealed)
    sealed.close()


def _patch_elst(data: bytes, *, trim_end: int = 0, skip: int = 0) -> bytes:
    """Change the audio track's edit list, in samples at 48 kHz, as a lossless trim would."""
    out = bytearray(data)
    at, mvhd = data.index(b"elst"), data.index(b"mvhd")
    movie_scale = int.from_bytes(data[mvhd + 16:mvhd + 20], "big")
    span, start = at + 12, at + 16
    duration = int.from_bytes(data[span:span + 4], "big") - (trim_end + skip) * movie_scale // RATE
    out[span:span + 4] = duration.to_bytes(4, "big")
    media = int.from_bytes(data[start:start + 4], "big") + skip
    out[start:start + 4] = media.to_bytes(4, "big")
    return bytes(out)


@pytest.mark.parametrize("edit,seconds", [({"trim_end": 5 * RATE}, 15), ({"skip": 5 * RATE}, 15),
                                          ({"trim_end": 3 * RATE, "skip": 4 * RATE}, 13)])
def test_an_edit_list_that_trims_the_track_is_not_damage(app, data_dir, edit, seconds):
    s, c = app
    r = _post(c, _patch_elst(make_media(data_dir, seconds=20), **edit))
    assert r.status_code == 200, r.text
    assert _audio(s, r.json()["id"]).size / 16000 >= seconds - 0.2


def _stsz_entry_too_large(data: bytes, index: int = 9) -> bytes:
    """One sample claims nearly a gigabyte: the demuxer answers ENOMEM (found by fuzzing)."""
    at = data.index(b"stsz") + 16 + 4 * index
    return data[:at] + b"\x37" + data[at + 1:]


def _esds_config_too_long(data: bytes) -> bytes:
    """The decoder config claims more bytes than it has: opening the decoder answers ENOSYS (found by fuzzing)."""
    at = data.index(b"\x05\x80\x80\x80\x05", data.index(b"esds"))
    return data[:at + 1] + b"\x80\x48" + data[at + 3:]


@pytest.mark.parametrize("break_it,kw", [(_stsz_entry_too_large, {"movflags": "faststart"}),
                                         (_esds_config_too_long, {"video": True, "fmt": "mov"})])
def test_any_decoder_error_means_damaged(app, data_dir, break_it, kw):
    s, c = app
    _refused(s, c, break_it(make_media(data_dir, seconds=6, **kw)), 400, DAMAGED)


@pytest.mark.parametrize("error", [av.error.MemoryError, av.error.NotImplementedError, av.error.PatchWelcomeError])
def test_every_ffmpeg_error_class_is_caught_while_decoding(data_dir, error):
    from modwen.audio import importer
    from modwen.audio.chunks import ChunkWriter

    def bad():
        raise error(1, "from the decoder")

    container = SimpleNamespace(demux=lambda st: iter([SimpleNamespace(is_corrupt=False, dts=None, decode=bad)]))
    with pytest.raises(importer.ImportRefused, match="damaged"):
        importer._decode_track(container, None, ChunkWriter(data_dir / "m", "system", b"k" * 32), 3600)


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), MemoryError()])
def test_a_failure_reading_the_temp_file_is_not_damage(app, data_dir, monkeypatch, error):
    """The disk or the memory failed, not the file: the error comes up through PyAV's read callback."""
    from modwen.audio import importer
    from modwen.audio.chunks import ChunkWriter

    s, c = app
    body = make_media(data_dir, seconds=20)
    real_read, real_write = importer.SealedTempFile.read, ChunkWriter.write
    decoding = []

    def read(self, n=-1):
        if decoding:
            raise error
        return real_read(self, n)

    def write(self, frames):
        decoding.append(True)
        real_write(self, frames)

    monkeypatch.setattr(importer.SealedTempFile, "read", read)
    monkeypatch.setattr(ChunkWriter, "write", write)
    _refused(s, c, body, 400, GENERAL)


def test_a_change_of_format_mid_stream_is_refused(data_dir, monkeypatch):
    from modwen.audio import importer
    from modwen.audio.chunks import ChunkWriter

    def frame(rate):
        data = np.zeros((1, 1024), dtype=np.float32)
        f = av.AudioFrame.from_ndarray(data, format="fltp", layout="mono")
        f.sample_rate = rate
        return f

    packets = [SimpleNamespace(is_corrupt=False, dts=None, decode=lambda r=r: [frame(r)]) for r in (44100, 22050)]
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


def test_a_false_length_never_prints_an_exponent():
    from modwen.audio.importer import too_long_message

    assert "9999.9 hours long" in too_long_message(9999.9)
    for hours in (10_000, 2_562_047_788.0, 1e30):
        assert too_long_message(hours).startswith("This recording is over 6 hours long. Imports are limited to 6 hours.")


def test_a_hostile_declared_length_is_refused_in_plain_words(app, data_dir, monkeypatch):
    """The demuxer's answer is stood in for: the largest length a stream can state, in the file's own time base."""
    from fractions import Fraction

    from modwen.audio import importer

    s, c = app
    track = SimpleNamespace(duration=2 ** 63 - 1, time_base=Fraction(1, 1_000_000), index_entries=())
    monkeypatch.setattr(importer, "_pick_audio", lambda container: track)
    r = _post(c, make_media(data_dir, seconds=20))
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("This recording is over 6 hours long.")
    _nothing_left(s)


def test_declared_length_over_the_limit_is_refused(app, data_dir, monkeypatch):
    from modwen.audio import importer

    s, c = app
    monkeypatch.setattr(importer, "MAX_IMPORT_HOURS", 5 / 3600)
    _refused(s, c, make_media(data_dir, seconds=12), 400, importer.too_long_message(12 / 3600))


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
    monkeypatch.setattr(importer, "_pick_audio", lambda container: SimpleNamespace(duration=None, index_entries=()))
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

    def stolen(mid, *rest):
        s.store.update_meeting(mid, status="failed")
        return real(mid, *rest)

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

where, body, replaces = sys.argv[1], open(sys.argv[2], "rb").read(), sys.argv[3]
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
elif where == "transaction":  # the status is written, the record's removal isn't, and nothing is committed
    s.store._conn.set_trace_callback(lambda sql: die() if sql.startswith("DELETE FROM meetings") else None)
elif where == "removed":  # the moment after the transaction
    real = s.store.commit_import
    s.store.commit_import = lambda *a: (real(*a), die())
c = TestClient(create_app(s, token="t"))
c.cookies.set("mw_token", "t")
c.post("/api/meetings/import", params={"title": "Clip", "filename": "Clip.mp4", "replaces": replaces},
       content=body, headers={"content-type": "application/octet-stream"})
os._exit(0)
"""


def _crash(data_dir, where, replaces=""):
    """Run one import in a process that dies at ``where``, on the same data folder."""
    path = data_dir / "input.bin"
    path.write_bytes(make_media(data_dir, seconds=30, video=True))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, "-c", CRASH, where, str(path), replaces], cwd=data_dir, env=env,
                       capture_output=True, timeout=120)
    assert p.returncode == 9, p.stderr.decode(errors="replace")
    path.unlink()


@pytest.mark.parametrize("where", ["upload", "decode", "commit", "committed"])
def test_a_real_crash_at_each_stage(cfg, fake_llm, data_dir, where):
    _crash(data_dir, where)

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


@pytest.mark.parametrize("where,old_stays", [("transaction", True), ("removed", False)])
def test_a_crash_never_leaves_both_the_new_import_and_the_old_record(cfg, fake_llm, data_dir, where, old_stays):
    s = _services(cfg, fake_llm, start_worker=False)
    old = _record(s, "Old.mp4")
    s.store.close()
    _crash(data_dir, where, old)

    s = _services(cfg, fake_llm)
    assert (s.store.get_meeting(old) is not None) is old_stays
    new = [m for m in s.store.list_meetings() if m["id"] != old]
    assert len(new) == 1
    if old_stays:  # the import did not complete: a second record for this attempt, and the first one is kept
        assert new[0]["title"] == "Clip.mp4" and new[0]["error"].startswith("import:")
        assert not (cfg.audio_dir / new[0]["id"]).exists()
    else:
        assert new[0]["title"] == "Clip" and _wait(s, new[0]["id"])["status"] == "ready"


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


def test_the_commit_and_the_removal_succeed_or_fail_together(app, data_dir):
    s, c = app
    old = _record(s)
    with s.store._lock:
        s.store._conn.execute(f"CREATE TRIGGER stop BEFORE DELETE ON meetings WHEN old.id = '{old}' "
                              "BEGIN SELECT RAISE(ABORT, 'database trouble'); END")
        s.store._conn.commit()
    r = _post(c, make_media(data_dir), replaces=old)
    assert r.status_code == 400 and r.json()["detail"] == GENERAL
    assert [m["id"] for m in s.store.list_meetings()] == [old]
    assert s.store.get_meeting(old)["error"].startswith("import:")
    assert not list(s.cfg.audio_dir.glob("*")) and not s.store.interrupted_imports()


def test_trouble_after_the_commit_is_logged_by_id_and_the_import_succeeds(app, data_dir, monkeypatch, caplog):
    s, c = app
    old = _record(s, "Private name.mp4")
    real = s.meetings_changed

    def boom(mid, change):
        if mid == old:
            raise RuntimeError("no listeners")
        real(mid, change)

    monkeypatch.setattr(s, "meetings_changed", boom)
    r = _post(c, make_media(data_dir), replaces=old)
    assert r.status_code == 200, r.text
    assert s.store.get_meeting(old) is None
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
