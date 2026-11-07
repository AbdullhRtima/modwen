"""Live speaker names: a provisional guess of who is talking, shown on the live captions only.

Each system-audio (online) or Room (in person) utterance is compared with the saved, consented voiceprints.
The result is a name, a name with a question mark, or nothing. Privacy rules (security review, report 04):

* Nothing is embedded when no usable voiceprint is saved (the user's own print does not count online) or when
  the setting is off; turning it off mid-meeting wipes the loaded prints at once.
* The utterance's voice vector lives inside one function and is dropped after scoring. It is never stored,
  logged, put in an event or kept for clustering, and no score or person id leaves this module.
* Prints are loaded in memory for the meeting, zeroed on wipe, and re-read if a voiceprint or person changed.
* Failures log the exception class only. Late results (after the deadline or after ``close()``) are dropped.
* Display only: this module never writes to the Store, and names never reach segments, prompts or notes.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np

from modwen.audio import SAMPLE_RATE
from modwen.voiceprints import LOCAL, REMOTE, LiveThresholds, classify_live

log = logging.getLogger(__name__)

DEADLINE_S = 5.0  # a result later than this after the utterance is dropped (it would be a stale guess)
MAX_FAILURES = 3  # in a row; then live names stay off for the rest of the meeting
MIN_SPEECH_S = 1.2  # less speech than this is not worth an embedding
MAX_SPEECH_S = 8.0
QUEUE_SIZE = 2
TAIL_PAD_S = 0.15
_SILENCE_DB = -42.0  # same level the endpointer uses

State = Literal["named", "maybe", "other"]


@dataclass(frozen=True)
class LiveName:
    name: str | None
    state: State


NONE = LiveName(None, "other")


def trim_tail(utt: np.ndarray) -> np.ndarray:
    """Cut the trailing silence the endpointer keeps (about 1 s) so it does not dilute the voice."""
    win = SAMPLE_RATE // 20
    n = utt.size // win
    if n == 0:
        return utt
    frames = utt[: n * win].reshape(n, win)
    db = 20 * np.log10(np.sqrt(np.mean(frames**2, axis=1) + 1e-12) + 1e-12)
    loud = np.nonzero(db > _SILENCE_DB)[0]
    if loud.size == 0:
        return utt[:0]
    end = min(utt.size, (int(loud[-1]) + 1) * win + int(TAIL_PAD_S * SAMPLE_RATE))
    return utt[:end]


class LiveIdentifier:
    """Names one utterance at a time on its own ``live-id`` thread, never on the recorder or Whisper thread."""

    def __init__(self, bank, embedder, *, online: bool, enabled: Callable[[], bool],
                 thresholds: LiveThresholds = LiveThresholds(), defer: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.monotonic):
        self._bank, self._embedder = bank, embedder
        self._online = online
        self._enabled, self._defer, self._clock = enabled, defer, clock
        self._th = thresholds
        self.embed_calls = 0  # how many voices were embedded (tests: zero when nobody usable is saved)
        self._lock = threading.Lock()
        self._prints: dict[str, dict[str, np.ndarray]] = {}
        self._names: dict[str, str] = {}
        self._sig: object = None
        self._state = "loading"  # loading | ready | off | disabled | closed
        self._failures = 0
        self._busy = False
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="live-id", daemon=True)
        self._thread.start()

    # ----- called from the live captions thread (must be cheap) ---------------------------------
    def submit(self, channel: str, utt: np.ndarray, deadline_s: float = DEADLINE_S) -> Future | None:
        if self._state in ("off", "disabled", "closed"):
            return None
        if channel != ("system" if self._online else "room"):  # online "You" is decided by channel, not voice
            return None
        if not self._safe(self._enabled, False):
            self._wipe("off")  # switched off in Settings: off at once and for the rest of the meeting
            return None
        if self._busy or self._safe(self._defer, True):
            return None
        speech = trim_tail(utt)
        if speech.size < MIN_SPEECH_S * SAMPLE_RATE:
            return None
        fut: Future = Future()
        if self._q.qsize() >= QUEUE_SIZE:
            self._drop_oldest()
        self._q.put((fut, np.array(speech[: int(MAX_SPEECH_S * SAMPLE_RATE)]), self._clock() + deadline_s))
        return fut

    def status(self) -> Literal["on", "paused", "off"]:
        if self._state in ("off", "loading", "closed") or not self._safe(self._enabled, False):
            return "off"
        if self._state == "disabled" or self._safe(self._defer, True):
            return "paused"
        return "on"

    def close(self) -> None:
        """End of meeting: wipe the prints and drop every pending result."""
        self._wipe("closed")
        while True:
            try:
                fut = self._q.get_nowait()[0]
            except queue.Empty:
                break
            self._resolve(fut, NONE)

    # ----- the live-id thread ---------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._reload()
        except Exception as exc:
            self._failed(type(exc).__name__)
            self._wipe("off")  # could not read the prints: no names, and nothing is embedded
        while self._state != "closed":
            try:
                fut, utt, deadline = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._busy = True
            result = NONE
            try:
                result = self._identify(utt, deadline)
            except Exception as exc:  # includes TimeoutError and CancelledError from the worker pool
                self._failed(type(exc).__name__)
            finally:
                self._busy = False
                del utt
            self._resolve(fut, result, deadline)

    def _identify(self, utt: np.ndarray, deadline: float) -> LiveName:
        if self._state == "closed" or self._clock() >= deadline:
            return NONE
        if self._state == "ready" and self._signature() != self._sig:
            self._reload()  # a voiceprint or person was added or deleted during the meeting
        if self._state != "ready" or not self._safe(self._enabled, False):
            return NONE
        self.embed_calls += 1
        vec, _ = self._embedder.embed(utt, [(0.0, utt.size / SAMPLE_RATE)], min_seconds=MIN_SPEECH_S,
                                      min_piece=1.0, max_seconds=MAX_SPEECH_S,
                                      timeout=max(0.2, deadline - self._clock()))
        self._failures = 0
        if vec is None:
            return NONE
        with self._lock:
            pid, band = classify_live(vec, self._prints, REMOTE if self._online else LOCAL, self._th)
            name = self._names.get(pid) if pid else None
        del vec  # the voice vector ends here
        return LiveName(name, band) if name else NONE

    def _resolve(self, fut: Future, result: LiveName, deadline: float | None = None) -> None:
        if self._state == "closed" or (deadline is not None and self._clock() > deadline):
            result = NONE
        try:
            fut.set_result(result)
        except InvalidStateError:
            pass

    def _drop_oldest(self) -> None:
        try:
            self._resolve(self._q.get_nowait()[0], NONE)
        except queue.Empty:
            pass

    # ----- prints in memory -------------------------------------------------------------------------
    def _signature(self) -> object:
        store = self._bank.store
        summary = store.voice_summary()
        return (tuple(sorted((p, v["samples"], round(v["seconds"], 3)) for p, v in summary.items())),
                frozenset(p["id"] for p in store.list_persons()))

    def _reload(self) -> None:
        sig = self._signature()
        persons = self._bank.store.list_persons()
        names = {p["id"]: p["display_name"] for p in persons}
        selves = {p["id"] for p in persons if p.get("is_self")}
        loaded = self._bank.centroids()
        # Online, "You" is the mic channel, so the user's own print is never used (as in recognise()).
        prints = {pid: k for pid, k in loaded.items() if pid in names and not (self._online and pid in selves)}
        for pid in set(loaded) - set(prints):
            self._zero({pid: loaded[pid]})
        with self._lock:
            if self._state not in ("loading", "ready"):  # closed, switched off or disabled while loading
                self._zero(prints)
                return
            old, self._prints, self._names, self._sig = self._prints, prints, names, sig
            self._zero(old)
            self._state = "ready" if prints else "off"  # nobody usable: no embedding at all

    def _wipe(self, state: str) -> None:
        with self._lock:
            old, self._prints, self._names = self._prints, {}, {}
            self._zero(old)
            if self._state != "closed":
                self._state = state

    @staticmethod
    def _zero(prints: dict[str, dict[str, np.ndarray]]) -> None:
        for kinds in prints.values():
            for v in kinds.values():
                if v.flags.writeable:
                    v[:] = 0  # best effort: Python cannot promise no other copy exists

    def _failed(self, kind: str) -> None:
        log.warning("Live names: a voice comparison failed (%s)", kind)  # the class only, never the details
        self._failures += 1
        if self._failures >= MAX_FAILURES:
            log.warning("Live names are off for the rest of this meeting after %d failures", MAX_FAILURES)
            self._wipe("disabled")

    @staticmethod
    def _safe(fn: Callable[[], bool], default: bool):
        try:
            return bool(fn())
        except Exception:
            return default
