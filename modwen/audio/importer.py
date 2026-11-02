"""Import a recording file without ever writing it to disk in the clear, and without loading it into memory.

The upload is streamed into a ``SealedTempFile`` (AES-CTR with a key that only exists in memory: after a crash
the leftover file is unreadable noise), then decoded block by block straight from that file, downmixed,
resampled as one stream and written as the meeting's usual encrypted chunks.

WAV, FLAC, MP3, OGG and AIFF are decoded by soundfile. MP4, MOV and M4A, told apart by their first bytes, are
decoded by PyAV reading that same sealed file; their video is skipped and never stored.
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
ENCRYPTED_ENTRIES = {b"enca", b"drms", b"drmi", b"encs", b"encv"}
SAMPLE_TABLE_PATH = (b"moov", b"trak", b"mdia", b"minf", b"stbl")
MAX_BOXES = 10_000  # per level, so a hostile file can't keep the check walking
MAX_TOP_BOXES = 4_000_000  # more than two boxes per 20 ms fragment over the 6-hour limit
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


def _boxes(sealed: SealedTempFile, start: int, end: int):
    """(type, body start, box end) of each box between two offsets. Stops quietly at a broken one."""
    pos = start
    for _ in range(MAX_BOXES):
        if pos + 8 > end:
            return
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


def is_fragmented(sealed: SealedTempFile) -> bool:
    """True if the moov announces fragments, so its sample table doesn't list the file's audio."""
    moov = next(((body, stop) for kind, body, stop in _boxes(sealed, 0, sealed.size) if kind == b"moov"), None)
    return moov is not None and any(kind == b"mvex" for kind, _, _ in _boxes(sealed, *moov))


def check_boxes(sealed: SealedTempFile) -> None:
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
        if pos == sealed.size:
            return
        sealed.seek(pos)
        head = sealed.read(16)
        if len(head) < 8:
            return end_of_boxes()
        size, header, kind = int.from_bytes(head[:4], "big"), 8, head[4:8]
        if size == 0:
            return
        if size == 1:
            size, header = int.from_bytes(head[8:16], "big"), 16
        if len(head) < header or size < header or pos + size > sealed.size:
            if kind in KNOWN_BOXES:
                raise ImportRefused(DAMAGED)
            return end_of_boxes()
        seen.add(kind)
        pos += size
    raise ImportRefused(DAMAGED)


def is_encrypted(sealed: SealedTempFile, start: int = 0, end: int | None = None, depth: int = 0) -> bool:
    """True if any track's sample entry is an encrypted one (moov/trak/mdia/minf/stbl/stsd)."""
    end = sealed.size if end is None else end
    for kind, body, stop in _boxes(sealed, start, end):
        if depth == len(SAMPLE_TABLE_PATH):
            if kind == b"stsd" and any(k in ENCRYPTED_ENTRIES for k, _, _ in _boxes(sealed, body + 8, stop)):
                return True
        elif kind == SAMPLE_TABLE_PATH[depth]:
            if is_encrypted(sealed, body, stop, depth + 1):
                return True
            if depth == 0:  # there is one moov; the rest of the file is media
                return False
    return False


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
        writer.write(rs.resample_chunk(mono) if rs is not None else mono)
        if samples / rate > longest:
            raise ImportRefused(too_long_message(samples / rate / 3600))

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
    fragmented = is_fragmented(sealed)
    sealed.seek(0)
    try:
        container = av.open(sealed, format="mov")
    except av.error.FFmpegError as exc:
        raise ImportRefused(DAMAGED) from exc
    with container:
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
