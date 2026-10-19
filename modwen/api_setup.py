"""API routes for first-run setup, model downloads, the managed AI model, settings, people and meeting audio."""

from __future__ import annotations

import io
import logging
import re
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from modwen.egress import EgressBlocked, ensure_local
from modwen.pipeline.merge import gate_to_lines

log = logging.getLogger(__name__)

# Settings the UI may change, as dotted paths into Config.
EDITABLE = {
    "notes.summary_language", "notes.timezone", "notes.chunk_minutes",
    "stt.meeting_language", "stt.glossary", "stt.live.provider", "stt.live.model", "stt.final.model",
    "capture.mic_device", "capture.system_backend", "capture.system_device",
    "notify.desktop", "notify.sound", "llm.managed.autostart", "app.lan_access",
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
        before = s.cfg.model_dump()
        for k, v in patch.items():
            _set(s.cfg, k, v)
        try:
            Config.model_validate(s.cfg.model_dump())
        except ValidationError as exc:
            for k in patch:
                _set(s.cfg, k, _get(Config.model_validate(before), k))
            raise HTTPException(400, str(exc)) from exc
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

        def on_frames(channel, frames):
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

    @app.post("/api/notify/test")
    def notify_test():
        s.notifier.notify("Notes ready: Test meeting", "This is how Modwen tells you your notes are done.", None)
        return {"sent": True}

    # ----------------------------------------------------------------- people
    @app.get("/api/people")
    def people():
        return {"people": s.store.list_persons()}

    @app.post("/api/people")
    def add_person(p: Person):
        try:
            return s.store.upsert_person(p.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/people/{pid}")
    def edit_person(pid: str, p: Person):
        return s.store.upsert_person({**p.model_dump(), "id": pid})

    @app.delete("/api/people/{pid}")
    def delete_person(pid: str):
        s.store.delete_person(pid)
        return {"deleted": pid}

    # ----------------------------------------------------------------- audio player
    _cache: dict[tuple, bytes] = {}

    @app.get("/api/meetings/{mid}/audio")
    def meeting_audio(mid: str, request: Request):
        """All channels mixed to mono FLAC, decrypted in memory only (never written to disk)."""
        import soundfile as sf

        from modwen.audio.chunks import list_channels, read_channel

        m = s.store.get_meeting(mid)
        if not m:
            raise HTTPException(404, "Meeting not found")
        segments = s.store.get_segments(mid)
        mic_lines = [g for g in segments if g["channel"] == "mic"]
        # The mix depends on which mic lines exist, so moving lines or re-detecting speakers rebuilds it.
        cache_key = (mid, bool(segments), tuple((g["start_ms"], g["end_ms"]) for g in mic_lines))
        data = _cache.get(cache_key)
        if data is None:
            key = s.keystore.unwrap(s.store.get_wrapped_key(mid))
            mdir = s.cfg.audio_dir / mid
            channels = list_channels(mdir)
            tracks = [read_channel(mdir, ch, key) for ch in channels]
            if not tracks:
                raise HTTPException(404, "No audio for this meeting")
            if "mic" in channels and "system" in channels and segments:
                # Without headphones the mic also hears the call, a little late: mixed in, every remote
                # sentence plays twice. Keep the mic only where a local voice was transcribed.
                i = channels.index("mic")
                tracks[i] = gate_to_lines(tracks[i], mic_lines)
            n = max(t.size for t in tracks)
            mix = np.zeros(n, dtype=np.float32)
            for t in tracks:
                mix[: t.size] += t
            peak = float(np.max(np.abs(mix))) or 1.0
            mix = mix / max(1.0, peak)
            bio = io.BytesIO()
            sf.write(bio, mix, 16000, format="FLAC", subtype="PCM_16")
            data = bio.getvalue()
            _cache.clear()
            _cache[cache_key] = data
        size = len(data)
        rng = request.headers.get("range")
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s or 0)
            end = min(int(end_s) if end_s else size - 1, size - 1)
            if start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return Response(data[start : end + 1], status_code=206, media_type="audio/flac", headers=headers)
        return Response(data, media_type="audio/flac", headers=headers)
