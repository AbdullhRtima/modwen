"""Recording sessions: start/pause/mark/stop, encrypted chunks, level meters, live captions, watchdog."""

from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
from typing import Callable

import numpy as np
import soxr

from modwen.audio import SAMPLE_RATE
from modwen.audio.capture import CaptureError, CaptureSource, MicSource, is_bluetooth_name, system_source
from modwen.audio.chunks import ChunkWriter
from modwen.audio.vad import rms_level
from modwen.pipeline.live import LiveTranscriber
from modwen.storage import now_iso

log = logging.getLogger(__name__)


class RecorderBusy(RuntimeError):
    pass


# How far a channel may lag the clock (normal delivery jitter) before the missing audio is filled with silence.
CLOCK_SLACK = int(0.3 * SAMPLE_RATE)
# Audio waiting for the writer thread, in seconds, before the recording shows a "computer is busy" warning
# and live captions are skipped to catch up (the recording itself is never dropped).
BACKLOG_WARN_SECONDS = 5.0
# With device capture times, drift beyond this is corrected (resampler latency stays well below it).
CAPTURE_SLACK = int(0.08 * SAMPLE_RATE)
# A source that sends no data at all for this long has lost its device (silence still sends data).
DEAD_SOURCE_SECONDS = 5.0


class Session:
    def __init__(self, meeting_id: str, sources: list[CaptureSource], writers: dict[str, ChunkWriter],
                 live: LiveTranscriber | None):
        self.meeting_id = meeting_id
        self.sources = sources
        self.writers = writers
        self.live = live
        self.started = time.monotonic()
        self.paused = False
        self.paused_total = 0.0
        self._pause_t = 0.0
        self.levels = {ch: 0.0 for ch in writers}
        self.last_sound = {ch: time.monotonic() for ch in writers}
        self.warnings: dict[str, str] = {}
        self.bookmarks: list[int] = []
        self.written = {ch: 0 for ch in writers}
        self.gap_seconds = {ch: 0.0 for ch in writers}
        self.trimmed_seconds: dict[str, float] = {}
        self.realtime = {c.channel: getattr(c, "realtime", True) for c in sources}
        # Audio callbacks only enqueue (channel, frames, rate, arrival time); the writer thread does the rest.
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.queued_seconds = 0.0
        self.resamplers: dict[str, soxr.ResampleStream] = {}
        self.writer: threading.Thread | None = None
        self.last_frame = {c.channel: time.monotonic() for c in sources}  # any data at all, even silence
        self.restarted_at: dict[str, float] = {}

    def elapsed(self) -> float:
        now = self._pause_t if self.paused else time.monotonic()
        return now - self.started - self.paused_total

    def elapsed_at(self, t: float) -> float:
        """Recording time at monotonic time ``t`` (when a block of audio arrived)."""
        return t - self.started - self.paused_total


class Recorder:
    def __init__(self, cfg, store, keystore, publish: Callable[[dict], None], *,
                 on_stopped: Callable[[str], None], live_engine_factory=None, source_factory=None):
        self.cfg = cfg
        self.store = store
        self.keystore = keystore
        self.publish = publish
        self.on_stopped = on_stopped
        self.live_engine_factory = live_engine_factory
        self.source_factory = source_factory or self._default_sources
        self.session: Session | None = None
        self._lock = threading.Lock()
        self._watchdog: threading.Thread | None = None

    # ------------------------------------------------------------------
    def _default_sources(self, mode: str, sources: list[str]) -> list[CaptureSource]:
        out: list[CaptureSource] = []
        if "mic" in sources:
            out.append(MicSource(self.cfg.capture.mic_device))
        if "system" in sources and mode == "online":
            src = system_source(self.cfg)
            if src:
                out.append(src)
        return out

    def start(self, title: str, *, mode: str = "online", meeting_language: str | None = None,
              sources: list[str] | None = None, platform_name: str = "other", consent: dict | None = None,
              hardware_tier: str | None = None, extra_sources: list[CaptureSource] | None = None) -> dict:
        with self._lock:
            if self.session:
                raise RecorderBusy("A meeting is already being recorded")
            free_gb = shutil.disk_usage(self.cfg.data_dir).free / 1024**3
            if free_gb < self.cfg.capture.min_free_gb:
                raise CaptureError(f"Only {free_gb:.1f} GB free disk space; need {self.cfg.capture.min_free_gb} GB")
            sources = sources or list(self.cfg.capture.sources)
            # Sources first: when none is available there is no meeting to create (no "failed" leftovers).
            caps = extra_sources if extra_sources is not None else self.source_factory(mode, sources)
            if not caps:
                raise CaptureError("No audio sources available. Check the microphone and system-audio settings.")
            data_key, wrapped = self.keystore.new_data_key()
            meeting = self.store.create_meeting(
                title or "Untitled meeting", wrapped_key=wrapped, mode=mode, status="recording",
                platform=platform_name, meeting_language=meeting_language or self.cfg.stt.meeting_language,
                summary_language=self.cfg.notes.summary_language, consent=consent or {},
                llm_profile=self.cfg.llm.active, hardware_tier=hardware_tier, timezone=self.cfg.notes.timezone,
            )
            mid = meeting["id"]
            # In-person meetings record the mic as the "room" channel (diarized, no automatic "You").
            for c in caps:
                if mode == "in_person" and c.channel == "mic":
                    c.channel = "room"
            mdir = self.cfg.audio_dir / mid
            writers = {c.channel: ChunkWriter(mdir, c.channel, data_key, self.cfg.capture.chunk_seconds) for c in caps}
            live = None
            if self.live_engine_factory is not None and self.cfg.stt.live.provider != "none":
                live = LiveTranscriber(self.live_engine_factory, self.publish, mid,
                                       mode=meeting_language or self.cfg.stt.meeting_language,
                                       allowed=self.cfg.stt.allowed_languages,
                                       arabic_threshold=self.cfg.stt.arabic_threshold, prompt=self.cfg.stt_prompt())
            session = Session(mid, caps, writers, live)
            session.writer = threading.Thread(target=self._write_loop, args=(session,), name="rec-writer",
                                              daemon=True)
            session.writer.start()
            self.session = session  # before the sources start, so their first frames are kept
            started: list[CaptureSource] = []
            errors = []
            for c in caps:
                try:
                    c.start(self._on_frames)
                    started.append(c)
                except Exception as exc:
                    errors.append(f"{c.channel}: {exc}")
                    log.error("Could not start %s source: %s", c.channel, exc)
                    writers.pop(c.channel, None)
            if not started:
                self.session = None
                session.queue.put(None)
                session.writer.join(timeout=5)
                self.store.update_meeting(mid, status="failed", error="; ".join(errors))
                raise CaptureError("Could not start any audio source: " + "; ".join(errors))
            session.sources = started
            session.levels = {ch: 0.0 for ch in writers}
            session.last_sound = {ch: time.monotonic() for ch in writers}
            if errors:
                session.warnings["sources"] = "; ".join(errors)
            for c in started:
                desc = getattr(c, "description", "")
                if c.channel == "mic" and is_bluetooth_name(desc):
                    session.warnings["bluetooth"] = "Bluetooth mic lowers accuracy. Use built-in or wired."
            self.store.update_meeting(mid, channels=list(writers))
            self._watchdog = threading.Thread(target=self._watch, name="rec-watchdog", daemon=True)
            self._watchdog.start()
        self.publish({"type": "recording_started", "meeting_id": mid, "title": meeting["title"]})
        return self.store.get_meeting(mid)  # type: ignore[return-value]

    def _on_frames(self, channel: str, frames: np.ndarray, rate: int = SAMPLE_RATE,
                   captured_at: float | None = None) -> None:
        """Called on audio threads: hand the frames to the writer thread and return at once."""
        s = self.session
        if s is None or s.paused or channel not in s.writers:
            return
        now = time.monotonic()
        s.last_frame[channel] = now
        s.queue.put((channel, frames, rate, now, captured_at))
        s.queued_seconds += len(frames) / rate

    def _write_loop(self, s: Session) -> None:
        while True:
            item = s.queue.get()
            if item is None:
                return
            channel, frames, rate, t, captured_at = item
            s.queued_seconds = max(0.0, s.queued_seconds - len(frames) / rate)
            try:
                self._handle(s, channel, frames, rate, t, captured_at)
            except Exception:  # a bad block must never stop the recording
                log.exception("Could not write a block of %s audio", channel)

    def _handle(self, s: Session, channel: str, frames: np.ndarray, rate: int, t: float,
                captured_at: float | None = None) -> None:
        """Downmix, resample (one continuous stream per channel: no clicks between blocks), keep the channel on
        the clock, then write, meter and caption it.

        The clock is the block's capture time when the device reports it (exact: a block that arrives late is
        not missing audio), else its arrival time (with slack for delivery jitter)."""
        w = s.writers.get(channel)
        if w is None:
            return
        mono = np.asarray(frames, dtype=np.float32)
        if mono.ndim == 2:
            mono = mono.mean(axis=1)
        if rate != SAMPLE_RATE:
            rs = s.resamplers.get(channel)
            if rs is None:
                rs = s.resamplers[channel] = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")
            mono = rs.resample_chunk(mono)
        if mono.size == 0:
            return
        busy = s.queued_seconds > BACKLOG_WARN_SECONDS
        if s.realtime.get(channel):
            # Keep every channel on the clock, so the mic and system audio stay aligned (echo detection and
            # line timing depend on it).
            if captured_at is not None:
                # Exact: where this block belongs vs. what is written. A hole (the device produced nothing,
                # e.g. no sound playing, or dropped audio) is filled with silence; audio that would run ahead
                # of the clock (a device delivering more than real time) is trimmed.
                drift = round(s.elapsed_at(captured_at) * SAMPLE_RATE) - s.written[channel]
                if drift > CAPTURE_SLACK:
                    self._pad(s, channel, w, drift, busy)
                elif drift < -CAPTURE_SLACK:
                    cut = min(mono.size, -drift)
                    mono = mono[cut:]
                    s.trimmed_seconds[channel] = s.trimmed_seconds.get(channel, 0.0) + cut / SAMPLE_RATE
                    if mono.size == 0:
                        return
            else:
                behind = round(s.elapsed_at(t) * SAMPLE_RATE) - (s.written[channel] + mono.size)
                if behind > CLOCK_SLACK:
                    self._pad(s, channel, w, behind - CLOCK_SLACK // 3, busy)
        w.write(mono)
        s.written[channel] += mono.size
        lvl = rms_level(mono)
        s.levels[channel] = round(0.6 * s.levels.get(channel, 0) + 0.4 * lvl, 3)
        if lvl > 0.15:
            s.last_sound[channel] = time.monotonic()
        if s.live and not busy:  # behind: catch up on the recording first, captions can skip
            s.live.feed(channel, mono)

    def _pad(self, s: Session, channel: str, w, gap: int, busy: bool) -> None:
        w.write(np.zeros(gap, dtype=np.float32))
        if s.live and not busy:
            s.live.feed(channel, np.zeros(gap, dtype=np.float32))
        s.written[channel] += gap
        s.gap_seconds[channel] += gap / SAMPLE_RATE
        if gap > SAMPLE_RATE // 2:
            log.info("%s: no audio for %.2f s; filled with silence", channel, gap / SAMPLE_RATE)

    def pause(self) -> None:
        s = self._require()
        if not s.paused:
            s.paused, s._pause_t = True, time.monotonic()
            self.publish({"type": "status", "meeting_id": s.meeting_id, "status": "paused"})

    def resume(self) -> None:
        s = self._require()
        if s.paused:
            s.paused_total += time.monotonic() - s._pause_t
            s.paused = False
            for ch in s.last_sound:
                s.last_sound[ch] = time.monotonic()
            self.publish({"type": "status", "meeting_id": s.meeting_id, "status": "recording"})

    def mark(self) -> int:
        s = self._require()
        ms = int(s.elapsed() * 1000)
        s.bookmarks.append(ms)
        self.store.update_meeting(s.meeting_id, bookmarks=s.bookmarks)
        self.publish({"type": "bookmark", "meeting_id": s.meeting_id, "at_ms": ms})
        return ms

    def stop(self) -> str:
        with self._lock:
            s = self._require()
            self.session = None  # no new frames are queued from here
        for c in s.sources:
            try:
                c.stop()
            except Exception as exc:
                log.warning("stopping %s: %s", c.channel, exc)
        s.queue.put(None)  # the writer finishes what was queued, then exits
        if s.writer:
            s.writer.join(timeout=60)
        for ch, rs in s.resamplers.items():
            tail = rs.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
            if tail.size and ch in s.writers:
                s.writers[ch].write(tail)
        for w in s.writers.values():
            w.close()
        if any(s.gap_seconds.values()) or s.trimmed_seconds:
            log.info("Recording timeline: filled %s s, trimmed %s s",
                     {k: round(v, 2) for k, v in s.gap_seconds.items()},
                     {k: round(v, 2) for k, v in s.trimmed_seconds.items()})
        if s.live:
            s.live.stop()
        duration_ms = int(s.elapsed() * 1000)
        self.store.update_meeting(s.meeting_id, status="processing", ended_at=now_iso(), duration_ms=duration_ms,
                                  bookmarks=s.bookmarks)
        self.publish({"type": "recording_stopped", "meeting_id": s.meeting_id, "duration_ms": duration_ms})
        self.on_stopped(s.meeting_id)
        return s.meeting_id

    def _revive_dead_sources(self, s: Session, now: float) -> None:
        """A source that delivers no data at all (not just silence) lost its device: reopen it, at most every
        15 s. The gap is filled with silence by the clock padding, so the timeline stays right."""
        for c in s.sources:
            ch = c.channel
            if not getattr(c, "realtime", True) or now - s.last_frame.get(ch, now) < DEAD_SOURCE_SECONDS:
                continue
            if getattr(c, "quiet_when_idle", False) and c.alive():
                continue  # e.g. the macOS tap: no data just means nothing is playing
            if now - s.restarted_at.get(ch, 0) < 15:
                continue
            s.restarted_at[ch] = now
            log.warning("No audio data from %s for %.0f s; reopening the device", ch, now - s.last_frame[ch])
            try:
                c.restart(self._on_frames)
                s.warnings[f"device_{ch}"] = (f"The {'microphone' if ch != 'system' else 'system audio'} device "
                                              f"changed; recording continues on {getattr(c, 'description', 'it')}.")
                s.last_frame[ch] = time.monotonic()
            except Exception as exc:
                s.warnings[f"device_{ch}"] = f"Lost the {ch} device and could not reopen it: {exc}"

    def _stop_quietly(self) -> None:
        """Watchdog stop: the user may have pressed Stop at the same moment."""
        try:
            self.stop()
        except RuntimeError:
            pass

    def _require(self) -> Session:
        if not self.session:
            raise RuntimeError("Not recording")
        return self.session

    def status(self) -> dict:
        s = self.session
        if not s:
            return {"recording": False}
        return {
            "recording": True,
            "paused": s.paused,
            "meeting_id": s.meeting_id,
            "elapsed_ms": int(s.elapsed() * 1000),
            "levels": s.levels,
            "warnings": s.warnings,
            "bookmarks": s.bookmarks,
            "sources": {c.channel: getattr(c, "description", "") for c in s.sources},
            "live": {
                "enabled": bool(s.live and s.live.enabled),
                "paused": bool(s.live and s.live.paused),
                "error": s.live.error if s.live else None,
            },
        }

    def live_lines(self, meeting_id: str, limit: int = 500) -> list[dict]:
        s = self.session
        if s and s.meeting_id == meeting_id and s.live:
            return list(s.live.lines)[-limit:]
        return []

    # ------------------------------------------------------------------
    def _watch(self) -> None:
        while True:
            time.sleep(2)
            s = self.session
            if s is None:
                return
            now = time.monotonic()
            hours = s.elapsed() / 3600
            if not s.paused:
                for ch, last in s.last_sound.items():
                    key = f"silent_{ch}"
                    if now - last > self.cfg.capture.silence_warning_seconds:
                        label = "system audio" if ch == "system" else ch
                        s.warnings[key] = f"No {label} for {int(now - last)} s. Is the meeting muted?"
                    else:
                        s.warnings.pop(key, None)
            if hours >= self.cfg.capture.warn_hours:
                s.warnings["long"] = f"Recording for {hours:.1f} h; safety stop at {self.cfg.capture.max_hours:g} h."
            try:
                free_gb = shutil.disk_usage(self.cfg.data_dir).free / 1024**3
                if free_gb < self.cfg.capture.min_free_gb:
                    s.warnings["disk"] = f"Low disk space ({free_gb:.1f} GB); stopping to protect the recording."
                    self.publish({"type": "status", "meeting_id": s.meeting_id, "status": "auto_stop_disk"})
                    self._stop_quietly()
                    return
            except Exception:
                pass
            if hours >= self.cfg.capture.max_hours:
                self.publish({"type": "status", "meeting_id": s.meeting_id, "status": "auto_stop_max_hours"})
                self._stop_quietly()
                return
            if not s.paused:
                self._revive_dead_sources(s, now)
            if s.queued_seconds > BACKLOG_WARN_SECONDS:
                s.warnings["busy"] = "This computer is busy; live captions are paused. The recording is safe."
            else:
                s.warnings.pop("busy", None)
            lost = sum(getattr(c, "overflows", 0) for c in s.sources)
            if lost:
                s.warnings["dropped"] = (f"The audio device dropped sound {lost} time(s); timing was kept. "
                                         "Close heavy apps if this keeps happening.")
            self.publish({"type": "recorder", **self.status()})
