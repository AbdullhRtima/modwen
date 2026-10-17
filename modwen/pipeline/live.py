"""Simple live captions (design: "keep it simple").

Per channel: buffer audio, end an utterance on ~0.7 s of silence or 12 s max, transcribe it with the fast
model in a worker thread, publish "You"/"Others" lines. Live text is display-only: never stored as the
final transcript, never sent to the LLM. If the machine falls behind, utterances are skipped and a
"live paused" flag is raised; recording itself is never affected.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from modwen.audio import SAMPLE_RATE
from modwen.stt.hallucination import clean_text, is_hallucination
from modwen.stt.language import choose_language, script_language

log = logging.getLogger(__name__)

LABELS = {"mic": "You", "system": "Others", "room": "Room"}


class _Endpointer:
    def __init__(self, silence_s: float = 1.0, max_s: float = 12.0, threshold_db: float = -42.0):
        self.silence = int(silence_s * SAMPLE_RATE)
        self.max = int(max_s * SAMPLE_RATE)
        self.th = threshold_db
        self.buf: list[np.ndarray] = []
        self.size = 0
        self.quiet = 0
        self.voiced = False
        self.start_sample = 0
        self.total = 0

    def feed(self, frames: np.ndarray) -> list[tuple[int, np.ndarray]]:
        out = []
        rms = float(np.sqrt(np.mean(frames**2) + 1e-12)) if frames.size else 0.0
        loud = 20 * np.log10(rms + 1e-12) > self.th
        if not self.buf:
            self.start_sample = self.total
        self.total += frames.size
        if loud:
            self.voiced = True
            self.quiet = 0
        else:
            self.quiet += frames.size
        if self.voiced or self.buf:
            self.buf.append(frames)
            self.size += frames.size
        if not self.voiced and self.size > SAMPLE_RATE:  # drop leading silence
            self.buf, self.size = [], 0
        if self.voiced and (self.quiet >= self.silence or self.size >= self.max):
            out.append((self.start_sample, np.concatenate(self.buf)))
            self.buf, self.size, self.quiet, self.voiced = [], 0, 0, False
        return out


class LiveTranscriber:
    def __init__(self, engine_factory: Callable[[], object] | None, publish: Callable[[dict], None],
                 meeting_id: str, *, mode: str = "auto", allowed=("ar", "en"), arabic_threshold: float = 0.2,
                 prompt: str | None = None, max_backlog: int = 4):
        self.engine_factory = engine_factory
        self.engine = None
        self.publish = publish
        self.meeting_id = meeting_id
        self.mode = mode
        self.allowed = list(allowed)
        self.th = arabic_threshold
        self.prompt = prompt
        self.max_backlog = max_backlog
        self.lines: deque[dict] = deque(maxlen=5000)
        self.paused = False
        self.enabled = engine_factory is not None
        self.error: str | None = None
        self._q: queue.Queue = queue.Queue()
        self._ep: dict[str, _Endpointer] = {}
        self._running = True
        self._worker = threading.Thread(target=self._run, name="live-stt", daemon=True)
        if self.enabled:
            self._worker.start()

    def feed(self, channel: str, frames: np.ndarray) -> None:
        if not self.enabled:
            return
        ep = self._ep.setdefault(channel, _Endpointer())
        for start, utt in ep.feed(frames):
            if self._q.qsize() >= self.max_backlog:
                try:
                    self._q.get_nowait()  # drop the oldest: live view skips ahead
                except queue.Empty:
                    pass
                if not self.paused:
                    self.paused = True
                    self.publish({"type": "live_status", "meeting_id": self.meeting_id, "paused": True})
            self._q.put((channel, start, utt))

    def _run(self) -> None:
        try:
            self.engine = self.engine_factory() if self.engine_factory else None
        except Exception as exc:
            self.error = f"Live transcript unavailable: {exc}"
            self.enabled = False
            log.warning(self.error)
            self.publish({"type": "live_status", "meeting_id": self.meeting_id, "error": self.error})
            return
        while self._running:
            try:
                channel, start, utt = self._q.get(timeout=0.5)
            except queue.Empty:
                if self.paused and self._q.empty():
                    self.paused = False
                    self.publish({"type": "live_status", "meeting_id": self.meeting_id, "paused": False})
                continue
            t0 = time.monotonic()
            try:
                probs = self.engine.detect_language(utt) if self.mode == "auto" else None
                lang = choose_language(probs, self.mode, self.allowed, self.th)
                segs = self.engine.transcribe(utt, lang, self.prompt if lang == "ar" else None)
            except Exception as exc:
                log.warning("live transcription error: %s", exc)
                continue
            text = clean_text(" ".join(s.text.strip() for s in segs))[0]
            if not text or is_hallucination(text):
                continue
            line = {"channel": channel, "speaker": LABELS.get(channel, channel), "start_ms": int(start / 16),
                    "text": text, "language": script_language(text),
                    "latency_ms": int((time.monotonic() - t0) * 1000)}
            self.lines.append(line)
            self.publish({"type": "live_line", "meeting_id": self.meeting_id, "line": line})

    def stop(self) -> None:
        self._running = False
