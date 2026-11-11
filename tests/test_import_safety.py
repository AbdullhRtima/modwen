"""Synthetic metadata regressions: native allocation attacks run only in the limited production child."""

from __future__ import annotations

import io
import struct
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from modwen.audio import importer
from modwen.audio.chunks import ChunkWriter
from tests.test_import_media import (FRAGMENTED, _nothing_left, _post, app as app, make_media)

av = pytest.importorskip("av")
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}
MDHD = struct.pack(">I4s", 32, b"mdhd") + bytes(12) + struct.pack(">II", 48000, 0) + bytes(4)  # every track has one


def box(kind, body):
    return struct.pack(">I4s", len(body) + 8, kind) + body


def rewrite(data, kind, replace, occurrence=0):
    """Replace one structural box and repair its parents; fixtures use ordinary 32-bit sizes."""
    seen = 0

    def walk(data):
        nonlocal seen
        parts, at = [], 0
        while at + 8 <= len(data):
            size, tag = struct.unpack_from(">I4s", data, at)
            body = data[at + 8:at + size]
            if tag == kind:
                seen += 1
                if seen - 1 == occurrence:
                    body = replace(body)
            elif tag in CONTAINERS:
                body = walk(body)
            parts.append(box(tag, body))
            at += size
        return b"".join(parts) + data[at:]

    return walk(data)


def hostile(data, attack, occurrence=0):
    if attack == "index":
        return rewrite(data, b"stsz", lambda _: struct.pack(">III", 0, 8, 2 ** 26), occurrence)
    if attack == "edits":
        edits = struct.pack(">II", 0, 10_000) + struct.pack(">IiI", 2000, 0, 1 << 16) * 10_000
        return rewrite(data, b"edts", lambda _: box(b"elst", edits), occurrence)
    if attack == "compressed":
        # Tiny compressed payload, hostile declared expansion. Never allocate the advertised gigabyte.
        return rewrite(data, b"moov", lambda body: box(b"cmov", box(b"dcom", b"zlib") +
                       box(b"cmvd", struct.pack(">I", 1 << 30) + zlib.compress(box(b"moov", body)))))
    if attack == "fragment":
        return data + box(b"moof", box(b"traf", box(b"trun", struct.pack(">II", 0, 2 ** 26))))
    if attack == "misplaced":
        # Native MOV parsing is permissive about placement; a table outside trak must not skip the bounds.
        return rewrite(data, b"moov", lambda body: body + box(b"stsz", struct.pack(">III", 0, 8, 2 ** 26)))
    if attack == "sample_config":
        # Proved with a tiny invalid dcom: FFmpeg dispatches cmov nested in an audio entry's wave box too.
        cmov = box(b"cmov", box(b"dcom", b"XXXX") + box(b"cmvd", bytes(4) + b"tiny"))
        return rewrite(data, b"stsd", lambda body: body[:8] + rewrite(body[8:], b"mp4a",
                       lambda sample: sample + box(b"wave", cmov)), occurrence)
    if attack == "descriptions":
        return rewrite(data, b"stsd", lambda body: body[:4] + struct.pack(">I", 2 ** 26) + body[8:], occurrence)
    raise AssertionError(attack)


@pytest.mark.parametrize("attack", ["index", "edits", "compressed", "fragment", "misplaced", "sample_config",
                                    "descriptions"])
@pytest.mark.parametrize("second_track", [False, True])
def test_hostile_tables_are_contained_by_the_real_decoder(data_dir, attack, second_track):
    from modwen.audio.decoder import decode_isolated

    body = hostile(make_media(data_dir, seconds=2, freqs=(440, 880) if second_track else (440,)),
                   attack, int(second_track))
    with importer.SealedTempFile(data_dir) as sealed:
        sealed.append(body)
        if attack in ("edits", "misplaced") or (attack == "index" and second_track):
            # Repeated edits and metadata in an unselected track stay under the OS budget. The selected audio still
            # has to be complete. Decision0003 explicitly removes estimates as a pre-native refusal contract.
            samples = decode_isolated(sealed, ChunkWriter(data_dir / "out", "system", b"k" * 32))
            assert samples > 32000 if attack == "edits" and not second_track else samples == pytest.approx(32000, abs=500)
            return
        with pytest.raises(importer.ImportRefused):
            decode_isolated(sealed, ChunkWriter(data_dir / "out", "system", b"k" * 32))


@pytest.mark.parametrize("fragmented", [False, True])
def test_padding_cannot_skip_drm_or_fragment_checks(data_dir, monkeypatch, fragmented):
    body = make_media(data_dir, seconds=2, movflags=FRAGMENTED if fragmented else None)
    moov = body.index(b"moov") - 4
    body = body[:moov] + box(b"free", b"") * 10_001 + body[moov:]
    body = body.replace(b"mp4a", b"enca", 1)
    monkeypatch.setattr(av, "open", lambda *a, **k: pytest.fail("walk exhaustion reached native opening"))
    with importer.SealedTempFile(data_dir) as sealed:
        sealed.append(body)
        with pytest.raises(importer.ImportRefused):
            importer.decode_media_into(sealed, ChunkWriter(data_dir / "out", "system", b"k" * 32))


def test_nested_box_budget_is_shared_between_tracks(data_dir, monkeypatch):
    # Each individual level has fewer than MAX_BOXES; total work must still be bounded.
    body = box(b"moov", box(b"trak", box(b"mdia", MDHD) * 8) * 8)
    monkeypatch.setattr(importer, "MAX_BOXES", 16)
    with importer.SealedTempFile(data_dir) as sealed:
        sealed.append(body)
        with pytest.raises(importer.ImportRefused, match="supported"):
            importer.is_encrypted(sealed)


@pytest.mark.parametrize("attack", ["index", "edits", "compressed", "fragment", "misplaced", "sample_config",
                                    "descriptions"])
def test_metadata_refusals_use_existing_localized_errors_and_cleanup(app, data_dir, attack):
    from modwen.audio.decoder import decode_isolated

    s, c = app
    s._media_decoder_override = decode_isolated
    response = _post(c, hostile(make_media(data_dir, seconds=2), attack))
    if attack in ("edits", "misplaced"):
        assert response.status_code == 200
        if attack == "edits":
            assert response.json()["duration_ms"] > 2000
        else:
            # av18 keeps about5ms of AAC padding in this generated2s clip; this is complete selected audio.
            assert response.json()["duration_ms"] == pytest.approx(2000, abs=20)
        assert list(s.cfg.data_dir.glob("*.sealed")) == []
        return
    assert response.status_code == 400, response.text
    assert response.json()["detail"] in (importer.DAMAGED, importer.NOT_SUPPORTED) or "limited to" in response.json()["detail"]
    _nothing_left(s)


@pytest.mark.parametrize("version", ["18.1.0", "19.0.1"])
def test_native_open_options_are_restrictive_and_metadata_keyword_is_versioned(data_dir, monkeypatch, version):
    body = make_media(data_dir, seconds=2)
    real, calls = av.open, []

    def observed(*args, **kwargs):
        calls.append(kwargs.copy())
        # Simulate each version's API while the real installed decoder verifies the file.
        forwarded = dict(kwargs)
        forwarded.pop("metadata_errors", None)
        return real(*args, **forwarded)

    monkeypatch.setattr(av, "__version__", version)
    monkeypatch.setattr(av, "open", observed)
    with importer.SealedTempFile(data_dir) as sealed:
        sealed.append(body)
        importer.decode_media_into(sealed, ChunkWriter(data_dir / "out", "system", b"k" * 32))
    assert calls[0]["options"]["enable_drefs"] == "0"
    assert calls[0]["options"]["protocol_whitelist"] == ""
    assert calls[0]["options"]["codec_whitelist"] == ",".join(sorted(importer.MEDIA_CODECS))
    assert calls[0].get("metadata_errors") == ("replace" if version.startswith("18.") else None)


def test_video_decoder_is_not_opened_but_the_audio_still_imports(data_dir):
    """FFmpeg's stream probing opens a decoder for every track; the whitelist keeps it to the audio ones."""
    import av.logging

    body = make_media(data_dir, video=True, seconds=2)
    level = av.logging.get_level()
    av.logging.set_level(av.logging.VERBOSE)
    try:
        with av.logging.Capture() as logs, importer.SealedTempFile(data_dir) as sealed:
            sealed.append(body)
            samples = importer.decode_media_into(sealed, ChunkWriter(data_dir / "out", "system", b"k" * 32))
    finally:
        av.logging.set_level(level)
    assert samples > 0
    assert any("(mpeg4) not on whitelist" in message for _, _, message in logs), logs


def test_every_allowed_codec_is_a_real_decoder():
    # A misspelt name would silently refuse every file that uses that codec.
    for name in importer.MEDIA_CODECS:
        assert av.Codec(name, "r").name == name


@pytest.mark.parametrize("name,expected", [
    ("اجتماع الفريق.M4A", "اجتماع الفريق.M4A"),
    ("Q3 review مراجعة.mov", "Q3 review مراجعة.mov"),
    ("wrong\u202ename\u202c\x00\r\n.mp4", "wrongname.mp4"),
    ("x" * 300, "x" * 255),
])
def test_interrupted_filename_is_safe_but_ordinary_names_are_preserved(app, name, expected):
    s, c = app
    saved = []
    original = s.store.create_meeting

    def observe(title, **kwargs):
        saved.append(title)
        return original(title, **kwargs)

    s.store.create_meeting = observe
    response = _post(c, b"not media", filename=name, title="Existing caller title")
    assert response.status_code == 400
    assert saved == [expected]
    _nothing_left(s)


def test_six_hour_pcm_sample_count_is_not_a_packet_count():
    """Validate a six-hour PCM header without creating gigabytes of fixture audio or opening it natively."""
    n = 6 * 3600 * 48000
    tables = (box(b"stsd", bytes(4) + struct.pack(">I", 1) + box(b"sowt", bytes(28))) +
              box(b"stsz", struct.pack(">III", 0, 4, n)) +
              box(b"stts", struct.pack(">IIII", 0, 1, n, 1)) +
              box(b"stsc", struct.pack(">IIIII", 0, 1, 1, n, 1)) +
              box(b"stco", struct.pack(">III", 0, 1, 100)))
    data = box(b"moov", box(b"trak", box(b"mdia", MDHD + box(b"hdlr", bytes(8) + b"soun") +
               box(b"minf", box(b"stbl", tables)))))
    data += struct.pack(">I4s", n * 4 + 8, b"mdat")

    class SparseMedia(io.BytesIO):
        size = len(data) + n * 4

    with SparseMedia(data) as sealed:
        assert not importer.is_encrypted(sealed)


@pytest.mark.parametrize("longest", [5, 60])
def test_low_sample_rate_cannot_expand_a_whole_frame_before_the_length_guard(monkeypatch, longest):
    # The resampler is a tripwire: test the hazardous call without actually allocating its amplified output.
    sizes = []
    monkeypatch.setattr(importer.soxr, "ResampleStream", lambda *a, **k: SimpleNamespace(
        resample_chunk=lambda data, **kw: sizes.append(len(data)) or np.zeros(0, dtype=np.float32)))
    monkeypatch.setattr(av, "AudioResampler", lambda **kw: SimpleNamespace(
        resample=lambda frame: [frame] if frame is not None else []))
    frame = SimpleNamespace(sample_rate=1, layout=SimpleNamespace(name="mono", nb_channels=1),
                            format=SimpleNamespace(name="fltp"),
                            to_ndarray=lambda: np.zeros((1, 8), dtype=np.float32))
    packet = SimpleNamespace(is_corrupt=False, dts=None, decode=lambda: [frame])
    container = SimpleNamespace(demux=lambda track: [packet])
    writer = SimpleNamespace(write=lambda data: None)
    if longest == 5:
        with pytest.raises(importer.ImportRefused, match="limited"):
            importer._decode_track(container, None, writer, longest)
        assert sizes == []
    else:
        assert importer._decode_track(container, None, writer, longest) == 8
        assert sizes and max(sizes) <= 1
