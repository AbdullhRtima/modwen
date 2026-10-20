"""API routes for first-run setup, model downloads, the managed AI model, settings, people and meeting audio."""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from modwen.egress import EgressBlocked, ensure_local

log = logging.getLogger(__name__)

# Settings the UI may change, as dotted paths into Config.
EDITABLE = {
    "notes.summary_language", "notes.timezone", "notes.chunk_minutes",
    "stt.meeting_language", "stt.glossary", "stt.live.provider", "stt.live.model", "stt.final.model",
    "capture.mic_device", "capture.system_backend", "capture.system_device",
    "notify.desktop", "notify.sound", "llm.managed.autostart", "app.lan_access",
    "diarization.remember_my_voice",
    "privacy.retention.audio_days", "privacy.retention.transcript_days", "privacy.retention.notes_days",
}


class ManagedInstall(BaseModel):
    model_choice: str | None = None
    variant: str | None = None


class ExternalLLM(BaseModel):
    base_url: str
    model: str | None = None
    api_key: str | None = None
    context_window: int = 16384
    name: str = "my_local_llm"
    no_think_suffix: bool = False


class ProfileEdit(BaseModel):
    base_url: str
    model: str
    api_key: str | None = None
    context_window: int = 16384
    temperature: float = 0.3
    structured_output: str = "json_schema"
    thinking: bool = False


class Person(BaseModel):
    display_name: str
    display_name_ar: str | None = None
    email: str | None = None


def _get(cfg, path: str):
    obj = cfg
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set(cfg, path: str, value) -> None:
    *parents, last = path.split(".")
    obj = cfg
    for part in parents:
        obj = getattr(obj, part)
    setattr(obj, last, value)


def register(app: FastAPI, s) -> None:  # noqa: C901 - a flat list of small routes
    from modwen.runtime.llm_manager import MODEL_CHOICES, TIER_MODEL, default_variant
    from modwen.runtime import whisper_models

    def save():
        from modwen.config import save_config

        return str(save_config(s.cfg))

    # ----------------------------------------------------------------- setup status
    @app.get("/api/setup/status")
    def setup_status():
        backend, live, final = s.model_choice
        llm = s.llm_status()
        return {
            "setup_complete": s.cfg.app.setup_complete,
            "hardware": s.hardware.to_dict(),
            "plan": s.plan.to_dict(),
            "whisper": {
                "backend": backend,
                "models": [{"name": m, "role": r, "present": whisper_models.is_present(s.cfg, m, backend)}
                           for m, r in ((final, "final"), (live, "live"))],
            },
            "llm": {"active": s.cfg.llm.active, "online": bool(llm.get("ok")), "health": llm,
                    "managed": s.llm_manager.status(),
                    "recommended": TIER_MODEL.get(s.plan.tier, "qwen3-8b"),
                    "choices": [{"id": k, "file": v[1], "size_gb": v[4], "context": v[3]}
                                for k, v in MODEL_CHOICES.items()],
                    "default_variant": default_variant(s.plan)},
            "config_path": s.cfg.loaded_from,
            "jobs": s.tasks.list(),
        }

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": s.tasks.list()}

    @app.post("/api/setup/whisper/download")
    def whisper_download():
        from modwen.diarize import download_sherpa_models, sherpa_models_present

        backend, live, final = s.model_choice
        models = [m for m in dict.fromkeys([final, live]) if not whisper_models.is_present(s.cfg, m, backend)]
        try:
            import sherpa_onnx  # noqa: F401

            speakers = not sherpa_models_present(s.cfg.models_dir)
        except ImportError:
            speakers = False
        if not models and not speakers:
            return {"job": None, "message": "Speech models already downloaded"}
        steps = len(models) + int(speakers)

        def run(job, update):
            for i, m in enumerate(models, 1):
                update(step=f"Speech model {i}/{steps}: Whisper {m}", done=0, total=0)
                whisper_models.download(s.cfg, m, backend, update)
            if speakers:
                update(step=f"Speech model {steps}/{steps}: speaker detection", done=0, total=0)
                download_sherpa_models(s.cfg.models_dir, lambda done, total: update(done=done, total=total))
            s.reset_engines()

        return {"job": s.tasks.start("whisper", "Download speech models", run).to_dict()}

    # ----------------------------------------------------------------- managed AI model
    @app.post("/api/setup/llm/managed/install")
    def managed_install(req: ManagedInstall):
        choice = req.model_choice or TIER_MODEL.get(s.plan.tier, "qwen3-8b")
        if choice not in MODEL_CHOICES:
            raise HTTPException(400, f"Unknown model {choice}")
        variant = req.variant or default_variant(s.plan)

        def run(job, update):
            s.llm_manager.stop()
            s.llm_manager.install(choice, variant, update)
            save()
            update(step="Starting the AI model")
            s.llm_manager.start()

        return {"job": s.tasks.start("llm", f"Install {choice}", run).to_dict()}

    @app.post("/api/setup/llm/managed/start")
    def managed_start():
        try:
            s.llm_manager.register_profile()
            save()
            return s.llm_manager.start(wait=60)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/setup/llm/managed/stop")
    def managed_stop():
        s.llm_manager.stop()
        return s.llm_manager.status()

    @app.post("/api/setup/llm/external")
    def use_external(req: ExternalLLM):
        """Bring your own local server (Ollama, LM Studio, llama.cpp, vLLM): localhost only."""
        from modwen.config import LLMProfile
        from modwen.notes.llm_client import LLMClient

        try:
            ensure_local(req.base_url.rstrip("/"))
        except EgressBlocked as exc:
            raise HTTPException(400, str(exc)) from exc
        name = re.sub(r"[^a-z0-9_]+", "_", req.name.lower()).strip("_") or "my_local_llm"
        base = req.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        prof = LLMProfile(base_url=base, model=req.model or "local", api_key=req.api_key or "sk-local",
                          context_window=req.context_window, no_think_suffix=req.no_think_suffix)
        health = LLMClient(prof, name=name, cfg=s.cfg).health()
        if not health.get("ok"):
            raise HTTPException(400, f"Could not reach {base}: {health.get('error')}")
        if not req.model and health.get("models"):
            prof.model = health["models"][0]
        s.cfg.llm.profiles[name] = prof
        s.cfg.llm.active = name
        s.cfg.llm.managed.autostart = False if s.cfg.llm.managed.enabled else s.cfg.llm.managed.autostart
        save()
        return {"profile": name, "model": prof.model, "models": health.get("models", []), "base_url": base}

    # ----------------------------------------------------------------- profiles
    @app.put("/api/llm/profiles/{name}")
    def edit_profile(name: str, req: ProfileEdit):
        from modwen.config import LLMProfile

        try:
            ensure_local(req.base_url)
        except EgressBlocked as exc:
            raise HTTPException(400, str(exc)) from exc
        old = s.cfg.llm.profiles.get(name)
        extra = dict(old.extra_body) if old else {}
        if req.thinking:
            extra.pop("chat_template_kwargs", None)
        else:
            extra["chat_template_kwargs"] = {"enable_thinking": False}
        s.cfg.llm.profiles[name] = LLMProfile(
            base_url=req.base_url, model=req.model, api_key=req.api_key or (old.api_key if old else "sk-local"),
            context_window=req.context_window, temperature=req.temperature,
            structured_output=req.structured_output, extra_body=extra,
            no_think_suffix=(not req.thinking) and "11434" in req.base_url)
        save()
        return {"saved": name}

    @app.delete("/api/llm/profiles/{name}")
    def delete_profile(name: str):
        if name == s.cfg.llm.active:
            raise HTTPException(400, "Switch to another profile before deleting the active one")
        s.cfg.llm.profiles.pop(name, None)
        s.cfg.llm.fallback = [f for f in s.cfg.llm.fallback if f != name]
        save()
        return {"deleted": name}

    # ----------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        return {"settings": {k: _get(s.cfg, k) for k in sorted(EDITABLE)}, "config_path": s.cfg.loaded_from,
                "setup_complete": s.cfg.app.setup_complete}

    @app.put("/api/settings")
    def put_settings(patch: dict[str, Any]):
        from pydantic import ValidationError

        from modwen.config import Config

        unknown = set(patch) - EDITABLE
        if unknown:
            raise HTTPException(400, f"Not editable: {', '.join(sorted(unknown))}")
        # Validate the whole config with the patch applied, then copy the *validated* values to the live config
        # (so "15" becomes 15, and nothing changes at all when one value is invalid).
        draft = s.cfg.model_dump()
        for k, v in patch.items():
            *parents, last = k.split(".")
            node = draft
            for part in parents:
                node = node[part]
            node[last] = v
        try:
            checked = Config.model_validate(draft)
        except ValidationError as exc:
            err = exc.errors()[0]
            raise HTTPException(400, f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}") from exc
        for k in patch:
            _set(s.cfg, k, _get(checked, k))
        if any(k.startswith("stt.") for k in patch):
            s.reset_engines()
        return {"saved": save(), "settings": {k: _get(s.cfg, k) for k in sorted(EDITABLE)}}

    @app.post("/api/setup/audio-test")
    def audio_test(seconds: float = 3.0):
        """Record a few seconds from the mic and system audio and report levels (triggers OS permission prompts)."""
        import threading
        import time as _time

        from modwen.audio.capture import MicSource, system_source
        from modwen.audio.vad import rms_level

        if s.recorder.session:
            raise HTTPException(409, "A meeting is being recorded")
        peaks: dict[str, float] = {}
        errors: dict[str, str] = {}
        lock = threading.Lock()

        def on_frames(channel, frames, rate=None, capture_time=None):  # same callback shape as the recorder's
            with lock:
                peaks[channel] = max(peaks.get(channel, 0.0), rms_level(frames))

        sources = [MicSource(s.cfg.capture.mic_device)]
        try:
            sys_src = system_source(s.cfg)
            if sys_src:
                sources.append(sys_src)
        except Exception as exc:
            errors["system"] = str(exc)
        started = []
        for src in sources:
            try:
                src.start(on_frames)
                started.append(src)
            except Exception as exc:
                errors[src.channel] = str(exc)
        _time.sleep(max(1.0, min(seconds, 8.0)))
        for src in started:
            try:
                src.stop()
            except Exception:
                pass
        return {"levels": {k: round(v, 3) for k, v in peaks.items()}, "errors": errors,
                "sources": {src.channel: getattr(src, "description", "") for src in started}}

    @app.get("/api/network")
    def network():
        """LAN access status and the links other devices can use (only shown to token holders)."""
        from modwen.network import lan_urls

        running_lan = bool(app.state.lan)
        return {"lan_access_setting": s.cfg.app.lan_access, "lan_active": running_lan,
                "urls": lan_urls(s.cfg.app.ui_port, app.state.token) if running_lan else [],
                "restart_needed": running_lan != s.cfg.app.lan_access}

    @app.post("/api/setup/complete")
    def setup_complete():
        s.cfg.app.setup_complete = True
        return {"saved": save()}

    @app.get("/api/diagnostics", response_class=PlainTextResponse)
    def diagnostics(lines: int = 400):
        """A report to attach to a bug: versions, hardware, setup, recent log lines. No meeting content, no keys."""
        import platform
        import sys

        from modwen import __version__
        from modwen.cli import log_file

        backend, live, final = s.model_choice
        llm = s.llm_status()
        prof = s.cfg.llm.profiles.get(s.cfg.llm.active)
        out = [
            f"Modwen {__version__} · Python {sys.version.split()[0]} · {platform.platform()}",
            f"Hardware: {s.hardware.to_dict()}",
            f"Tier: {s.plan.tier} · STT {backend} final={final} live={live}",
            f"Diarizer: {s.cfg.diarization.provider} · threshold {s.cfg.diarization.threshold}",
            f"LLM: {s.cfg.llm.active} model={prof.model if prof else '?'} url={prof.base_url if prof else '?'} "
            f"ok={llm.get('ok')} {'' if llm.get('ok') else llm.get('error', '')}",
            f"Managed LLM: {s.llm_manager.status()}",
            f"Meetings: {len(s.store.list_meetings(limit=1000))} · jobs queued: {s.jobs.qsize()}",
            f"Recorder: {s.recorder.status()}",
            "", f"--- last {lines} log lines ---",
        ]
        try:
            text = log_file(s.cfg).read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 5000)):]
        except OSError:
            text = ["(no log file yet)"]
        return PlainTextResponse("\n".join(out + text))

    @app.post("/api/notify/test")
    def notify_test():
        s.notifier.notify("Notes ready: Test meeting", "This is how Modwen tells you your notes are done.", None)
        return {"sent": True}

    # ----------------------------------------------------------------- people
    @app.get("/api/people")
    def people():
        voice = s.store.voice_summary()
        return {"people": [{**p, "voice": voice.get(p["id"])} for p in s.store.list_persons()]}

    @app.delete("/api/people/{pid}/voiceprint")
    def delete_voiceprint(pid: str):
        if not s.store.get_person(pid):
            raise HTTPException(404, "Person not found")
        s.store.delete_voiceprints(pid)
        if s.store.get_person(pid)["is_self"] and s.cfg.diarization.remember_my_voice:
            s.cfg.diarization.remember_my_voice = False  # otherwise the next meeting would save it again
            save()
        return {"deleted": pid}

    @app.post("/api/people")
    def add_person(p: Person):
        try:
            return s.store.upsert_person(p.model_dump())
        except ValueError as exc:
            raise HTTPException(409 if "already" in str(exc) else 400, str(exc)) from exc

    @app.put("/api/people/{pid}")
    def edit_person(pid: str, p: Person):
        """Edit a person; a new name replaces the old one in every meeting (transcript and notes)."""
        meetings = len({sp["meeting_id"] for sp in s.store.linked_speakers(pid)})
        try:
            person = s.store.upsert_person({**p.model_dump(), "id": pid})
        except KeyError as exc:
            raise HTTPException(404, "Person not found") from exc
        except ValueError as exc:
            raise HTTPException(409 if "already" in str(exc) else 400, str(exc)) from exc
        return {**person, "meetings": meetings}

    @app.delete("/api/people/{pid}")
    def delete_person(pid: str):
        """Delete a person and their voiceprints; in meetings they become "Person N" again."""
        try:
            meetings = s.store.delete_person(pid)
        except KeyError as exc:
            raise HTTPException(404, "Person not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"deleted": pid, "meetings": meetings}

    @app.post("/api/people/{pid}/self")
    def this_is_me(pid: str):
        """This person is you: your earlier entry (and its voiceprints) merge into it."""
        try:
            return s.store.make_self(pid)
        except KeyError as exc:
            raise HTTPException(404, "Person not found") from exc

    # ----------------------------------------------------------------- audio player
    _players: dict[tuple, object] = {}
    _players_lock = threading.Lock()
    max_range = 4 * 1024 * 1024  # an open-ended range ("bytes=0-") gets this much; the browser asks for more

    @app.get("/api/meetings/{mid}/audio")
    def meeting_audio(mid: str, request: Request):
        """The meeting as one mono WAV, rendered per byte range from the encrypted chunks (decrypted in memory
        only, never written to disk). Seeking and long meetings cost the same as a short one."""
        from starlette.responses import StreamingResponse

        from modwen.audio.playback import MeetingAudio

        m = s.store.get_meeting(mid)
        if not m:
            raise HTTPException(404, "Meeting not found")
        if m.get("audio_deleted_at"):
            raise HTTPException(404, "The audio of this meeting was deleted (retention settings)")
        mdir = s.cfg.audio_dir / mid
        channels = sorted({p.name.split("-")[0] for p in mdir.glob("*-*.flac.enc")})
        if not channels:
            raise HTTPException(404, "No audio for this meeting")
        segments = s.store.get_segments(mid)
        mic_lines = [g for g in segments if g["channel"] == "mic"]
        gate = mic_lines if ("mic" in channels and "system" in channels and segments) else None
        chunk_names = tuple(sorted(p.name for p in mdir.glob("*-*.flac.enc")))
        cache_key = (mid, chunk_names, None if gate is None else tuple((g["start_ms"], g["end_ms"]) for g in gate))
        with _players_lock:
            player = _players.get(cache_key)
        if player is None:
            player = MeetingAudio(mdir, s.keystore.unwrap(s.store.get_wrapped_key(mid)), gate)
            with _players_lock:
                _players.clear()  # one meeting at a time is plenty, and frees the old one's cache
                _players[cache_key] = player
        size = player.size
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        rng = request.headers.get("range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].split(",")[0].partition("-")
            try:
                if start_s:
                    start, end = int(start_s), int(end_s) if end_s else None
                else:  # suffix range: the last N bytes
                    start, end = max(0, size - int(end_s)), size - 1
            except ValueError:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            if start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            end = min(size - 1, end if end is not None else start + max_range - 1)
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return Response(player.wav_bytes(start, end), status_code=206, media_type="audio/wav", headers=headers)

        def stream():
            for a in range(0, size, max_range):
                yield player.wav_bytes(a, min(size, a + max_range) - 1)

        headers["Content-Length"] = str(size)
        return StreamingResponse(stream(), media_type="audio/wav", headers=headers)
