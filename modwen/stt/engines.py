"""Whisper engines behind one small interface.

* ``FasterWhisperEngine``: CTranslate2; NVIDIA GPU (CUDA) or CPU. Works on Windows, macOS and Linux.
* ``MlxWhisperEngine``: Apple Silicon (Metal). Requires ``uv sync --extra mac``.
* ``FakeEngine``: deterministic engine for tests and demos (no model download).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from modwen.audio import SAMPLE_RATE

log = logging.getLogger(__name__)

MLX_REPOS = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


@dataclass
class STTSegment:
    start: float  # seconds, relative to the audio passed in
    end: float
    text: str
    language: str
    confidence: float = 0.0
    words: list[dict] = field(default_factory=list)


class STTEngine(Protocol):
    name: str

    def detect_language(self, audio: np.ndarray) -> dict[str, float]: ...

    def transcribe(self, audio: np.ndarray, language: str | None, initial_prompt: str | None) -> list[STTSegment]: ...


class FasterWhisperEngine:
    def __init__(self, model: str, device: str = "auto", compute_type: str = "auto",
                 download_root: str | Path | None = None, beam_size: int = 5):
        from faster_whisper import WhisperModel

        self.name = f"faster-whisper:{model}"
        self.beam_size = beam_size
        if device == "auto":
            from modwen.hardware import cuda_available

            device = "cuda" if cuda_available() else "cpu"
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        log.info("Loading %s on %s (%s)", model, device, compute_type)
        try:
            self.model = WhisperModel(model, device=device, compute_type=compute_type, local_files_only=True,
                                      download_root=str(download_root) if download_root else None)
        except Exception as exc:
            if "local" in str(exc).lower() or "cache" in str(exc).lower() or "find" in str(exc).lower():
                raise ModelNotDownloaded(f"Whisper model '{model}' is not downloaded. Open Modwen's setup "
                                         "(Settings → Models) or run `modwen models download`.") from exc
            raise
        self._lock = threading.Lock()

    def detect_language(self, audio: np.ndarray) -> dict[str, float]:
        with self._lock:
            _, _, all_probs = self.model.detect_language(audio)
        return {lang: float(p) for lang, p in all_probs}

    def transcribe(self, audio: np.ndarray, language: str | None, initial_prompt: str | None) -> list[STTSegment]:
        with self._lock:
            segments, info = self.model.transcribe(
                audio,
                language=language,
                task="transcribe",
                beam_size=self.beam_size,
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,
                word_timestamps=False,
                vad_filter=False,
            )
            out = []
            for s in segments:
                words = [{"w": w.word, "start": w.start, "end": w.end, "p": w.probability} for w in (s.words or [])]
                conf = float(np.exp(s.avg_logprob)) if s.avg_logprob is not None else 0.0
                if s.no_speech_prob and s.no_speech_prob > 0.8 and conf < 0.4:
                    continue
                out.append(STTSegment(s.start, s.end, s.text.strip(), info.language, conf, words))
        return out


class ModelNotDownloaded(RuntimeError):
    pass


def mlx_model_path(model: str, cache_dir: str | Path | None, local_only: bool = True) -> str:
    """Local folder of an MLX Whisper model inside Modwen's models dir.

    ``local_only`` (the default everywhere except explicit downloads): never touch the network.
    """
    from huggingface_hub import snapshot_download

    repo = MLX_REPOS.get(model, model)
    if Path(repo).is_dir():
        return repo
    try:
        return snapshot_download(repo, cache_dir=str(cache_dir) if cache_dir else None, local_files_only=local_only)
    except Exception as exc:
        if local_only:
            raise ModelNotDownloaded(f"Whisper model '{model}' is not downloaded. Open Modwen's setup "
                                     "(Settings → Models) or run `modwen models download`.") from exc
        raise


class MlxWhisperEngine:
    def __init__(self, model: str, beam_size: int = 5, cache_dir: str | Path | None = None):
        import mlx_whisper  # noqa: F401

        self.repo = mlx_model_path(model, cache_dir)
        self.name = f"mlx-whisper:{model}"
        self._lock = threading.Lock()
        self._model = None

    def _load(self):
        # Share mlx_whisper's own cached instance (float16) instead of loading a second copy: on a 16 GB Mac
        # two copies of large-v3 plus the LLM cause heavy swapping.
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        return ModelHolder.get_model(self.repo, mx.float16)

    def detect_language(self, audio: np.ndarray) -> dict[str, float]:
        try:
            import mlx.core as mx
            from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim

            with self._lock:
                model = self._load()
                # Same steps mlx_whisper.transcribe uses: mel of the first 30 s window, then detect.
                mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
                segment = pad_or_trim(mel[:N_FRAMES], N_FRAMES, axis=-2).astype(mx.float16)
                _, probs = model.detect_language(segment)
            if isinstance(probs, list):
                probs = probs[0]
            return {k: float(v) for k, v in probs.items()}
        except Exception as exc:
            log.debug("mlx detect_language failed (%s); falling back to transcribe-based detection", exc)
            import mlx_whisper

            with self._lock:
                res = mlx_whisper.transcribe(audio, path_or_hf_repo=self.repo, language=None, verbose=None)
            return {res.get("language", "ar"): 1.0}

    def transcribe(self, audio: np.ndarray, language: str | None, initial_prompt: str | None) -> list[STTSegment]:
        import mlx_whisper

        with self._lock:
            res = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.repo,
                language=language,
                task="transcribe",
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,
                word_timestamps=False,
                verbose=None,
            )
        lang = res.get("language", language or "ar")
        out = []
        for s in res.get("segments", []):
            words = [{"w": w["word"], "start": w["start"], "end": w["end"], "p": w.get("probability", 0)}
                     for w in s.get("words", [])]
            conf = float(np.exp(s.get("avg_logprob", -1.0)))
            if s.get("no_speech_prob", 0) > 0.8 and conf < 0.4:
                continue
            out.append(STTSegment(float(s["start"]), float(s["end"]), s["text"].strip(), lang, conf, words))
        return out


class FakeEngine:
    """Returns scripted text per utterance: ``script`` is a list of (language, text) or a callable."""

    def __init__(self, script: list[tuple[str, str]] | Callable[[int, np.ndarray], tuple[str, str]] | None = None):
        self.name = "fake"
        self.script = script or [("en", "This is a test utterance.")]
        self.calls = 0
        self._next: tuple[str, str] | None = None

    def _item(self, audio: np.ndarray) -> tuple[str, str]:
        if callable(self.script):
            return self.script(self.calls, audio)
        return self.script[self.calls % len(self.script)]

    def detect_language(self, audio: np.ndarray) -> dict[str, float]:
        self._next = self._item(audio)
        lang = self._next[0]
        return {"ar": 0.7, "en": 0.3} if lang in ("ar", "mixed") else {"en": 0.9, "ar": 0.05, "fr": 0.05}

    def transcribe(self, audio: np.ndarray, language: str | None, initial_prompt: str | None) -> list[STTSegment]:
        lang, text = self._next or self._item(audio)
        self._next = None
        self.calls += 1
        dur = audio.size / SAMPLE_RATE
        return [STTSegment(0.0, dur, text, language or lang, 0.9)]


def _add_windows_cuda_dlls() -> None:
    """Windows + NVIDIA: make pip-installed cuBLAS/cuDNN (nvidia-cublas-cu12, nvidia-cudnn-cu12) loadable."""
    import os
    import sys

    if sys.platform != "win32":
        return
    import site

    for base in site.getsitepackages():
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            d = Path(base) / "nvidia" / sub / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def resolve_models(cfg, plan) -> tuple[str, str, str]:
    """Return (backend, live_model, final_model) honouring config overrides and installed backends."""
    from modwen.hardware import mlx_available

    backend = cfg.stt.final.backend
    if backend == "auto":
        backend = plan.whisper_backend
    if backend == "mlx" and not mlx_available():
        log.warning("MLX Whisper not installed; using faster-whisper on CPU (install with: uv sync --extra mac)")
        backend = "faster_whisper"
    live = cfg.stt.live.model if cfg.stt.live.model != "auto" else plan.live_model
    final = cfg.stt.final.model if cfg.stt.final.model != "auto" else plan.final_model
    if backend == "faster_whisper" and plan.device in ("metal", "cpu") and cfg.stt.final.model == "auto":
        # No GPU acceleration for CTranslate2 here: keep it usable.
        final = "large-v3-turbo"
        live = "small" if cfg.stt.live.model == "auto" else live
    return backend, live, final


def build_engine(cfg, model: str, backend: str, pass_cfg) -> STTEngine:
    if pass_cfg.provider == "fake":
        return FakeEngine()
    if pass_cfg.provider == "none":
        raise RuntimeError("STT provider is 'none'")
    if backend == "mlx":
        return MlxWhisperEngine(model, beam_size=pass_cfg.beam_size, cache_dir=cfg.models_dir / "mlx")
    _add_windows_cuda_dlls()
    return FasterWhisperEngine(model, device=pass_cfg.device, compute_type=pass_cfg.compute_type,
                               download_root=cfg.models_dir / "whisper", beam_size=pass_cfg.beam_size)
