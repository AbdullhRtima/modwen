"""Crash-safe, encrypted audio chunks.

Each channel ("mic", "system", "room") is written as a series of ~30 s FLAC chunks, each sealed with
AES-GCM using the meeting's data key: ``<meeting_dir>/<channel>-0001.flac.enc``. A crash loses at most
the chunk in progress.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from modwen.audio import SAMPLE_RATE
from modwen.crypto import decrypt, encrypt


def to_mono_16k(data: np.ndarray, sample_rate: int) -> np.ndarray:
    """Downmix to mono float32 and resample to 16 kHz."""
    a = np.asarray(data, dtype=np.float32)
    if a.ndim == 2:
        a = a.mean(axis=1)
    if sample_rate != SAMPLE_RATE and a.size:
        a = soxr.resample(a, sample_rate, SAMPLE_RATE).astype(np.float32)
    return a


def load_audio_file(path: str | Path) -> np.ndarray:
    """Read any libsndfile-supported file (wav, flac, ogg, mp3, aiff) as mono 16 kHz float32."""
    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    return to_mono_16k(data, sr)


class ChunkWriter:
    def __init__(self, meeting_dir: Path, channel: str, data_key: bytes, chunk_seconds: int = 30):
        self.dir = Path(meeting_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.channel = channel
        self.key = data_key
        self.chunk_samples = int(chunk_seconds * SAMPLE_RATE)
        self._buf: list[np.ndarray] = []
        self._buffered = 0
        self._index = len(list(self.dir.glob(f"{channel}-*.flac.enc")))
        self.total_samples = 0
        self._lock = threading.Lock()

    def write(self, frames: np.ndarray) -> None:
        if frames.size == 0:
            return
        with self._lock:
            self._buf.append(frames.astype(np.float32, copy=False))
            self._buffered += frames.size
            self.total_samples += frames.size
            if self._buffered >= self.chunk_samples:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffered:
            return
        audio = np.concatenate(self._buf)
        self._buf, self._buffered = [], 0
        bio = io.BytesIO()
        sf.write(bio, audio, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
        self._index += 1
        path = self.dir / f"{self.channel}-{self._index:04d}.flac.enc"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(encrypt(self.key, bio.getvalue(), self.channel.encode()))
        tmp.replace(path)

    def close(self) -> None:
        with self._lock:
            self._flush_locked()


def list_channels(meeting_dir: Path) -> list[str]:
    return sorted({p.name.split("-")[0] for p in Path(meeting_dir).glob("*-*.flac.enc")})


def read_channel(meeting_dir: Path, channel: str, data_key: bytes) -> np.ndarray:
    parts = []
    for p in sorted(Path(meeting_dir).glob(f"{channel}-*.flac.enc")):
        raw = decrypt(data_key, p.read_bytes(), channel.encode())
        data, _ = sf.read(io.BytesIO(raw), dtype="float32")
        parts.append(data)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def write_channel(meeting_dir: Path, channel: str, data_key: bytes, audio: np.ndarray, chunk_seconds: int = 30) -> int:
    """Write a whole in-memory signal as encrypted chunks (used for imported recordings)."""
    w = ChunkWriter(meeting_dir, channel, data_key, chunk_seconds)
    step = SAMPLE_RATE * chunk_seconds
    for i in range(0, audio.size, step):
        w.write(audio[i : i + step])
    w.close()
    return w.total_samples
