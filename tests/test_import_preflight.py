"""Regressions for QA's bypasses of the metadata preflight (FFmpeg's mov.c dispatches tables by box type, not place).

Every hostile file is generated here and judged with av.open replaced by a tripwire, so no bomb ever goes off."""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from modwen.audio import importer
from modwen.audio.chunks import ChunkWriter
from tests.test_import_media import make_media
from tests.test_import_safety import box, rewrite

av = pytest.importorskip("av")
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts"}
N = 1 << 26
FULL = bytes(4)


# ----- a small box tree, so a test can move boxes around -----
def parse(data):
    out, at = [], 0
    while at + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, at)
        body = data[at + 8:at + size]
        out.append([kind, parse(body) if kind in CONTAINERS else body])
        at += size
    return out


def ser(boxes):
    return b"".join(box(k, ser(v) if isinstance(v, list) else v) for k, v in boxes)


def find(tree, *path):
    for item in tree:
        if item[0] == path[0]:
            return item if len(path) == 1 else find(item[1], *path[1:])
    raise KeyError(path)


def trak(tree):
    return find(tree, b"moov", b"trak")[1]


def stbl(tree):
    return find(tree, b"moov", b"trak", b"mdia", b"minf", b"stbl")[1]


def put(parent, kind, body):
    for item in parent:
        if item[0] == kind:
            item[1] = body
            return
    raise KeyError(kind)


def stco(tree, chunks=1):
    item = next(b for b in stbl(tree) if b[0] in (b"stco", b"co64"))
    item[0], item[1] = b"stco", FULL + struct.pack(">I", chunks) + struct.pack(">I", 40) * chunks


def sound_entry(tag, version=0, spf=0):
    body = bytes(6) + struct.pack(">H", 1) + struct.pack(">HHI", version, 0, 0) + struct.pack(">HHHH", 1, 16, 0, 0)
    body += struct.pack(">I", 48000 << 16)
    if version == 1:
        body += struct.pack(">IIII", spf, 2, 2, 2)
    return box(tag, body)


CMOV = box(b"cmov", box(b"dcom", b"zlib") + box(b"cmvd", struct.pack(">I", 1 << 30) + b"tiny"))


def index_bomb(base):
    tree = parse(base)
    s = stbl(tree)
    put(s, b"stts", FULL + struct.pack(">III", 1, N, 1024))
    put(s, b"stsc", FULL + struct.pack(">IIII", 1, 1, N, 1))
    put(s, b"stsz", FULL + struct.pack(">II", 8, N))
    stco(tree)
    return tree


def stsc_bomb(base, delta=1):
    tree = parse(base)
    s = stbl(tree)
    put(s, b"stts", FULL + struct.pack(">III", 1, 1, delta))
    put(s, b"stsc", FULL + struct.pack(">IIII", 1, 1, 0x7fffff00, 1))
    put(s, b"stsz", FULL + struct.pack(">II", 2, 1))
    stco(tree, 40)
    return tree


def edit_box(count):
    return [b"edts", ser([[b"elst", FULL + struct.pack(">I", count) + struct.pack(">IiI", 96000, 0, 1 << 16) * count]])]


def hide(base, kind, inner):
    tree = parse(base)
    trak(tree).append([kind, inner])
    return ser(tree)


def moved(base, kind):
    tree = index_bomb(base)
    s = stbl(tree)
    tables = b"".join(ser([b]) for b in s if b[0] != b"stsd")
    s[:] = [b for b in s if b[0] == b"stsd"]
    trak(tree).append([kind, tables])
    return ser(tree)


def edit_moved(base):
    tree = parse(base)
    t = trak(tree)
    t[:] = [b for b in t if b[0] != b"edts"]
    t.append([b"tref", ser([edit_box(1_000)])])
    return ser(tree)


def set_stsd(base, edit):
    tree = parse(base) if isinstance(base, bytes) else base
    for item in stbl(tree):
        if item[0] == b"stsd":
            item[1] = edit(item[1])
    return ser(tree)


def with_wave(body):
    wave = box(b"wave", CMOV)
    entry = body[8:]
    return body[:8] + struct.pack(">I", len(entry) + len(wave)) + entry[4:] + wave


def wave_isom_v1(body):
    wave = box(b"wave", CMOV)
    entry = body[8:]
    head = struct.pack(">I", len(entry) + len(wave)) + entry[4:16] + struct.pack(">H", 1) + entry[18:36]
    return bytes([1, 0, 0, 0]) + body[4:8] + head + wave + entry[36:]


def handler(base, subtype):
    tree = parse(with_wave_file(base))
    mdia = find(tree, b"moov", b"trak", b"mdia")[1]
    item = next(b for b in mdia if b[0] == b"hdlr")
    if subtype is None:
        mdia.remove(item)
    else:
        item[1] = item[1][:8] + subtype + item[1][12:]
    return ser(tree)


def with_wave_file(base):
    return set_stsd(base, with_wave)


def two_handlers(base, n=1 << 24):
    tree = parse(base)
    s = stbl(tree)
    put(s, b"stts", FULL + struct.pack(">III", 1, n, 1))
    put(s, b"stsc", FULL + struct.pack(">IIII", 1, 1, n, 1))
    put(s, b"stsz", FULL + struct.pack(">II", 1, n))
    put(s, b"stsd", FULL + struct.pack(">I", 1) + sound_entry(b"sowt"))
    stco(tree)
    find(tree, b"moov", b"trak", b"mdia", b"minf")[1].insert(0, [b"hdlr", FULL + b"mhlr" + b"vide" + bytes(13)])
    return ser(tree)


def late_video_handler(base, n=1 << 24):
    """FFmpeg builds the index after the whole trak, so a trak-level meta after mdia can still turn the track to video."""
    tree = parse(two_handlers(base, n))
    minf = find(tree, b"moov", b"trak", b"mdia", b"minf")[1]
    minf.remove(next(b for b in minf if b[0] == b"hdlr"))
    trak(tree).append([b"meta", FULL + box(b"hdlr", FULL + b"mhlr" + b"vide" + bytes(13))])
    return ser(tree)


def top_trak(base):
    tree = index_bomb(base)
    good = parse(base)
    at = next(i for i, b in enumerate(good) if b[0] == b"moov")
    return ser(good[:at]) + ser([find(tree, b"moov", b"trak")]) + ser(good[at:])


def nested(traks=1000, mdias=1000):
    tracks = box(b"trak", box(b"mdia", b"") * mdias) * traks
    return box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", tracks) + box(b"mdat", b"")


def enca_wrapped(base):
    tree = parse(base.replace(b"mp4a", b"enca", 1))
    t = trak(tree)
    at = next(i for i, b in enumerate(t) if b[0] == b"mdia")
    t[at] = [b"udta", ser([t[at]])]
    return ser(tree)


def enca_moov_in_moov(base):
    tree = parse(base.replace(b"mp4a", b"enca", 1))
    at = next(i for i, b in enumerate(tree) if b[0] == b"moov")
    tree[at] = [b"moov", ser([tree[at]])]
    return ser(tree)


def overrun_entry(base):
    """The last sample entry claims to reach past stsd into a free box, where a compressed moov waits."""
    tree = parse(base)
    s = stbl(tree)
    entry = sound_entry(b"mp4a")
    junk = box(b"junk", bytes(8))
    put(s, b"stsd", FULL + struct.pack(">I", 1) + struct.pack(">I", len(entry) + 64 + len(CMOV)) + entry[4:] + junk)
    at = next(i for i, b in enumerate(s) if b[0] == b"stsd")
    s.insert(at + 1, [b"free", bytes(48) + CMOV])
    return ser(tree)


def ctts_bomb(base):
    tree = parse(base)
    stbl(tree).append([b"ctts", FULL + struct.pack(">III", 1, 0x7fffff00, 0)])
    return ser(tree)


def sync_group_bomb(base):
    tree = parse(base)
    stbl(tree).append([b"sbgp", FULL + b"sync" + struct.pack(">III", 1, 0x7fffff00, 1)])
    return ser(tree)


def no_moov(base):
    """With no moov FFmpeg retries and takes a free box starting with mvhd for one."""
    tree = index_bomb(base)
    moov = find(tree, b"moov")[1]
    return box(b"ftyp", b"isom" + bytes(4)) + box(b"free", box(b"mvhd", bytes(100)) + ser(moov)) + box(b"mdat", b"x")


def hoov(base):
    tree = index_bomb(base)
    item = find(tree, b"moov")
    item[0], item[1] = b"hoov", box(b"mvhd", bytes(100)) + ser(item[1])
    return ser(tree)


def in_meta_after_junk(base):
    # mov_read_meta steps four bytes at a time to an hdlr, then parses what follows as boxes.
    return hide(base, b"meta", FULL + b"\xff" * 8 + box(b"hdlr", bytes(24)) + CMOV)


def in_ipco(base):
    return hide(base, b"meta", FULL + box(b"hdlr", bytes(24)) + box(b"iprp", box(b"ipco", CMOV)))


REFUSED = {
    "index": lambda b: ser(index_bomb(b)),
    "edits_1000000": lambda b: rewrite(b, b"trak", lambda t: ser([edit_box(1_000_000)]) + t),
    "wave_cmov": with_wave_file,
    "enca_after_10001_free": lambda b: padded_enca(b),
    "nested": lambda b: nested(),
    "toplevel_trak": top_trak,
    "tables_in_tref": lambda b: hide(b, b"tref", index_tables(b)),
    "tables_in_clip": lambda b: hide(b, b"clip", index_tables(b)),
    "tables_in_sinf": lambda b: hide(b, b"sinf", index_tables(b)),
    "tables_in_schi": lambda b: hide(b, b"schi", index_tables(b)),
    "tables_in_tapt": lambda b: hide(b, b"tapt", index_tables(b)),
    "tables_in_udta": lambda b: hide(b, b"udta", index_tables(b)),
    "cmov_in_tref": lambda b: hide(b, b"tref", CMOV),
    "cmov_in_clip": lambda b: hide(b, b"clip", CMOV),
    "cmov_in_sinf": lambda b: hide(b, b"sinf", CMOV),
    "cmov_in_schi": lambda b: hide(b, b"sinf", box(b"schi", CMOV)),
    "wave_cmov_handler_text": lambda b: handler(b, b"text"),
    "wave_cmov_no_handler": lambda b: handler(b, None),
    "wave_cmov_isom_v1": lambda b: set_stsd(b, wave_isom_v1),
    "meta_junk_cmov": in_meta_after_junk,
    "ipco_cmov": in_ipco,
    "stsc_bomb_aac": lambda b: ser(stsc_bomb(b)),
    "stsc_bomb_clipped_delta": lambda b: ser(stsc_bomb(b, 0xffffffff)),
    "pcm_spf_spoof": lambda b: set_stsd(stsc_bomb(b), lambda _: bytes([1, 0, 0, 0]) + struct.pack(">I", 1) +
                                        sound_entry(b"sowt", 1, 0x7fffffff)),
    "mixed_descriptions": lambda b: set_stsd(stsc_bomb(b), lambda s: FULL + struct.pack(">I", 2) +
                                             sound_entry(b"sowt") + s[8:]),
    "two_handlers": two_handlers,
    "late_video_handler": late_video_handler,
    "moved_tref": lambda b: moved(b, b"tref"),
    "moved_sinf": lambda b: moved(b, b"sinf"),
    "moved_stbl": lambda b: moved(b, b"stbl"),
    "edit_list_in_tref": edit_moved,
    "enca_mdia_in_udta": enca_wrapped,
    "enca_moov_in_moov": enca_moov_in_moov,
    "overrun_sample_entry": overrun_entry,
    "ctts_bomb": ctts_bomb,
    "sync_group_bomb": sync_group_bomb,
    "free_as_moov": no_moov,
    "hoov": hoov,
    "iinf_count": lambda b: root_meta(b, box(b"iinf", bytes([1, 0, 0, 0]) + struct.pack(">I", 1 << 27))),
    "iloc_count": lambda b: root_meta(b, box(b"iloc", bytes([2, 0, 0, 0, 0, 0]) + struct.pack(">I", 1 << 27))),
    "sidx_into_a_box": lambda b: sidx_at(b, 8),
    "anevia_free": lambda b: box(b"free", b"Anevia\x1a\x1a" + bytes(8)) + b,
    "senc_in_traf": lambda b: b + box(b"moof", box(b"traf", box(b"senc", FULL + struct.pack(">I", 1 << 30)))),
}


def sidx_at(base, inside):
    """A segment index whose one reference lands ``inside`` the last top-level box, as the demuxer jumps there."""
    tree = parse(base)
    last = ser(tree[-1:])
    head = ser(tree[:-1])
    index = FULL + struct.pack(">III", 1, 48000, 0) + struct.pack(">I", inside) + struct.pack(">HH", 0, 1)
    index += struct.pack(">III", len(last), 1, 0)
    return head + box(b"sidx", index) + last


def root_meta(base, inner):
    tree = parse(base)
    at = next(i for i, b in enumerate(tree) if b[0] == b"moov")
    return ser(tree[:at]) + box(b"meta", FULL + box(b"hdlr", bytes(8) + b"pict" + bytes(13)) + inner) + ser(tree[at:])


def index_tables(base):
    return b"".join(ser([b]) for b in stbl(index_bomb(base)) if b[0] != b"stsd")


def padded_enca(base):
    tree = parse(base.replace(b"mp4a", b"enca", 1))
    at = next(i for i, b in enumerate(tree) if b[0] == b"moov")
    return ser(tree[:at]) + box(b"free", b"") * 10_001 + ser(tree[at:])


def judge(where, data):
    with importer.SealedTempFile(where) as sealed:
        sealed.append(data)
        return importer.decode_media_into(sealed, ChunkWriter(where / "out", "system", b"k" * 32))


def refused(where, monkeypatch, data):
    """Judge with av.open as a tripwire. It's set only here, because building the fixtures needs the real one."""
    monkeypatch.setattr(av, "open", lambda *a, **k: pytest.fail("hostile metadata reached av.open"))
    with pytest.raises(importer.ImportRefused) as caught:
        judge(where, data)
    return str(caught.value)


@pytest.mark.parametrize("attack", REFUSED)
def test_every_qa_probe_is_refused_before_native_open(data_dir, monkeypatch, attack):
    refused(data_dir, monkeypatch, REFUSED[attack](make_media(data_dir, seconds=2)))


@pytest.mark.parametrize("attack,message", [
    ("enca_mdia_in_udta", importer.NOT_SUPPORTED), ("enca_moov_in_moov", importer.NOT_SUPPORTED),
    ("cmov_in_tref", importer.NOT_SUPPORTED), ("stsc_bomb_aac", importer.NOT_SUPPORTED),
    ("moved_tref", importer.DAMAGED), ("toplevel_trak", importer.DAMAGED), ("overrun_sample_entry", importer.DAMAGED),
])
def test_message_says_limit_or_damage_truthfully(data_dir, monkeypatch, attack, message):
    assert refused(data_dir, monkeypatch, REFUSED[attack](make_media(data_dir, seconds=2))) == message


def _audio_track(sub=b"soun", tag=b"mp4a", samples=1, delta=1024, chunks=1, per=1, edits=0):
    tables = box(b"stsd", FULL + struct.pack(">I", 1) + box(tag, bytes(28))) + \
        box(b"stts", FULL + struct.pack(">III", 1, samples, delta)) + \
        box(b"stsc", FULL + struct.pack(">IIII", 1, 1, per, 1)) + \
        box(b"stsz", FULL + struct.pack(">II", 1, samples)) + \
        box(b"stco", FULL + struct.pack(">I", chunks) + bytes(4 * chunks))
    body = box(b"mdia", box(b"hdlr", bytes(8) + sub + bytes(12)) + box(b"minf", box(b"stbl", tables)))
    if edits:
        body = box(b"edts", box(b"elst", FULL + struct.pack(">I", edits) + bytes(12 * edits))) + body
    return box(b"trak", body)


class Sparse(io.BytesIO):
    size = 8 << 30


@pytest.mark.parametrize("track", [
    _audio_track(samples=1_012_500, edits=4) * 2,  # over the work limit only because of its edits
    _audio_track(samples=1_012_500) * 8,
])
def test_intact_files_over_a_safety_limit_are_not_called_damaged(track):
    with pytest.raises(importer.ImportRefused) as refused:
        importer._check_metadata(Sparse(box(b"moov", track)))
    assert str(refused.value) == importer.NOT_SUPPORTED


def test_too_many_edits_is_a_limit_not_damage():
    with pytest.raises(importer.ImportRefused) as refused:
        importer._check_metadata(Sparse(box(b"moov", _audio_track(edits=257))))
    assert str(refused.value) == importer.NOT_SUPPORTED


def test_nested_budget_is_a_limit_not_damage(monkeypatch):
    monkeypatch.setattr(importer, "MAX_BOXES", 4)
    with pytest.raises(importer.ImportRefused) as refused:
        importer._check_metadata(Sparse(box(b"moov", box(b"udta", box(b"junk", b"") * 8))))
    assert str(refused.value) == importer.NOT_SUPPORTED


def test_fragment_walks_share_one_budget(monkeypatch):
    monkeypatch.setattr(importer, "MAX_FRAGMENT_BOXES", 20)
    moof = box(b"moof", box(b"free", b"") * 9)
    data = box(b"moov", b"") + moof * 2
    importer._check_metadata(Sparse(data))
    with pytest.raises(importer.ImportRefused) as refused:
        importer._check_metadata(Sparse(data + moof))
    assert str(refused.value) == importer.NOT_SUPPORTED


def test_six_hours_of_ordinary_tracks_pass():
    aac = _audio_track(samples=1_012_500, chunks=2000, per=506)
    video = _audio_track(sub=b"vide", tag=b"avc1", samples=1_296_000, delta=1000, chunks=2000, per=648, edits=1)
    importer._check_metadata(Sparse(box(b"moov", aac + video)))


def test_stsc_never_refuses_a_table_ffmpeg_repairs():
    # mov_read_stsc mends entries out of order, so this is bounded, not damaged, as long as it stays small.
    track = rewrite(_audio_track(chunks=4), b"stsc",
                    lambda _: FULL + struct.pack(">I", 2) + struct.pack(">III", 3, 1, 1) + struct.pack(">III", 2, 1, 1))
    importer._check_metadata(Sparse(box(b"moov", track)))


def test_a_segment_index_may_point_at_a_box(data_dir):
    body = make_media(data_dir, seconds=2)
    assert judge(data_dir, sidx_at(body, 0)) > 0


@pytest.mark.parametrize("flags", ["empty_moov+default_base_moof+frag_keyframe+global_sidx",
                                   "empty_moov+default_base_moof+frag_keyframe+dash"])
def test_real_segment_indexes_import(data_dir, flags):
    body = make_media(data_dir, seconds=2, movflags=flags)
    assert b"sidx" in body
    assert judge(data_dir, body) > 0


def test_table_names_in_free_are_left_alone(data_dir):
    """Editors leave an old moov behind in a free box. FFmpeg never reads it, so neither do we."""
    body = make_media(data_dir, seconds=2)
    assert judge(data_dir, hide(body, b"free", index_tables(body))) > 0


# ----- valid media that must keep importing -----
def pcm_file(where, fmt, codec, seconds):
    path = where / f"pcm-{fmt}-{codec}-{seconds}.bin"
    with av.open(str(path), "w", format=fmt) as out:
        stream = out.add_stream(codec, rate=48000, layout="mono")
        ctx = stream.codec_context
        resampler = av.AudioResampler(format=ctx.format.name, layout="mono", rate=48000, frame_size=4800)
        wave = (0.2 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype(np.float32)[None, :]
        for i in range(seconds):
            frame = av.AudioFrame.from_ndarray(wave, format="fltp", layout="mono")
            frame.sample_rate, frame.pts = 48000, i * 48000
            for f in resampler.resample(frame):
                for packet in stream.encode(f):
                    out.mux(packet)
        for f in resampler.resample(None):
            for packet in stream.encode(f):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    data = path.read_bytes()
    path.unlink()
    return data


@pytest.mark.parametrize("fmt,codec,tag", [("mp4", "pcm_s16le", b"ipcm"), ("mp4", "pcm_s24le", b"ipcm"),
                                           ("mp4", "pcm_f32le", b"fpcm"), ("mov", "pcm_s16le", b"sowt")])
def test_long_intact_pcm_imports(data_dir, fmt, codec, tag):
    # Above the 8 million raw samples that a packet count would stop at (167 s at 48 kHz).
    data = pcm_file(data_dir, fmt, codec, 170)
    assert tag in data
    assert judge(data_dir, data) == pytest.approx(170 * 16000, abs=2000)


@pytest.mark.parametrize("where", ["before", "after"])
def test_a_meta_box_does_not_change_the_tracks_handler(data_dir, where):
    meta = box(b"meta", FULL + box(b"hdlr", bytes(8) + b"mdta" + bytes(12)))
    data = rewrite(pcm_file(data_dir, "mov", "pcm_s16le", 170), b"trak",
                   lambda t: meta + t if where == "before" else t + meta)
    assert judge(data_dir, data) == pytest.approx(170 * 16000, abs=2000)
