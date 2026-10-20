"""Speaker diarization ("who spoke when").

* ``SherpaDiarizer``: the built-in default. pyannote's segmentation model plus a speaker-embedding model,
  both run with ONNX Runtime through sherpa-onnx (part of the ``stt`` extra). The two small models (~35 MB)
  are downloaded once from the sherpa-onnx GitHub releases, with no account or token; audio never leaves the
  machine.
* ``PyannoteDiarizer``: the full pyannote.audio pipeline (optional extra ``diarization``; pulls in PyTorch and
  needs a Hugging Face token once to download the model, then runs offline).
* ``SingleSpeakerDiarizer``: fallback when neither is available: one speaker per channel.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from modwen.audio import SAMPLE_RATE

log = logging.getLogger(__name__)

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SHERPA_SEGMENTATION = (f"{_RELEASES}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
                       "sherpa-onnx-pyannote-segmentation-3-0/model.onnx")
# CAM++ trained on Chinese + English speech (200k speakers); works well on other languages, Arabic included.
_EMBEDDING_FILE = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
SHERPA_EMBEDDING = (f"{_RELEASES}/speaker-recongition-models/{_EMBEDDING_FILE}", _EMBEDDING_FILE)


@dataclass
class Turn:
    start: float
    end: float
    label: str


class SingleSpeakerDiarizer:
    name = "single"

    def diarize(self, audio: np.ndarray, min_speakers: int | None = None, max_speakers: int | None = None,
                num_speakers: int | None = None) -> list[Turn]:
        return [Turn(0.0, audio.size / SAMPLE_RATE, "SPEAKER_00")] if audio.size else []


def sherpa_model_paths(models_dir: Path) -> tuple[Path, Path]:
    root = Path(models_dir) / "sherpa-diarization"
    return root / SHERPA_SEGMENTATION[1], root / SHERPA_EMBEDDING[1]


def sherpa_models_present(models_dir: Path) -> bool:
    return all(p.is_file() for p in sherpa_model_paths(models_dir))


def download_sherpa_models(models_dir: Path, progress=None) -> tuple[Path, Path]:
    """Fetch the two diarization models (once). Only model files are downloaded; no audio is sent."""
    from modwen.runtime.download import download, extract

    seg, emb = sherpa_model_paths(models_dir)
    root = seg.parent.parent
    if not seg.is_file():
        archive = download(SHERPA_SEGMENTATION[0], root / "segmentation.tar.bz2", progress)
        extract(archive, root)
        archive.unlink(missing_ok=True)
    if not emb.is_file():
        download(SHERPA_EMBEDDING[0], emb, progress)
    return seg, emb


# ----- sherpa-onnx runs in a worker process -----------------------------------------------------
# sherpa-onnx keeps Python's GIL for the whole of a call (minutes for a long meeting), which froze every other
# thread: the web UI stopped answering while speakers were detected. One long-lived worker process holds the
# models and does the work; this process only waits for the result, which releases the GIL.
_POOL = None
_POOL_LOCK = threading.Lock()
_W: dict = {}  # in the worker: loaded models


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor

            _POOL = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"), initializer=_w_init)
            atexit.register(_POOL.shutdown, wait=False, cancel_futures=True)
        return _POOL


def _w_init() -> None:
    """Worker start-up: Ctrl+C in the terminal reaches the whole process group; only Modwen should handle it
    (and then shut this worker down), so the worker ignores it instead of printing a traceback."""
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)


def shutdown_worker() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
            _POOL = None


def _call(fn, *args):
    """Run ``fn(*args)`` in the worker; restart it once if it died (e.g. out of memory)."""
    global _POOL
    from concurrent.futures.process import BrokenProcessPool

    for attempt in (1, 2):
        try:
            return _pool().submit(fn, *args).result()
        except BrokenProcessPool:
            with _POOL_LOCK:
                _POOL = None
            if attempt == 2:
                raise
    return None


def _w_diarizer(seg: str, emb: str, threads: int):
    import sherpa_onnx as so

    key = ("diar", seg, emb, threads)
    if key not in _W:
        _W[key] = so.OfflineSpeakerDiarization(_w_config(seg, emb, threads, -1, 1.0))
    return _W[key]


def _w_config(seg: str, emb: str, threads: int, num_clusters: int, threshold: float):
    import sherpa_onnx as so

    return so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg), num_threads=threads),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=threads),
        clustering=so.FastClusteringConfig(num_clusters=num_clusters, threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5,
    )


def _w_diarize(seg: str, emb: str, threads: int, threshold: float, num_clusters: int,
               shm_name: str, n: int) -> list[tuple[float, float, int]]:
    from multiprocessing import shared_memory

    pipe = _w_diarizer(seg, emb, threads)
    pipe.set_config(_w_config(seg, emb, threads, num_clusters, threshold))
    shm = shared_memory.SharedMemory(name=shm_name)
    try:
        audio = np.ndarray((n,), dtype=np.float32, buffer=shm.buf)  # the parent's copy, not a pickled one
        result = pipe.process(audio).sort_by_start_time()
        del audio
    finally:
        shm.close()
    return [(float(r.start), float(r.end), int(r.speaker)) for r in result]


def _shared(audio: np.ndarray):
    """Put ``audio`` in shared memory for the worker (one copy instead of pickling two)."""
    from multiprocessing import shared_memory

    a = np.ascontiguousarray(audio, dtype=np.float32)
    shm = shared_memory.SharedMemory(create=True, size=max(1, a.nbytes))
    np.ndarray(a.shape, dtype=np.float32, buffer=shm.buf)[:] = a
    return shm


def _w_embed(emb: str, threads: int, pieces: list[np.ndarray]) -> list[np.ndarray | None]:
    import sherpa_onnx as so

    key = ("emb", emb, threads)
    if key not in _W:
        _W[key] = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=threads))
    x = _W[key]
    out: list[np.ndarray | None] = []
    for piece in pieces:
        stream = x.create_stream()
        stream.accept_waveform(SAMPLE_RATE, piece)
        stream.input_finished()
        out.append(np.array(x.compute(stream), dtype=np.float32) if x.is_ready(stream) else None)
    return out


def _threads() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


class SherpaDiarizer:
    name = "sherpa"

    def __init__(self, models_dir: Path, threshold: float = 1.0):
        import sherpa_onnx  # noqa: F401  (fail here, not in the worker, when it is not installed)

        seg, emb = download_sherpa_models(models_dir)
        self.threshold = threshold
        self.threads = _threads()
        self._seg, self._emb = str(seg), str(emb)
        self._extractor = None

    def _run(self, audio: np.ndarray, num_clusters: int) -> list[Turn]:
        shm = _shared(audio)
        try:
            rows = _call(_w_diarize, self._seg, self._emb, self.threads, self.threshold, num_clusters,
                         shm.name, int(audio.size))
        finally:
            shm.close()
            shm.unlink()
        return [Turn(a, b, f"SPEAKER_{k:02d}") for a, b, k in rows]

    def diarize(self, audio: np.ndarray, min_speakers: int | None = None, max_speakers: int | None = None,
                num_speakers: int | None = None) -> list[Turn]:
        if audio.size < SAMPLE_RATE:  # under a second: nothing to cluster
            return [Turn(0.0, audio.size / SAMPLE_RATE, "SPEAKER_00")] if audio.size else []
        if num_speakers:
            return self._run(audio, num_speakers)
        turns = self._run(audio, -1)
        found = len({t.label for t in turns})
        if max_speakers and found > max_speakers:
            turns = self._run(audio, max_speakers)
        elif min_speakers and 0 < found < min_speakers:
            turns = self._run(audio, min_speakers)
        return turns

    def voiceprints(self, audio: np.ndarray, turns: list[Turn], max_seconds: float = 60.0) -> dict[str, np.ndarray]:
        """One normalised embedding per label, from its longest turns (used to spot a split speaker)."""
        if self._extractor is None:
            self._extractor = SpeakerEmbedder(emb_path=self._emb, threads=self.threads)
        out: dict[str, np.ndarray] = {}
        for label in {t.label for t in turns}:
            vec, _ = self._extractor.embed(audio, [(t.start, t.end) for t in turns if t.label == label],
                                           max_seconds=max_seconds, min_seconds=0.0)
            if vec is not None:
                out[label] = vec
        return out


class SpeakerEmbedder:
    """Voice embedding (CAM++, the diarizer's model) of chosen stretches of audio: the voiceprint (§5.1)."""

    name = "sherpa-campplus"

    def __init__(self, models_dir: Path | None = None, *, emb_path: str | None = None, threads: int | None = None):
        import sherpa_onnx  # noqa: F401

        if emb_path is None:
            emb_path = str(download_sherpa_models(models_dir)[1])
        self.threads = threads or _threads()
        self._emb = emb_path

    def embed(self, audio: np.ndarray, spans: list[tuple[float, float]], *, max_seconds: float = 60.0,
              min_seconds: float = 10.0, min_piece: float = 1.0) -> tuple[np.ndarray | None, float]:
        """Mean normalised embedding of the longest ``spans`` (seconds) up to ``max_seconds``.

        Returns (None, seconds) when there is less than ``min_seconds`` of usable speech."""
        pieces, used = [], 0.0
        for a, b in sorted(spans, key=lambda s: s[0] - s[1]):
            if b - a < min_piece or used >= max_seconds:
                continue
            piece = np.ascontiguousarray(audio[int(a * SAMPLE_RATE): int(b * SAMPLE_RATE)], dtype=np.float32)
            if piece.size < min_piece * SAMPLE_RATE:
                continue
            pieces.append(piece)
            used += piece.size / SAMPLE_RATE
        vecs = [v / (np.linalg.norm(v) or 1.0) for v in (_call(_w_embed, self._emb, self.threads, pieces)
                                                         if pieces else []) if v is not None]
        if not vecs or used < min_seconds:
            return None, used
        v = np.mean(vecs, axis=0)
        return v / (np.linalg.norm(v) or 1.0), used


class PyannoteDiarizer:
    name = "pyannote"

    def __init__(self, model: str, token: str | None, cache_dir: str | None = None):
        from pyannote.audio import Pipeline

        kwargs = {"cache_dir": cache_dir} if cache_dir else {}
        try:
            self.pipeline = Pipeline.from_pretrained(model, token=token, **kwargs)
        except TypeError:  # pyannote 3.x uses use_auth_token
            self.pipeline = Pipeline.from_pretrained(model, use_auth_token=token, **kwargs)
        try:
            import torch

            if torch.cuda.is_available():
                self.pipeline.to(torch.device("cuda"))
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                self.pipeline.to(torch.device("mps"))
        except Exception:
            pass

    def diarize(self, audio: np.ndarray, min_speakers: int | None = None, max_speakers: int | None = None,
                num_speakers: int | None = None) -> list[Turn]:
        import torch

        waveform = torch.from_numpy(audio.astype(np.float32)).unsqueeze(0)
        kwargs = {}
        if num_speakers:
            kwargs["num_speakers"] = num_speakers
        else:
            if min_speakers:
                kwargs["min_speakers"] = min_speakers
            if max_speakers:
                kwargs["max_speakers"] = max_speakers
        result = self.pipeline({"waveform": waveform, "sample_rate": SAMPLE_RATE}, **kwargs)
        annotation = getattr(result, "speaker_diarization", result)  # pyannote 4 returns a richer object
        return [Turn(float(seg.start), float(seg.end), str(label))
                for seg, _, label in annotation.itertracks(yield_label=True)]


def _pyannote(cfg):
    import pyannote.audio  # noqa: F401

    from modwen.config import resolve_secret

    token = resolve_secret(cfg.diarization.hf_token, cfg)
    return PyannoteDiarizer(cfg.diarization.model, token, cache_dir=str(cfg.models_dir / "pyannote"))


def _sherpa(cfg):
    return SherpaDiarizer(cfg.models_dir, threshold=cfg.diarization.threshold)


def make_diarizer(cfg):
    """``auto`` prefers pyannote when it is installed (and has its model), then the built-in sherpa-onnx one."""
    provider = cfg.diarization.provider
    if provider == "none":
        return SingleSpeakerDiarizer()
    order = {"auto": [_pyannote, _sherpa], "pyannote": [_pyannote, _sherpa], "sherpa": [_sherpa]}[provider]
    for build in order:
        try:
            return build(cfg)
        except ImportError:
            continue
        except Exception as exc:
            log.warning("Could not load the %s diarizer (%s)", build.__name__.strip("_"), exc)
    log.warning("No speaker diarization available (uv sync --extra stt); one speaker per channel")
    return SingleSpeakerDiarizer()
