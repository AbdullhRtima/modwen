"""Import a recording file without ever writing it to disk in the clear, and without loading it into memory.

The upload is streamed into a ``SealedTempFile`` (AES-CTR with a key that only exists in memory: after a crash
the leftover file is unreadable noise), then decoded block by block straight from that file, downmixed,
resampled as one stream and written as the meeting's usual encrypted chunks.

WAV, FLAC, MP3, OGG and AIFF are decoded by soundfile. MP4, MOV and M4A, told apart by their first bytes, are
decoded by PyAV reading that same sealed file; their video is skipped and never stored.
"""

from __future__ import annotations

import bisect
import io
import math
import os
import secrets
import struct
import tempfile
from array import array
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from modwen.audio import SAMPLE_RATE
from modwen.audio.chunks import ChunkWriter

MAX_IMPORT_HOURS = 6.0
TMP_PREFIX = "import-"
TMP_SUFFIX = ".sealed"

# Refusals are shown to the user as they are; the UI matches them to show the Arabic.
NOT_SUPPORTED = ("This isn't a supported audio or video file. It may be copy-protected or use a format Modwen "
                 "can't read. Use WAV, FLAC, MP3, OGG, AIFF, MP4, MOV or M4A.")
NO_AUDIO = "This file has no audio, so there's nothing to transcribe. Choose a file with sound."
DAMAGED = "This file is damaged or incomplete, so nothing was imported. If you can, get a fresh copy and import that."
EMPTY = "This file is empty. Choose the original recording and import it again."
TOO_BIG = "This file is larger than 8 GB, the limit for imports. Use a smaller copy and import that."
IMPORT_FAILED = ("The import didn't finish, so the file wasn't imported. Check that this computer has free space "
                 "and that Modwen is running, then try again.")

# The first eight bytes of an MP4, MOV or M4A: a box size, then one of these box types.
MEDIA_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"styp", b"moof"}
# Audio codecs Modwen takes from those files, as PyAV names their decoders.
MEDIA_CODECS = {"aac", "alac", "mp3", "mp3float", "opus", "flac", "ac3", "eac3", "pcm_s16le", "pcm_s16be",
                "pcm_s24le", "pcm_s24be", "pcm_s32le", "pcm_s32be", "pcm_f32le", "pcm_f32be", "pcm_alaw",
                "pcm_mulaw"}
# Sample entries of encrypted tracks. ``enca`` decodes as plain "aac", so the codec can't show this.
ENCRYPTED_ENTRIES = {b"enca", b"drms", b"drmi", b"encs", b"encv"}
# Only encrypted files carry these. FFmpeg parses them wherever they are, and senc allocates per declared sample.
ENCRYPTION_BOXES = {b"sinf", b"schi", b"tenc", b"senc", b"pssh"}
NOT_READ = {b"cmov"} | ENCRYPTION_BOXES  # refused unread: they are unsupported, not damaged
SAMPLE_TABLE_PATH = (b"moov", b"trak", b"mdia", b"minf", b"stbl")
EDIT_LIST_PATH = (b"moov", b"trak", b"edts")
# What FFmpeg's mov demuxer descends into (mov.c, mov_read_default and its callers, FFmpeg 8.1 and 9.0). Check it again
# whenever PyAV moves: a table anywhere in one of these updates the current track, whatever the box around it.
MOV_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"dinf", b"mvex", b"moof", b"traf",
                  b"mfra", b"meta", b"wave", b"tref", b"ilst", b"iprp", b"ipco"}
TABLE_WIDTHS = {b"stts": 8, b"ctts": 8, b"stsc": 12, b"stco": 4, b"co64": 8, b"stss": 4, b"stps": 4}
SAMPLE_TABLES = {*TABLE_WIDTHS, b"stsz", b"stz2", b"stsd"}
# Inside a sample entry none of these is legitimate, whichever offset FFmpeg starts reading its atoms from.
ENTRY_FORBIDDEN = {*SAMPLE_TABLES, *NOT_READ, b"elst", b"hdlr", b"trak", b"moov", b"sbgp", b"sgpd"}
NEVER_PARSED = {b"free", b"skip", b"wide", b"mdat"}  # editors park old moovs in free; FFmpeg doesn't look inside
PCM_ENTRIES = {b"sowt", b"twos", b"in24", b"in32", b"fl32", b"fl64", b"lpcm", b"raw ", b"alaw", b"ulaw", b"ipcm",
               b"fpcm"}
TRACK_TYPES = {b"vide", b"soun", b"subp", b"clcp"}  # the handlers that set a track's type; others leave it alone
PCM_PACKET_SAMPLES = 1024  # mov_build_index groups uncompressed audio like this...
MIN_PACKET_SAMPLES = 160  # ...and never finer than this, whatever a sample entry declares
MAX_BOXES = 10_000  # shared across the moov, so a hostile file can't keep the check walking
MAX_TOP_BOXES = 4_000_000  # more than two boxes per 20 ms fragment over the 6-hour limit
MAX_FRAGMENT_BOXES = 6_000_000  # shared across every moof: five boxes per 20 ms fragment over the 6-hour limit
MAX_INDEX_ENTRIES = 8_000_000  # shared across tracks, including conservative edit-list expansion (~192 MB of indexes)
MAX_EDITS = 256  # ordinary trims use one or two; repeated edits can multiply native indexing and decode work
MAX_SHORTFALL_SECONDS = 0.1  # fragmented files only: their packets reach the declared end exactly when intact
LENGTH_SLACK_SECONDS = 1.0  # so a file of exactly 6 hours that decodes a few ms long still imports
MAX_PLAIN_HOURS = 10_000  # 8 GB of the leanest speech is about 4,000 hours; beyond this the length itself is false
# Top-level boxes of these files. One running past the end was cut off; anything else there is trailing junk.
KNOWN_BOXES = {b"moov", b"mdat", b"moof", b"mfra", b"free", b"skip", b"uuid", b"udta", b"ftyp", b"wide", b"meta"}


class ImportRefused(ValueError):
    """The file can't be imported; the message says why and what to do."""


class SealedTempFile(io.RawIOBase):
    """A seekable temp file encrypted with AES-CTR under a random in-memory key."""

    def __init__(self, directory: Path):
        super().__init__()
        self._key, self._iv = secrets.token_bytes(32), int.from_bytes(secrets.token_bytes(16), "big")
        fd, name = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=directory)
        self.path = Path(name)
        self._fh = os.fdopen(fd, "r+b")
        self._pos = 0
        self._size = 0

    def _crypt(self, data: bytes, offset: int) -> bytes:
        block, skip = divmod(offset, 16)
        ctr = ((self._iv + block) % (1 << 128)).to_bytes(16, "big")
        c = Cipher(algorithms.AES(self._key), modes.CTR(ctr)).encryptor()
        return c.update(b"\0" * skip + data)[skip:]

    # writing (append only)
    def append(self, data: bytes) -> None:
        self._fh.seek(self._size)
        self._fh.write(self._crypt(data, self._size))
        self._fh.flush()
        self._size += len(data)

    # reading (what soundfile needs)
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        n = max(0, min(n, self._size - self._pos))
        self._fh.seek(self._pos)
        out = self._crypt(self._fh.read(n), self._pos)
        self._pos += len(out)
        return out

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def tell(self) -> int:
        return self._pos

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            self.path.unlink(missing_ok=True)
            super().close()


def remove_leftovers(directory: Path) -> int:
    """Delete sealed temp files left by a crash (they can't be decrypted anyway)."""
    n = 0
    for p in Path(directory).glob(f"{TMP_PREFIX}*{TMP_SUFFIX}"):
        p.unlink(missing_ok=True)
        n += 1
    return n


class AudioInfo:
    def __init__(self, frames: int, rate: int, channels: int):
        self.frames, self.samplerate, self.channels = frames, rate, channels

    @property
    def duration(self) -> float:
        return self.frames / self.samplerate


def too_long_message(hours: float) -> str:
    """Rounded up, so 6.04 hours doesn't read "6 hours" against a limit of 6. A false header can claim any length,
    which is no use to the reader, so past ``MAX_PLAIN_HOURS`` this states the limit instead."""
    if hours >= MAX_PLAIN_HOURS:
        return (f"This recording is over {MAX_IMPORT_HOURS:g} hours long. Imports are limited to "
                f"{MAX_IMPORT_HOURS:g} hours. Trim or split it, then import it again.")
    shown = math.ceil(round(hours * 10, 6)) / 10
    return (f"This recording is {shown:g} hours long. Imports are limited to {MAX_IMPORT_HOURS:g} hours. "
            "Trim or split it, then import it again.")


def probe(sealed: SealedTempFile) -> AudioInfo:
    """Check the upload is a readable audio file of a sensible length; raises ImportRefused otherwise."""
    sealed.seek(0)
    try:
        with sf.SoundFile(sealed) as f:
            frames, rate, channels = f.frames, f.samplerate, f.channels
    except Exception as exc:
        raise ImportRefused(NOT_SUPPORTED) from exc
    if rate <= 0 or frames <= 0:
        raise ImportRefused(EMPTY)
    hours = frames / rate / 3600
    if hours > MAX_IMPORT_HOURS:
        raise ImportRefused(too_long_message(hours))
    return AudioInfo(frames, rate, channels)


def decode_into(sealed: SealedTempFile, writer: ChunkWriter, block_seconds: float = 10.0) -> int:
    """Decode the sealed upload block by block into ``writer`` (mono 16 kHz). Returns samples written."""
    sealed.seek(0)
    with sf.SoundFile(sealed) as f:
        rate = f.samplerate
        rs = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32") if rate != SAMPLE_RATE else None
        for block in f.blocks(blocksize=int(rate * block_seconds), dtype="float32", always_2d=True):
            mono = block.mean(axis=1).astype(np.float32)
            if rs is not None:
                mono = rs.resample_chunk(mono)
            writer.write(mono)
        if rs is not None:
            writer.write(rs.resample_chunk(np.zeros(0, dtype=np.float32), last=True))
    writer.close()
    return writer.total_samples


def is_media_container(sealed: SealedTempFile) -> bool:
    """True for MP4, MOV and M4A. The extension plays no part: the first bytes decide."""
    sealed.seek(0)
    head = sealed.read(8)
    if head[:4] in (b"RIFF", b"FORM"):  # WAV and AIFF hold a size there, which could spell a box type
        return False
    return head[4:] in MEDIA_BOXES


def _boxes(sealed, start: int, end: int, budget: list[int] | None = None, limit: int | None = None):
    """(type, body start, box end) of each box between two offsets. Stops quietly at a broken one, as FFmpeg does.
    Running out of budget or ``limit`` refuses the file, as a safety limit rather than as damage."""
    pos = start
    for _ in range(MAX_BOXES if limit is None else limit):
        if pos + 8 > end:
            return
        if budget is not None:
            budget[0] -= 1
            if budget[0] < 0:
                raise ImportRefused(NOT_SUPPORTED)
        sealed.seek(pos)
        head = sealed.read(16)
        size, kind, body = int.from_bytes(head[:4], "big"), head[4:8], pos + 8
        if size == 1:  # the real size follows, in 64 bits
            size, body = int.from_bytes(head[8:16], "big"), pos + 16
        elif size == 0:  # to the end of the file
            size = end - pos
        if size < body - pos:
            return
        yield kind, body, min(pos + size, end)
        pos += size
    if pos + 8 <= end:
        raise ImportRefused(NOT_SUPPORTED)


def is_fragmented(sealed: SealedTempFile) -> bool:
    """True if the moov announces fragments, so its sample table doesn't list the file's audio."""
    moov = next(((body, stop) for kind, body, stop in _boxes(sealed, 0, sealed.size) if kind == b"moov"), None)
    return moov is not None and any(kind == b"mvex" for kind, _, _ in _boxes(sealed, *moov))


def check_boxes(sealed: SealedTempFile) -> None:
    # Avoid a seek/decryption for every eight-byte padding box. Detach: the caller owns the sealed file.
    buffered = io.BufferedReader(sealed, buffer_size=64 * 1024)
    try:
        _check_boxes(buffered, sealed.size, sealed)
    finally:
        buffered.detach()


def _check_boxes(reader, size_bytes: int, sealed: SealedTempFile) -> None:
    """Refuse a file whose top-level boxes don't fit it: a known box running past the end means it was cut off, even
    if all the audio is there. A size of 0 means "to the end of the file", so it can show no cut.

    Devices and tools can leave bytes after the last box. Once the moov and the mdat are whole those are ignored if
    they are too short to be a box or don't name one, but not in a fragmented file, where they can be a lost fragment."""
    seen: set[bytes] = set()

    def end_of_boxes() -> None:
        if not {b"moov", b"mdat"} <= seen or b"moof" in seen or is_fragmented(sealed):
            raise ImportRefused(DAMAGED)

    pos = 0
    for _ in range(MAX_TOP_BOXES):
        if pos == size_bytes:
            return
        reader.seek(pos)
        head = reader.read(16)
        if len(head) < 8:
            return end_of_boxes()
        size, header, kind = int.from_bytes(head[:4], "big"), 8, head[4:8]
        if size == 0:
            return
        if size == 1:
            size, header = int.from_bytes(head[8:16], "big"), 16
        if len(head) < header or size < header or pos + size > size_bytes:
            if kind in KNOWN_BOXES:
                raise ImportRefused(DAMAGED)
            return end_of_boxes()
        if kind == b"moov" and kind in seen:
            raise ImportRefused(DAMAGED)
        seen.add(kind)
        pos += size
    raise ImportRefused(DAMAGED)


def is_encrypted(sealed: SealedTempFile, start: int = 0, end: int | None = None, depth: int = 0,
                 _budget: list[int] | None = None) -> bool:
    """True if any track's sample entry is an encrypted one (moov/trak/mdia/minf/stbl/stsd)."""
    end = sealed.size if end is None else end
    budget = [MAX_BOXES] if _budget is None else _budget
    for kind, body, stop in _boxes(sealed, start, end, budget):
        if depth == len(SAMPLE_TABLE_PATH):
            if kind == b"stsd" and any(k in ENCRYPTED_ENTRIES for k, _, _ in _boxes(sealed, body + 8, stop, budget)):
                return True
        elif kind == SAMPLE_TABLE_PATH[depth]:
            if is_encrypted(sealed, body, stop, depth + 1, budget):
                return True
            if depth == 0:  # there is one moov; the rest of the file is media
                return False
    return False


class _Preflight:
    """Everything ``_check_metadata`` knows about one file while it walks. See its docstring for the rules."""

    def __init__(self, reader, size: int):
        self.reader, self.size = reader, size
        self.tracks: list[dict] = []
        self.moov_budget, self.other_budget = [MAX_BOXES], [MAX_FRAGMENT_BOXES]
        self.table_work = self.fragments = 0
        self.has_moov = self.misplaced = False
        self.box_starts, self.references = array("Q", [0]), array("Q")

    def read(self, at: int, count: int, stop: int) -> bytes:
        if at + count > stop:
            raise ImportRefused(DAMAGED)
        self.reader.seek(at)
        data = self.reader.read(count)
        if len(data) != count:
            raise ImportRefused(DAMAGED)
        return data

    def records(self, body: int, stop: int, width: int, count_at: int = 4):
        """The rows of a counted table, in bounded blocks: never a whole table in memory."""
        count = int.from_bytes(self.read(body + count_at, 4, stop), "big")
        first = body + count_at + 4
        if first + count * width > stop:
            raise ImportRefused(DAMAGED)
        if count > MAX_INDEX_ENTRIES:
            raise ImportRefused(NOT_SUPPORTED)
        for row in range(0, count, 4096):
            data = self.read(first + row * width, min(4096, count - row) * width, stop)
            yield from struct.iter_unpack(">" + "I" * (width // 4), data)

    def blocks(self, start: int, stop: int):
        """(offset, bytes) in overlapping blocks, so a four-byte tag can't fall between two of them."""
        for at in range(start, stop, 64 * 1024):
            yield at, self.read(at, min(64 * 1024 + 3, stop - at), stop)

    def meta_start(self, body: int, stop: int) -> int | None:
        """mov_read_meta steps four bytes at a time to an hdlr, then parses boxes from that hdlr's size field."""
        for at, data in self.blocks(body, stop):
            found = data.find(b"hdlr")
            while found >= 0:
                if not (at + found - body) % 4:
                    return at + found - 4 if at + found > body else None
                found = data.find(b"hdlr", found + 1)
        return None

    def sample_entries(self, body: int, stop: int) -> list[tuple[bytes, int]]:
        """(type, samples per frame) of each entry. FFmpeg trusts an entry's size past the end of stsd, so one has to fit:
        otherwise it reads a sibling box as this entry's atoms. Each entry is searched whole, because where its atoms
        start depends on the track type and the file's brand, and neither has to be what it looks like here."""
        declared = int.from_bytes(self.read(body + 4, 4, stop), "big")
        if declared > MAX_BOXES or body + 8 + declared * 8 > stop:
            raise ImportRefused(DAMAGED)
        entries, at = [], body + 8
        for _ in range(declared):
            size, tag = struct.unpack(">I4s", self.read(at, 8, stop))
            if size < 8 or at + size > stop:
                raise ImportRefused(DAMAGED)
            if tag in ENCRYPTED_ENTRIES:
                raise ImportRefused(NOT_SUPPORTED)
            for _, data in self.blocks(at + 8, at + size):
                found = {t for t in ENTRY_FORBIDDEN if t in data}
                if found & NOT_READ:
                    raise ImportRefused(NOT_SUPPORTED)
                self.misplaced |= bool(found)
            entries.append((tag, self.samples_per_frame(at + 8, at + size) if tag in PCM_ENTRIES else 0))
            at += size
        return entries

    def samples_per_frame(self, entry: int, stop: int) -> int:
        """The field FFmpeg may read, or 2 (which ends the PCM exemption) if it can't be told."""
        version = int.from_bytes(self.read(entry + 8, 2, stop), "big") if entry + 10 <= stop else 3
        if version == 0:
            return 0
        at = entry + {1: 28, 2: 60}.get(version, stop)
        return int.from_bytes(self.read(at, 4, stop), "big") if at + 4 <= stop else 2

    def table(self, kind: bytes, body: int, stop: int, track: dict | None) -> None:
        entries = self.sample_entries(body, stop) if kind == b"stsd" else None
        if track is None or kind in track:
            self.misplaced = True
            return
        track[kind] = (body, stop)
        if entries is not None:
            track["entries"] = entries
        elif kind in TABLE_WIDTHS:
            count = int.from_bytes(self.read(body + 4, 4, stop), "big")
            if body + 8 + count * TABLE_WIDTHS[kind] > stop:
                raise ImportRefused(DAMAGED)
            self.table_work += count
            if self.table_work > MAX_INDEX_ENTRIES:
                raise ImportRefused(NOT_SUPPORTED)

    def edits(self, body: int, stop: int, track: dict | None) -> None:
        if track is None or b"elst" in track:
            self.misplaced = True
            return
        head = self.read(body, 8, stop)
        count = int.from_bytes(head[4:], "big")
        if head[0] > 1 or body + 8 + count * (20 if head[0] else 12) > stop:
            raise ImportRefused(DAMAGED)
        if count > MAX_EDITS:
            raise ImportRefused(NOT_SUPPORTED)
        track[b"elst"] = count

    def sample_group(self, body: int, stop: int, track: dict) -> None:
        """build_open_gop_key_points writes one integer per sample a sync group names."""
        if self.read(body + 4, 4, stop) == b"sync":
            count_at = 12 if self.read(body, 1, stop)[0] == 1 else 8
            track["sync"] = track.get("sync", 0) + sum(n for n, _ in self.records(body, stop, 8, count_at))

    def item_table(self, kind: bytes, body: int, stop: int) -> None:
        """iinf and iloc each clear 8 bytes per declared item before reading any of them."""
        version = self.read(body, 1, stop)[0]
        narrow = version == 0 if kind == b"iinf" else version < 2
        at = body + (4 if kind == b"iinf" else 6)
        count = int.from_bytes(self.read(at, 2 if narrow else 4, stop), "big")
        if at + count * 4 > stop:
            raise ImportRefused(DAMAGED)

    def segment_index(self, body: int, stop: int) -> None:
        """A sidx sends the demuxer to parse the root level at the offsets it names, after opening."""
        wide = self.read(body, 1, stop)[0] != 0
        at = body + (20 if wide else 16)
        offset = stop + int.from_bytes(self.read(at, 8 if wide else 4, stop), "big")
        at += 10 if wide else 6
        count = int.from_bytes(self.read(at, 2, stop), "big")
        for row in range(count):
            if offset > self.size:
                raise ImportRefused(DAMAGED)
            self.references.append(offset)
            offset += int.from_bytes(self.read(at + 2 + row * 12, 4, stop), "big") & 0x7fffffff
        if len(self.references) > MAX_TOP_BOXES:
            raise ImportRefused(NOT_SUPPORTED)

    def trun(self, body: int, stop: int) -> None:
        head = self.read(body, 8, stop)
        flags, count = int.from_bytes(head[:4], "big") & 0xffffff, int.from_bytes(head[4:], "big")
        offset = 8 + 4 * bool(flags & 1) + 4 * bool(flags & 4)
        if body + offset + count * 4 * (flags & 0xf00).bit_count() > stop:
            raise ImportRefused(DAMAGED)
        self.fragments += count
        if self.fragments > MAX_INDEX_ENTRIES:
            raise ImportRefused(NOT_SUPPORTED)

    def walk(self, start: int, end: int, budget: list[int], path: tuple = (), track: dict | None = None) -> None:
        if len(path) > 16:
            raise ImportRefused(NOT_SUPPORTED)
        top = not path
        for kind, body, stop in _boxes(self.reader, start, end, None if top else budget,
                                       MAX_TOP_BOXES if top else None):
            if kind in NOT_READ:
                raise ImportRefused(NOT_SUPPORTED)
            if kind == b"hoov" and body + 8 <= stop and self.read(body + 4, 4, stop) in (b"mvhd", b"cmov"):
                raise ImportRefused(NOT_SUPPORTED)  # FFmpeg reads such a box as the moov
            if kind == b"free" and body + 8 <= stop and self.read(body, 8, stop) == b"Anevia\x1a\x1a":
                raise ImportRefused(NOT_SUPPORTED)  # this makes FFmpeg parse wherever an mfra points
            if top:
                self.box_starts.append(stop)
            if kind == b"trak":
                self.misplaced |= path != (b"moov",)
                child: dict = {}
                self.tracks.append(child)
                self.walk(body, stop, budget, path + (kind,), child)
            elif kind == b"moov":
                self.misplaced |= not top
                self.has_moov = True
                self.walk(body, stop, self.moov_budget if top else budget, path + (kind,), track)
            elif kind in SAMPLE_TABLES:
                self.table(kind, body, stop, track if path == SAMPLE_TABLE_PATH else None)
            elif kind == b"elst":
                self.edits(body, stop, track if path == EDIT_LIST_PATH else None)
            elif kind in (b"sbgp", b"sgpd"):  # also legal in a traf, where nothing uses them any more
                self.misplaced |= path != SAMPLE_TABLE_PATH and path[-1:] != (b"traf",)
                if path == SAMPLE_TABLE_PATH and kind == b"sbgp":
                    self.sample_group(body, stop, track)
            elif kind == b"trun":
                self.trun(body, stop)
            elif kind == b"sidx":
                self.segment_index(body, stop)
            elif kind in (b"iinf", b"iloc"):
                self.item_table(kind, body, stop)
            elif kind == b"meta":
                first = self.meta_start(body, stop)
                if first is not None:
                    self.walk(first, stop, budget, path + (kind,), track)
            elif kind == b"hdlr":
                if track is not None and body + 12 <= stop:
                    track.setdefault("handlers", []).append(self.read(body + 8, 4, stop))
            elif kind in MOV_CONTAINERS:
                self.walk(body, stop, budget, path + (kind,), track)
            elif track is not None and kind not in NEVER_PARSED and len(path) < 16 and kind.isalnum() and kind.isascii():
                # Unknown here, and possibly known to a later FFmpeg: a table inside it still has no business there.
                self.walk(body, stop, budget, path + (kind,), track)

    def chunk_packets(self, track: dict, group: int) -> int:
        """Packets of mov_build_index's uncompressed-audio branch: one per ``group`` samples of each chunk."""
        offsets = track.get(b"stco", track.get(b"co64"))
        if offsets is None or b"stsc" not in track:
            return 0
        chunks = int.from_bytes(self.read(offsets[0] + 4, 4, offsets[1]), "big")
        if not chunks:
            return 0
        total, largest, ordered, previous = 0, 0, True, None
        for first, samples, _ in self.records(*track[b"stsc"], 12):
            ordered = ordered and first >= 1 and (previous is None or first > previous[0])
            largest = max(largest, samples)
            if previous and ordered:
                total += (first - previous[0]) * ((previous[1] + group - 1) // group)
            previous = (first, samples)
        if previous is None:
            return 0
        if not ordered:  # mov_read_stsc repairs these; the repair can't name more samples than the largest
            return chunks * ((largest + group - 1) // group)
        if previous[0] > chunks:  # sanity_checks refuses this too
            raise ImportRefused(DAMAGED)
        return total + (chunks - previous[0] + 1) * ((previous[1] + group - 1) // group)

    def track_work(self, track: dict) -> int:
        """The most native index entries this track can cost, before edits."""
        samples, fixed = 0, 0
        if b"stsz" in track:
            body, stop = track[b"stsz"]
            _, fixed, samples = struct.unpack(">III", self.read(body, 12, stop))
            if (fixed and fixed * samples > self.size) or (not fixed and body + 12 + samples * 4 > stop):
                raise ImportRefused(DAMAGED)
        elif b"stz2" in track:
            body, stop = track[b"stz2"]
            head = self.read(body, 12, stop)
            bits, samples = head[7], int.from_bytes(head[8:], "big")
            if bits not in (4, 8, 16) or body + 12 + (samples * bits + 7) // 8 > stop:
                raise ImportRefused(DAMAGED)
        runs, timed, delta = 0, 0, 0
        for n, duration in self.records(*track[b"stts"], 8) if b"stts" in track else ():
            runs, timed, delta = runs + 1, timed + n, duration
        offsets = sum(n for n, _ in self.records(*track[b"ctts"], 8)) if b"ctts" in track else 0
        # The last handler that sets a type wins, wherever it is: FFmpeg builds the index after the whole trak.
        kind = next((h for h in reversed(track.get("handlers", ())) if h in TRACK_TYPES), None)
        entries = track.get("entries", ())
        if (kind == b"soun" and runs == 1 and delta == 1 and b"ctts" not in track and entries and
                all(tag in PCM_ENTRIES and spf < 2 for tag, spf in entries)):
            # The raw sample count isn't a packet count here, and these entries can't set a smaller group.
            return max(self.chunk_packets(track, PCM_PACKET_SAMPLES), track.get("sync", 0))
        # stts durations can be clipped to 1 natively, so any track can reach the chunk branch.
        return max(samples, timed, offsets, track.get("sync", 0), self.chunk_packets(track, MIN_PACKET_SAMPLES))


def _check_metadata(sealed: SealedTempFile) -> None:
    """Refuse, BEFORE opening, any file whose metadata could make FFmpeg allocate or index without limit.

    FFmpeg's parser is context-free: a table inside anything it descends into updates the current track. So this works
    by position, not by predicting what FFmpeg would do: sample tables, edit lists and tracks are legal in one place
    each, and anywhere else refuses the file. What remains legal is counted for every track, video and unselected ones
    included, and compressed metadata is never decompressed.

    Over a limit, an intact file isn't supported; a table that is misplaced or doesn't fit is damage. These are resource
    bounds, not a sandbox or a complete validation of FFmpeg's metadata grammar.
    """
    reader = io.BufferedReader(sealed, buffer_size=64 * 1024)
    try:
        check = _Preflight(reader, sealed.size)
        check.walk(0, sealed.size, check.other_budget)
        if check.misplaced or not check.has_moov:  # with no moov, FFmpeg looks inside free boxes for one
            raise ImportRefused(DAMAGED)
        for offset in check.references:  # anything but a box walked above would be metadata nobody checked
            at = bisect.bisect_left(check.box_starts, offset)
            if at == len(check.box_starts) or check.box_starts[at] != offset:
                raise ImportRefused(DAMAGED)
        total = check.fragments
        for track in check.tracks:
            total += check.track_work(track) * max(1, track.get(b"elst", 1))
            if total > MAX_INDEX_ENTRIES:
                raise ImportRefused(NOT_SUPPORTED)
    finally:
        reader.detach()


def _pick_audio(container):
    """The track the file marks as default, else its first audio track. No fallback if that one is unusable."""
    from av.stream import Disposition

    if len(container.streams) == 0:  # a damaged moov can leave no track at all: that isn't "no audio"
        raise ImportRefused(DAMAGED)
    tracks = list(container.streams.audio)
    if not tracks:
        raise ImportRefused(NO_AUDIO)
    track = next((t for t in tracks if t.disposition & Disposition.default), tracks[0])
    if track.codec_context is None or track.codec_context.name not in MEDIA_CODECS:
        raise ImportRefused(NOT_SUPPORTED)
    return track


def _decode_track(container, track, writer: ChunkWriter, longest: float, promised: float | None = None,
                  expected: int = 0) -> float:
    """Downmix and resample one track into ``writer`` as ``decode_into`` does. Returns the seconds decoded.

    A cut on a box boundary ends with no error at all: the only sign is what is missing. A file with a sample table
    lists its packets, so all ``expected`` of them must arrive. A fragmented one has only the time it ``promised``,
    measured on its own timeline, so a gap in the audio doesn't count as lost."""
    import av

    planar = av.AudioResampler(format="fltp")
    shape = rs = None
    rate = samples = arrived = 0
    reached = 0.0

    def write(frame) -> None:
        nonlocal samples
        mono = frame.to_ndarray().mean(axis=0).astype(np.float32)
        samples += mono.size
        if samples / rate > longest:
            raise ImportRefused(too_long_message(samples / rate / 3600))
        # A maliciously low rate must not expand one frame to gigabytes before the length check.
        block = max(1, min(rate, 16384))
        for at in range(0, mono.size, block):
            part = mono[at:at + block]
            writer.write(rs.resample_chunk(part) if rs is not None else part)

    try:
        for packet in container.demux(track):
            if packet.is_corrupt:
                raise ImportRefused(DAMAGED)
            if packet.dts is not None:  # the last packet is an empty one, to flush the decoder
                arrived += 1
                reached = max(reached, float((packet.dts + (packet.duration or 0)) * packet.time_base))
            for frame in packet.decode():
                now = (frame.sample_rate, frame.layout.name, frame.format.name)
                if shape is None:
                    shape, rate = now, frame.sample_rate
                    if rate <= 0:
                        raise ImportRefused(DAMAGED)
                    if rate != SAMPLE_RATE:
                        rs = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")
                elif now != shape:
                    raise ImportRefused(DAMAGED)
                for plain in planar.resample(frame):
                    write(plain)
    except av.error.FFmpegError as exc:  # disk and memory failures on our side are Python errors, not these
        raise ImportRefused(DAMAGED) from exc
    if arrived < expected or (promised and promised - reached > MAX_SHORTFALL_SECONDS):
        raise ImportRefused(DAMAGED)
    if shape is None:
        return 0.0
    for plain in planar.resample(None):
        write(plain)
    if rs is not None:
        writer.write(rs.resample_chunk(np.zeros(0, dtype=np.float32), last=True))
    return samples / rate


def decode_media_into(sealed: SealedTempFile, writer: ChunkWriter) -> int:
    """Decode an MP4, MOV or M4A into ``writer`` (mono 16 kHz), or raise ImportRefused. Returns samples written.

    Forcing the mov demuxer keeps playlist formats such as HLS, which open other files or URLs, out of reach."""
    import av

    if is_encrypted(sealed):
        raise ImportRefused(NOT_SUPPORTED)
    check_boxes(sealed)
    _check_metadata(sealed)
    fragmented = is_fragmented(sealed)
    sealed.seek(0)
    try:
        kwargs = {"metadata_errors": "replace"} if int(av.__version__.split(".")[0]) < 19 else {}
        container = av.open(sealed, format="mov", options={"enable_drefs": "0", "protocol_whitelist": ""}, **kwargs)
    except av.error.FFmpegError as exc:
        raise ImportRefused(DAMAGED) from exc
    with container:
        if sum(len(st.index_entries) for st in container.streams) > MAX_INDEX_ENTRIES:
            raise ImportRefused(NOT_SUPPORTED)
        track = _pick_audio(container)
        declared = float(track.duration * track.time_base) if track.duration else None
        known = declared or (container.duration or 0) / 1_000_000
        if known > MAX_IMPORT_HOURS * 3600:
            raise ImportRefused(too_long_message(known / 3600))
        # FFmpeg's index, not st.frames: edit lists drop samples from it, and PCM counts groups of samples, not packets.
        expected, promised = (0, declared) if fragmented else (len(track.index_entries), None)
        decoded = _decode_track(container, track, writer, MAX_IMPORT_HOURS * 3600 + LENGTH_SLACK_SECONDS, promised,
                                expected)
    if not decoded:
        raise ImportRefused(DAMAGED if declared or fragmented else EMPTY)
    writer.close()
    return writer.total_samples
