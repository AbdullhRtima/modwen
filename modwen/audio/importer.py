"""Import a recording file without ever writing it to disk in the clear, and without loading it into memory.

The upload is streamed into a ``SealedTempFile`` (AES-CTR with a key that only exists in memory: after a crash
the leftover file is unreadable noise), then decoded block by block straight from that file, downmixed,
resampled as one stream and written as the meeting's usual encrypted chunks.

WAV, FLAC, MP3, OGG and AIFF are decoded by soundfile. MP4, MOV and M4A, told apart by their first bytes, are
decoded by PyAV reading that same sealed file; their video is skipped and never stored. That decoding runs only in
the limited child process of ``modwen.audio.decoder``: a small hostile file can make FFmpeg allocate gigabytes or
crash, and this module's checks are about what a file means, not about containing that.
"""

from __future__ import annotations

import io
import math
import os
import secrets
import tempfile
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
ENCRYPTED_ENTRIES = {b"enca", b"drms", b"drmi", b"encs", b"encv", b"aavd"}
# Only protected files carry these, and compressed metadata isn't read here. All are refused unread, as unsupported.
NOT_READ = {b"cmov", b"sinf", b"schi", b"tenc", b"senc", b"pssh", b"adrm"}
# What FFmpeg's mov demuxer descends into (mov.c, mov_read_default and its callers, FFmpeg 8.1 and 9.0), plus hoov,
# which it reads as a moov. Anything else, such as a udta item, is opaque to it, so it stays opaque here.
MOV_CONTAINERS = {b"moov", b"hoov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"dinf", b"mvex", b"moof",
                  b"traf", b"mfra", b"meta", b"tref", b"ilst", b"iprp", b"ipco"}
MAX_BOXES = 10_000  # shared across the moov, so a hostile file can't keep the check walking
MAX_DEPTH = 16
MAX_TOP_BOXES = 4_000_000  # more than two boxes per 20 ms fragment over the 6-hour limit
MAX_FRAGMENT_BOXES = 6_000_000  # shared across every moof: five boxes per 20 ms fragment over the 6-hour limit
MAX_INDEX_ENTRIES = 16_000_000  # counted after opening, across tracks: about 600 MiB, past eight 6-hour AAC tracks
MAX_SHORTFALL_SECONDS = 0.1  # fragmented files only: their packets reach the declared end exactly when intact
LENGTH_SLACK_SECONDS = 1.0  # so a file of exactly 6 hours that decodes a few ms long still imports
MAX_PLAIN_HOURS = 10_000  # 8 GB of the leanest speech is about 4,000 hours; beyond this the length itself is false
# Top-level boxes of these files. One running past the end was cut off; anything else there is trailing junk.
KNOWN_BOXES = {b"moov", b"mdat", b"moof", b"mfra", b"free", b"skip", b"uuid", b"udta", b"ftyp", b"wide", b"meta"}
# Bytes a tfhd's flags add after its track id: base data offset, sample description, default duration, size and flags.
# FFmpeg's mov_read_tfhd reads these, in this order, whatever the box's own size says.
TFHD_FIELDS = {0x01: 8, 0x02: 4, 0x08: 4, 0x10: 4, 0x20: 4}


# The decoder child sends these numbers, never text.
REFUSALS = {1: NOT_SUPPORTED, 2: NO_AUDIO, 3: DAMAGED, 4: EMPTY}
TOO_LONG_CODE = 5


class ImportRefused(ValueError):
    """The file can't be imported; the message says why and what to do."""


class TooLong(ImportRefused):
    def __init__(self, hours: float):
        super().__init__(too_long_message(hours))
        self.hours = hours


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
        self._owner = True

    @classmethod
    def reopen(cls, path: str | Path, key: bytes, iv: int, size: int) -> SealedTempFile:
        """Read another process's sealed file. Closing it leaves the file to its owner."""
        self = cls.__new__(cls)
        io.RawIOBase.__init__(self)
        self._key, self._iv, self._size, self._pos, self._owner = key, iv, size, 0, False
        self.path = Path(path)
        self._fh = open(self.path, "rb")
        return self

    def handoff(self) -> dict:
        """What ``reopen`` needs. This key unlocks only this upload, and only the decoder child ever gets it."""
        return {"path": str(self.path), "key": self._key.hex(), "iv": self._iv, "size": self._size}

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
            if self._owner:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:  # still held open somewhere: unreadable noise, and the next start removes it
                    pass
            super().close()


def remove_leftovers(directory: Path) -> int:
    """Delete sealed temp files left by a crash (they can't be decrypted anyway)."""
    n = 0
    for p in Path(directory).glob(f"{TMP_PREFIX}*{TMP_SUFFIX}"):
        try:
            p.unlink(missing_ok=True)
        except OSError:  # a dying decoder child can still hold it for a moment; the next start gets it
            continue
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


def _boxes(sealed, start: int, end: int, budget: list[int] | None = None, limit: int | None = None,
           declared: bool = False, strict: bool = False):
    """(type, body start, box end) of each box between two offsets. Stops quietly at a broken one, as FFmpeg does.
    Running out of budget or ``limit`` refuses the file, as a safety limit rather than as damage.

    With ``declared``, the box end is the one its size claims, even past ``end``, and None for a size of 0. With
    ``strict``, a box too small for its own header, or a 64-bit size with no room for it, is damage when bytes follow
    its first eight: FFmpeg stops reading the parent there, skipping whatever comes after."""
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
        to_end = size == 0
        if size == 1:  # the real size follows, in 64 bits
            size, body = int.from_bytes(head[8:16], "big"), pos + 16
        elif to_end:  # to the end of the file
            size = end - pos
        if size < body - pos or (strict and body > end):
            if strict and pos + 8 < end:
                raise ImportRefused(DAMAGED)
            return
        if declared:
            yield kind, body, None if to_end else pos + size
        else:
            yield kind, body, min(pos + size, end)
        pos += size
    if pos + 8 <= end:
        raise ImportRefused(NOT_SUPPORTED)


def _find_moov(sealed: SealedTempFile) -> tuple[int, int] | None:
    return next(((body, stop) for kind, body, stop in _boxes(sealed, 0, sealed.size, None, MAX_TOP_BOXES)
                 if kind == b"moov"), None)


def is_fragmented(sealed: SealedTempFile) -> bool:
    """True if the moov announces fragments, so its sample table doesn't list the file's audio."""
    moov = _find_moov(sealed)
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
            if kind not in KNOWN_BOXES and is_fragmented(sealed):  # FFmpeg skips it, and every fragment after it
                raise ImportRefused(DAMAGED)
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


def _check_mdhd(reader, body: int, stop: int) -> None:
    """Without a timescale FFmpeg guesses the movie's, so an intact edit list then keeps a fraction of the audio."""
    reader.seek(body)
    at = 20 if reader.read(1) == b"\x01" else 12
    reader.seek(body + at)
    if body + at + 4 > stop or int.from_bytes(reader.read(4), "big") == 0:
        raise ImportRefused(DAMAGED)


def _protected(reader, start: int, end: int, budget: list[int], depth: int, file_size: int,
               path: tuple[bytes, ...] = ()) -> bool:
    timed = False
    for kind, body, stop in _boxes(reader, start, end, budget):
        if kind in NOT_READ:
            return True
        if kind == b"mdhd" and path[1:] == (b"trak", b"mdia"):
            _check_mdhd(reader, body, stop)
            timed = True
        if kind == b"stsz":
            # This is a completeness check, not an allocation estimate. A sample cannot contain more bytes than
            # the entire upload. In particular, FFmpeg reports ENOMEM for an oversized sample, hiding its damage.
            reader.seek(body)
            header = reader.read(12)
            if len(header) != 12:
                raise ImportRefused(DAMAGED)
            sample_size, count = int.from_bytes(header[4:8], "big"), int.from_bytes(header[8:12], "big")
            if sample_size:
                if sample_size > file_size:
                    raise ImportRefused(DAMAGED)
            else:
                if count * 4 > stop - body - 12:
                    raise ImportRefused(DAMAGED)
                while count:
                    amount = min(count, 16384)
                    values = reader.read(amount * 4)
                    if len(values) != amount * 4:
                        raise ImportRefused(DAMAGED)
                    # Reused chunk offsets can legally refer to the same stored bytes, so do not sum the sizes.
                    if int(np.frombuffer(values, dtype=">u4").max()) > file_size:
                        raise ImportRefused(DAMAGED)
                    count -= amount
        if kind == b"stsd":
            if any(tag in ENCRYPTED_ENTRIES for tag, _, _ in _boxes(reader, body + 8, stop, budget)):
                return True
        elif kind in MOV_CONTAINERS:
            if depth >= MAX_DEPTH:
                raise ImportRefused(NOT_SUPPORTED)
            if _protected(reader, body + (4 if kind == b"meta" else 0), stop, budget, depth + 1, file_size,
                          (*path, kind)):
                return True
    if path[1:] == (b"trak", b"mdia") and not timed:  # a box every track must have; a udta item called mdia isn't one
        raise ImportRefused(DAMAGED)
    return False


def is_encrypted(sealed: SealedTempFile) -> bool:
    """True if any box FFmpeg would read is an encrypted sample entry or a sign of protection, at any depth.

    AC8 needs this for its message: an encrypted track can decode as noise, or be called damaged. It keeps nothing
    safe; a file built to get past it only reaches the decoder child, which holds the limits."""
    reader = io.BufferedReader(sealed, buffer_size=64 * 1024)  # detached below: the caller owns the sealed file
    moov_budget, other_budget = [MAX_BOXES], [MAX_FRAGMENT_BOXES]  # one for the moov, one across every moof
    try:
        for kind, body, stop in _boxes(reader, 0, sealed.size, None, MAX_TOP_BOXES):
            if kind in NOT_READ:
                return True
            if kind in MOV_CONTAINERS:
                budget = moov_budget if kind in (b"moov", b"hoov") else other_budget
                if _protected(reader, body + (4 if kind == b"meta" else 0), stop, budget, 1, sealed.size, (kind,)):
                    return True
        return False
    finally:
        reader.detach()


def check_fragments(sealed: SealedTempFile, moov: tuple[int, int] | None) -> None:
    """Refuse a fragment whose header FFmpeg can't read whole: it skips that fragment with no error, so the file
    would import shorter, every byte of it present. Valid files never have these; a damaged header that still looks
    valid, such as a changed decode time, can't be told apart and isn't caught here.

    ``moov`` is the one the caller found. In a moof, a box of size 0 runs to the moof's end, which loses nothing only
    if it is the traf itself."""
    if moov is None:
        return
    reader = io.BufferedReader(sealed, buffer_size=64 * 1024)  # detached below: the caller owns the sealed file
    try:
        tracks = _track_ids(reader, *moov)
        budget = [MAX_FRAGMENT_BOXES]
        for kind, body, stop in _boxes(reader, 0, sealed.size, None, MAX_TOP_BOXES):
            if kind == b"moof":
                for inner, start, end in _boxes(reader, body, stop, budget, declared=True, strict=True):
                    if end is None and inner != b"traf":
                        raise ImportRefused(DAMAGED)
                    if inner == b"traf":
                        _check_traf(reader, start, stop if end is None else min(end, stop), budget, tracks)
    finally:
        reader.detach()


def _track_ids(reader, start: int, end: int) -> set[int]:
    """The track ids the moov's tkhd boxes give, which FFmpeg matches a fragment's tfhd against."""
    ids = set()
    for kind, body, stop in _boxes(reader, start, end):
        if kind == b"trak":
            for inner, at, _ in _boxes(reader, body, stop):
                if inner == b"tkhd":
                    reader.seek(at)
                    head = reader.read(24)
                    where = 20 if head[:1] == b"\x01" else 12
                    if len(head) >= where + 4:
                        ids.add(int.from_bytes(head[where:where + 4], "big"))
    return ids


def _check_traf(reader, start: int, end: int, budget: list[int], tracks: set[int]) -> None:
    """A tfhd or tfdt that runs past its traf, or to its end with a size of 0, takes the trun after it along; a tfhd too
    short for the fields its flags declare makes FFmpeg read the next box as those fields; one naming no track is
    skipped. Any of these loses the fragment. The tfhd's version is left alone, as FFmpeg skips it."""
    for kind, body, ends in _boxes(reader, start, end, budget, declared=True, strict=True):
        if kind not in (b"tfhd", b"tfdt"):
            continue
        if ends is None or ends > end:
            raise ImportRefused(DAMAGED)
        if kind == b"tfhd":
            reader.seek(body)
            head = reader.read(8)
            flags = int.from_bytes(head[1:4], "big")
            needs = 8 + sum(size for bit, size in TFHD_FIELDS.items() if flags & bit)
            if len(head) < 8 or body + needs > ends or int.from_bytes(head[4:8], "big") not in tracks:
                raise ImportRefused(DAMAGED)


def _finite(samples: np.ndarray) -> np.ndarray:
    """Silence in place of NaN and infinity. One such sample in a float PCM file isn't damage, but the resampler would
    spread it, and the parent rightly refuses any non-finite audio from this process."""
    return samples if np.isfinite(samples).all() else np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)


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
                  expected: int = 0, beat=lambda: None) -> float:
    """Downmix and resample one track into ``writer`` as ``decode_into`` does. Returns the seconds decoded.

    A cut on a box boundary ends with no error at all: the only sign is what is missing. A file with a sample table
    lists its packets, so all ``expected`` of them must arrive. A fragmented one has only the time it ``promised``,
    measured on its own timeline, so a gap in the audio doesn't count as lost."""
    import av

    packed = av.AudioResampler(format="flt")  # to_ndarray misreads planes from 8 channels up: a crash on av 18
    shape = rs = None
    rate = samples = arrived = 0
    reached = 0.0
    heard = False  # any finite sample at all, before cleaning

    def write(frame) -> None:
        nonlocal samples, heard
        raw = frame.to_ndarray().reshape(-1, frame.layout.nb_channels)
        heard = heard or bool(np.isfinite(raw).any())  # checked only until the first finite sample
        channels = _finite(raw)  # so one channel keeps the other
        mono = _finite(channels.mean(axis=1).astype(np.float32))  # huge finite values can overflow the sum
        samples += mono.size
        if samples / rate > longest:
            raise TooLong(samples / rate / 3600)
        # A maliciously low rate must not expand one frame to gigabytes before the length check.
        block = max(1, min(rate, 16384))
        for at in range(0, mono.size, block):
            part = mono[at:at + block]
            writer.write(_finite(rs.resample_chunk(part)) if rs is not None else part)

    try:
        for packet in container.demux(track):
            if packet.is_corrupt:
                raise ImportRefused(DAMAGED)
            if packet.dts is not None:  # the last packet is an empty one, to flush the decoder
                arrived += 1
                if not arrived % 1000:
                    beat()
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
                for plain in packed.resample(frame):
                    write(plain)
    except MemoryError:  # first, as av.error.MemoryError is both. Running out says nothing about the file
        raise
    except av.error.FFmpegError as exc:  # disk failures on our side are Python errors, not these
        raise ImportRefused(DAMAGED) from exc
    if arrived < expected or (promised and promised - reached > MAX_SHORTFALL_SECONDS):
        raise ImportRefused(DAMAGED)
    if shape is None:
        return 0.0
    for plain in packed.resample(None):
        write(plain)
    if rs is not None:
        writer.write(_finite(rs.resample_chunk(np.zeros(0, dtype=np.float32), last=True)))
    if samples and not heard:  # NaN or infinity throughout: no audio to clean, only damage that would import as silence
        raise ImportRefused(DAMAGED)
    return samples / rate


def decode_media_into(sealed: SealedTempFile, writer: ChunkWriter, beat=lambda: None) -> int:
    """Decode an MP4, MOV or M4A into ``writer`` (mono 16 kHz), or raise ImportRefused. Returns samples written.

    This is the decoder child's body: outside tests, only ``modwen.audio.decoder_child`` calls it. ``beat`` tells the
    parent the work is moving. Forcing the mov demuxer keeps playlist formats such as HLS, which open other files or
    URLs, out of reach."""
    import av

    if is_encrypted(sealed):
        raise ImportRefused(NOT_SUPPORTED)
    beat()
    check_boxes(sealed)
    beat()
    moov = _find_moov(sealed)
    if moov is None:  # with no moov, FFmpeg looks inside free boxes for one
        raise ImportRefused(DAMAGED)
    fragmented = is_fragmented(sealed)
    if fragmented:
        check_fragments(sealed, moov)
        beat()
    sealed.seek(0)
    try:
        kwargs = {"metadata_errors": "replace"} if int(av.__version__.split(".")[0]) < 19 else {}
        container = av.open(sealed, format="mov", options={"enable_drefs": "0", "protocol_whitelist": ""}, **kwargs)
    except MemoryError:
        raise
    except av.error.FFmpegError as exc:
        raise ImportRefused(DAMAGED) from exc
    beat()
    with container:
        if sum(len(st.index_entries) for st in container.streams) > MAX_INDEX_ENTRIES:
            raise ImportRefused(NOT_SUPPORTED)
        track = _pick_audio(container)
        declared = float(track.duration * track.time_base) if track.duration else None
        known = declared or (container.duration or 0) / 1_000_000
        if known > MAX_IMPORT_HOURS * 3600:
            raise TooLong(known / 3600)
        # FFmpeg's index, not st.frames: edit lists drop samples from it, and PCM counts groups of samples, not packets.
        expected, promised = (0, declared) if fragmented else (len(track.index_entries), None)
        decoded = _decode_track(container, track, writer, MAX_IMPORT_HOURS * 3600 + LENGTH_SLACK_SECONDS, promised,
                                expected, beat)
    if not decoded:
        raise ImportRefused(DAMAGED if declared or fragmented else EMPTY)
    writer.close()
    return writer.total_samples
