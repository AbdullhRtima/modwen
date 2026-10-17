"""Recording sessions: start/pause/mark/stop, encrypted chunks, level meters, live captions, watchdog."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from typing import Callable

import numpy as np

from modwen.audio.capture import CaptureError, CaptureSource, MicSource, is_bluetooth_name, system_source
from modwen.audio.chunks import ChunkWriter
from modwen.audio.vad import rms_level
from modwen.pipeline.live import LiveTranscriber
from modwen.storage import now_iso

log = logging.getLogger(__name__)


class RecorderBusy(RuntimeError):
    pass


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

    def elapsed(self) -> float:
        now = self._pause_t if self.paused else time.monotonic()
        return now - self.started - self.paused_total


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
            data_key, wrapped = self.keystore.new_data_key()
            meeting = self.store.create_meeting(
                title or "Untitled meeting", wrapped_key=wrapped, mode=mode, status="recording",
                platform=platform_name, meeting_language=meeting_language or self.cfg.stt.meeting_language,
                summary_language=self.cfg.notes.summary_language, consent=consent or {},
                llm_profile=self.cfg.llm.active, hardware_tier=hardware_tier, timezone=self.cfg.notes.timezone,
            )
            mid = meeting["id"]
            caps = extra_sources if extra_sources is not None else self.source_factory(mode, sources)
            if not caps:
                self.store.update_meeting(mid, status="failed", error="no audio sources")
                raise CaptureError("No audio sources available")
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
            self.session = session
            self._watchdog = threading.Thread(target=self._watch, name="rec-watchdog", daemon=True)
            self._watchdog.start()
        self.publish({"type": "recording_started", "meeting_id": mid, "title": meeting["title"]})
        return self.store.get_meeting(mid)  # type: ignore[return-value]

    def _on_frames(self, channel: str, frames: np.ndarray) -> None:
        s = self.session
        if s is None or s.paused:
            return
        w = s.writers.get(channel)
        if w is None:
            return
        w.write(frames)
        lvl = rms_level(frames)
        s.levels[channel] = round(0.6 * s.levels.get(channel, 0) + 0.4 * lvl, 3)
        if lvl > 0.15:
            s.last_sound[channel] = time.monotonic()
        if s.live:
            s.live.feed(channel, frames)

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
            self.session = None
        for c in s.sources:
            try:
                c.stop()
            except Exception as exc:
                log.warning("stopping %s: %s", c.channel, exc)
        for w in s.writers.values():
            w.close()
        if s.live:
            s.live.stop()
        duration_ms = int(s.elapsed() * 1000)
        self.store.update_meeting(s.meeting_id, status="processing", ended_at=now_iso(), duration_ms=duration_ms,
                                  bookmarks=s.bookmarks)
        self.publish({"type": "recording_stopped", "meeting_id": s.meeting_id, "duration_ms": duration_ms})
        self.on_stopped(s.meeting_id)
        return s.meeting_id

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
                    self.stop()
                    return
            except Exception:
                pass
            if hours >= self.cfg.capture.max_hours:
                self.publish({"type": "status", "meeting_id": s.meeting_id, "status": "auto_stop_max_hours"})
                self.stop()
                return
            self.publish({"type": "recorder", **self.status()})
