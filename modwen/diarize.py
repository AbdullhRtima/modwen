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

import logging
import os
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


class SherpaDiarizer:
    name = "sherpa"

    def __init__(self, models_dir: Path, threshold: float = 1.0):
        import sherpa_onnx

        seg, emb = download_sherpa_models(models_dir)
        self._so = sherpa_onnx
        self.threshold = threshold
        self.threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        self._seg, self._emb = str(seg), str(emb)
        self.pipeline = sherpa_onnx.OfflineSpeakerDiarization(self._config(-1))
        self._extractor = None

    def _config(self, num_clusters: int):
        so = self._so
        return so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=self._seg),
                num_threads=self.threads),
            embedding=so.SpeakerEmbeddingExtractorConfig(model=self._emb, num_threads=self.threads),
            clustering=so.FastClusteringConfig(num_clusters=num_clusters, threshold=self.threshold),
            min_duration_on=0.3, min_duration_off=0.5,
        )

    def _run(self, audio: np.ndarray, num_clusters: int) -> list[Turn]:
        self.pipeline.set_config(self._config(num_clusters))
        result = self.pipeline.process(np.ascontiguousarray(audio, dtype=np.float32)).sort_by_start_time()
        return [Turn(float(r.start), float(r.end), f"SPEAKER_{int(r.speaker):02d}") for r in result]

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
            so = self._so
            self._extractor = so.SpeakerEmbeddingExtractor(
                so.SpeakerEmbeddingExtractorConfig(model=self._emb, num_threads=self.threads))
        out: dict[str, np.ndarray] = {}
        for label in {t.label for t in turns}:
            mine = sorted((t for t in turns if t.label == label), key=lambda t: t.start - t.end)
            vecs, used = [], 0.0
            for t in mine:
                if t.end - t.start < 1.0 or used >= max_seconds:
                    continue
                piece = audio[int(t.start * SAMPLE_RATE): int(t.end * SAMPLE_RATE)].astype(np.float32)
                stream = self._extractor.create_stream()
                stream.accept_waveform(SAMPLE_RATE, piece)
                stream.input_finished()
                if self._extractor.is_ready(stream):
                    vecs.append(np.array(self._extractor.compute(stream), dtype=np.float32))
                    used += t.end - t.start
            if vecs:
                v = np.mean(vecs, axis=0)
                out[label] = v / (np.linalg.norm(v) or 1.0)
        return out


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
