"""Meeting playback as a seekable WAV stream, rendered on demand from the encrypted chunks.

A request for a byte range decodes only the one or two 30 s chunks it touches, so playback starts at once and
memory stays flat however long the meeting is (the old player decoded and mixed the whole meeting first).
"""

from __future__ import annotations

import io
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from modwen.audio import SAMPLE_RATE
from modwen.crypto import decrypt
from modwen.pipeline.merge import gate_to_lines

HEADER_BYTES = 44
BYTES_PER_SAMPLE = 2  # 16-bit mono


def wav_header(n_samples: int) -> bytes:
    data = n_samples * BYTES_PER_SAMPLE
    return (b"RIFF" + struct.pack("<I", 36 + data) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE,
            SAMPLE_RATE * BYTES_PER_SAMPLE, BYTES_PER_SAMPLE, 16) + b"data" + struct.pack("<I", data))


@dataclass
class _Chunk:
    path: Path
    start: int  # first sample of this chunk in the channel
    n: int


class MeetingAudio:
    """Index of a meeting's chunks (per channel), plus a small cache of decoded chunks."""

    def __init__(self, mdir: Path, key: bytes, mic_lines: list[dict] | None):
        self.key = key
        self.mic_lines = mic_lines  # None: play the mic as recorded; a list: keep the mic only in those lines
        self.channels: dict[str, list[_Chunk]] = {}
        for p in sorted(Path(mdir).glob("*-*.flac.enc")):
            ch = p.name.split("-")[0]
            chunks = self.channels.setdefault(ch, [])
            start = chunks[-1].start + chunks[-1].n if chunks else 0
            with sf.SoundFile(io.BytesIO(self._raw(p, ch))) as f:
                chunks.append(_Chunk(p, start, f.frames))
        self.total = max((c[-1].start + c[-1].n for c in self.channels.values() if c), default=0)
        self._cache: OrderedDict[Path, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def _raw(self, p: Path, ch: str) -> bytes:
        return decrypt(self.key, p.read_bytes(), ch.encode())

    def _decoded(self, c: _Chunk, ch: str) -> np.ndarray:
        with self._lock:
            hit = self._cache.get(c.path)
            if hit is not None:
                self._cache.move_to_end(c.path)
                return hit
        data, _ = sf.read(io.BytesIO(self._raw(c.path, ch)), dtype="float32")
        if ch == "mic" and self.mic_lines is not None:
            # Without headphones the mic also hears the call a little late: mixed in, remote voices would play
            # twice. Keep the mic only where a local voice was transcribed (lines are in meeting time). The gain
            # is computed with a margin around the chunk, so a line that crosses a chunk boundary has no dip.
            margin = SAMPLE_RATE  # 1 s, more than the gate's padding + fade
            origin_ms = (c.start - margin) * 1000 // SAMPLE_RATE
            span_ms = (c.n + 2 * margin) * 1000 // SAMPLE_RATE
            lines = [{"start_ms": ln["start_ms"] - origin_ms, "end_ms": ln["end_ms"] - origin_ms}
                     for ln in self.mic_lines if ln["end_ms"] - origin_ms > 0 and ln["start_ms"] - origin_ms < span_ms]
            gain = gate_to_lines(np.ones(c.n + 2 * margin, dtype=np.float32), lines)[margin: margin + c.n]
            data = data * gain
        with self._lock:
            self._cache[c.path] = data
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
        return data

    def render(self, start: int, n: int) -> np.ndarray:
        """Mixed mono samples [start, start + n)."""
        n = max(0, min(n, self.total - start))
        out = np.zeros(n, dtype=np.float32)
        for ch, chunks in self.channels.items():
            for c in chunks:
                a, b = max(start, c.start), min(start + n, c.start + c.n)
                if a >= b:
                    continue
                data = self._decoded(c, ch)
                out[a - start: b - start] += data[a - c.start: b - c.start]
        return out

    def wav_bytes(self, first: int, last: int) -> bytes:
        """Bytes [first, last] (inclusive) of the WAV file this meeting would be."""
        size = HEADER_BYTES + self.total * BYTES_PER_SAMPLE
        last = min(last, size - 1)
        if first > last:
            return b""
        out = b""
        if first < HEADER_BYTES:
            out += wav_header(self.total)[first: min(last + 1, HEADER_BYTES)]
        d0, d1 = max(first, HEADER_BYTES) - HEADER_BYTES, last + 1 - HEADER_BYTES
        if d1 > d0:
            s0, s1 = d0 // BYTES_PER_SAMPLE, -(-d1 // BYTES_PER_SAMPLE)
            pcm = (np.clip(self.render(s0, s1 - s0), -1.0, 1.0) * 32767).astype("<i2").tobytes()
            off = d0 - s0 * BYTES_PER_SAMPLE
            out += pcm[off: off + (d1 - d0)]
        return out

    @property
    def size(self) -> int:
        return HEADER_BYTES + self.total * BYTES_PER_SAMPLE
