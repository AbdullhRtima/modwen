"""Speaker diarization ("who spoke when").

* ``PyannoteDiarizer``: pyannote.audio pipeline (optional extra ``diarization``; needs a Hugging Face
  token once to download the model, then runs offline).
* ``SingleSpeakerDiarizer``: fallback when pyannote is not installed. The mic channel is still "You" and
  the system channel becomes one "Others" speaker, so online meetings remain usable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from modwen.audio import SAMPLE_RATE

log = logging.getLogger(__name__)


@dataclass
class Turn:
    start: float
    end: float
    label: str


class SingleSpeakerDiarizer:
    name = "single"

    def diarize(self, audio: np.ndarray, min_speakers: int | None = None, max_speakers: int | None = None) -> list[Turn]:
        return [Turn(0.0, audio.size / SAMPLE_RATE, "SPEAKER_00")] if audio.size else []


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

    def diarize(self, audio: np.ndarray, min_speakers: int | None = None, max_speakers: int | None = None) -> list[Turn]:
        import torch

        waveform = torch.from_numpy(audio.astype(np.float32)).unsqueeze(0)
        kwargs = {}
        if min_speakers:
            kwargs["min_speakers"] = min_speakers
        if max_speakers:
            kwargs["max_speakers"] = max_speakers
        result = self.pipeline({"waveform": waveform, "sample_rate": SAMPLE_RATE}, **kwargs)
        annotation = getattr(result, "speaker_diarization", result)  # pyannote 4 returns a richer object
        return [Turn(float(seg.start), float(seg.end), str(label))
                for seg, _, label in annotation.itertracks(yield_label=True)]


def make_diarizer(cfg):
    from modwen.config import resolve_secret

    provider = cfg.diarization.provider
    if provider == "none":
        return SingleSpeakerDiarizer()
    try:
        import pyannote.audio  # noqa: F401
    except Exception:
        if provider == "pyannote":
            log.warning("pyannote.audio is not installed (uv sync --extra diarization); using single speaker")
        return SingleSpeakerDiarizer()
    token = resolve_secret(cfg.diarization.hf_token, cfg)
    try:
        return PyannoteDiarizer(cfg.diarization.model, token, cache_dir=str(cfg.models_dir / "pyannote"))
    except Exception as exc:
        log.warning("Could not load pyannote (%s); using single speaker", exc)
        return SingleSpeakerDiarizer()
