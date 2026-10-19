"""Local web API + static UI. Bound to 127.0.0.1 only.

Security (design §10): a random session token is required for every /api and /ws call (cookie set when
the tray/CLI opens the UI with ?token=...), and Host/Origin must be localhost (blocks DNS rebinding).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from modwen import __version__
from modwen.audio.capture import CaptureError, list_input_devices
from modwen.audio.chunks import load_audio_file, write_channel
from modwen.egress import EgressBlocked, ensure_local
from modwen.notes.render import notes_markdown
from modwen.pipeline.recorder import RecorderBusy
from modwen.services import Services

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
from modwen.network import allowed_hosts as _allowed_hosts, host_of, origin_allowed  # noqa: E402


class StartRequest(BaseModel):
    title: str = "Untitled meeting"
    mode: str = "online"
    meeting_language: str | None = None
    sources: list[str] | None = None
    platform: str = "other"
    consent_confirmed: bool = False


class RenameRequest(BaseModel):
    name: str


class MergeRequest(BaseModel):
    into: str


class RegenerateRequest(BaseModel):
    summary_language: str | None = None
    llm_profile: str | None = None
    scope: str = "all"  # "all" (re-extract items + summary) | "summary" (keep edited items)


class SegmentEdit(BaseModel):
    text: str


class SegmentSpeaker(BaseModel):
    speaker_id: str  # an existing speaker id, or "new" for a new "Person N"


class RedetectRequest(BaseModel):
    num_speakers: int | None = None  # everyone in the meeting, you included; None = detect automatically


class NotesEdit(BaseModel):
    data: dict[str, Any]


class ProfileTest(BaseModel):
    profile: str | None = None


class ActiveProfile(BaseModel):
    profile: str


def create_app(services: Services, token: str | None = None, lan: bool | None = None) -> FastAPI:
    app = FastAPI(title="Modwen", version=__version__, docs_url=None, redoc_url=None)
    app.state.services = services
    app.state.token = token or secrets.token_urlsafe(24)
    app.state.lan = services.cfg.app.lan_access if lan is None else lan
    app.state.allowed_hosts = _allowed_hosts(services.cfg, app.state.lan)
    s = services

    # ----- security middleware -----
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = host_of(request.headers.get("host"))
        if host not in app.state.allowed_hosts:
            return PlainTextResponse("Forbidden host. To use Modwen from another device, start it with "
                                     "`modwen serve --lan`.", status_code=403)
        if not origin_allowed(request.headers.get("origin"), app.state.allowed_hosts):
            return PlainTextResponse("Forbidden origin", status_code=403)
        path = request.url.path
        q_token = request.query_params.get("token")
        if q_token and secrets.compare_digest(q_token, app.state.token):
            resp = RedirectResponse(url=path if path != "/" else "/", status_code=303)
            resp.set_cookie("mw_token", app.state.token, httponly=True, samesite="strict")
            return resp
        if path.startswith("/api/"):
            supplied = request.cookies.get("mw_token") or request.headers.get("x-modwen-token") or ""
            if not secrets.compare_digest(supplied, app.state.token):
                return JSONResponse({"detail": "Missing or invalid session token. Open Modwen from the tray."},
                                    status_code=401)
        return await call_next(request)

    def meeting_or_404(mid: str) -> dict:
        m = s.store.get_meeting(mid)
        if not m:
            raise HTTPException(404, "Meeting not found")
        return m

    # ----- status -----
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def status():
        return {
            "version": __version__,
            "recorder": s.recorder.status(),
            "local_only": True,
            "llm_active": s.cfg.llm.active,
            "tier": s.plan.tier,
            "summary_language": s.cfg.notes.summary_language,
            "setup_complete": s.cfg.app.setup_complete,
            "jobs_pending": s.jobs.qsize(),
        }

    @app.get("/api/hardware")
    def hardware():
        backend, live, final = s.model_choice
        return {"hardware": s.hardware.to_dict(), "plan": s.plan.to_dict(),
                "chosen": {"backend": backend, "live_model": live, "final_model": final}}

    @app.get("/api/devices")
    def devices():
        return {"inputs": list_input_devices()}

    # ----- recording -----
    @app.post("/api/meetings/start")
    def start(req: StartRequest):
        if not req.consent_confirmed:
            raise HTTPException(400, "Confirm that participants were informed before recording.")
        try:
            m = s.recorder.start(req.title, mode=req.mode, meeting_language=req.meeting_language,
                                 sources=req.sources, platform_name=req.platform,
                                 consent={"confirmed": True, "method": "start_dialog"}, hardware_tier=s.plan.tier)
        except RecorderBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except CaptureError as exc:
            raise HTTPException(500, str(exc)) from exc
        return m

    @app.post("/api/recorder/stop")
    def stop():
        try:
            mid = s.recorder.stop()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"meeting_id": mid}

    @app.post("/api/recorder/pause")
    def pause():
        s.recorder.pause()
        return s.recorder.status()

    @app.post("/api/recorder/resume")
    def resume():
        s.recorder.resume()
        return s.recorder.status()

    @app.post("/api/recorder/mark")
    def mark():
        return {"at_ms": s.recorder.mark()}

    @app.get("/api/meetings/{mid}/live")
    def live(mid: str):
        return {"lines": s.recorder.live_lines(mid)}

    # ----- import a recording file (testing without a live meeting) -----
    @app.post("/api/meetings/import")
    async def import_recording(file: UploadFile = File(...), title: str = Form("Imported recording"),
                               meeting_language: str = Form("auto"), channel: str = Form("system")):
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, dir=s.cfg.data_dir, delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        try:
            audio = await asyncio.to_thread(load_audio_file, tmp_path)
        except Exception as exc:
            raise HTTPException(400, f"Could not read audio file: {exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        return import_audio(s, audio, title=title, meeting_language=meeting_language, channel=channel)

    # ----- meetings -----
    @app.get("/api/meetings")
    def meetings():
        return {"meetings": s.store.list_meetings()}

    @app.get("/api/meetings/{mid}")
    def meeting(mid: str):
        m = meeting_or_404(mid)
        if m["status"] == "ready" and not m.get("opened_at"):
            from modwen.storage import now_iso

            s.store.update_meeting(mid, opened_at=now_iso())
        return {"meeting": m, "speakers": s.store.get_speakers(mid), "segments": s.store.get_segments(mid),
                "notes": s.store.latest_notes(mid)}

    @app.delete("/api/meetings/{mid}")
    def delete(mid: str):
        meeting_or_404(mid)
        import shutil

        shutil.rmtree(s.cfg.audio_dir / mid, ignore_errors=True)
        s.store.delete_meeting(mid)  # also removes the wrapped key -> audio unrecoverable
        return {"deleted": mid}

    @app.post("/api/meetings/{mid}/retry")
    def retry(mid: str):
        meeting_or_404(mid)
        s.enqueue_processing(mid)
        return {"queued": True}

    @app.post("/api/meetings/{mid}/regenerate")
    def regenerate(mid: str, req: RegenerateRequest):
        meeting_or_404(mid)
        if req.llm_profile and req.llm_profile not in s.cfg.llm.profiles:
            raise HTTPException(400, f"Unknown LLM profile {req.llm_profile}")
        if req.scope not in ("all", "summary"):
            raise HTTPException(400, "scope must be 'all' or 'summary'")
        s.enqueue_regenerate(mid, summary_language=req.summary_language, llm_profile=req.llm_profile, scope=req.scope)
        return {"queued": True}

    @app.post("/api/meetings/{mid}/speakers/{sid}/rename")
    def rename(mid: str, sid: str, req: RenameRequest):
        meeting_or_404(mid)
        old = s.store.rename_speaker(mid, sid, req.name.strip())
        if old is None:
            raise HTTPException(404, "Speaker not found")
        notes = s.store.latest_notes(mid)
        if notes:
            from modwen.notes.engine import refresh_warnings

            s.store.update_notes_data(notes["id"], {**notes["data"], "warnings": refresh_warnings(notes["data"])})
        return {"old": old, "new": req.name.strip()}

    @app.post("/api/meetings/{mid}/speakers/{sid}/merge")
    def merge(mid: str, sid: str, req: MergeRequest):
        meeting_or_404(mid)
        s.store.merge_speakers(mid, sid, req.into)
        return {"merged": sid, "into": req.into}

    @app.post("/api/meetings/{mid}/speakers/redetect")
    def redetect(mid: str, req: RedetectRequest):
        meeting_or_404(mid)
        if req.num_speakers is not None and not 1 <= req.num_speakers <= 20:
            raise HTTPException(400, "num_speakers must be between 1 and 20")
        s.enqueue_redetect(mid, num_speakers=req.num_speakers)
        return {"queued": True}

    @app.post("/api/meetings/{mid}/segments/{seg_id}/speaker")
    def set_segment_speaker(mid: str, seg_id: str, req: SegmentSpeaker):
        meeting_or_404(mid)
        sid = req.speaker_id
        seg = next((x for x in s.store.get_segments(mid) if x["id"] == seg_id), None)
        if seg is None:
            raise HTTPException(404, "Line not found")
        if sid == "new":
            sid = s.store.add_speaker(mid, seg.get("channel"))["id"]
        if not s.store.set_segment_speaker(mid, seg_id, sid):
            raise HTTPException(404, "Line or speaker not found")
        notes = s.store.latest_notes(mid)
        if notes:
            from modwen.notes.engine import retarget_line

            lines = {x["id"]: x["speaker_name"] for x in s.store.get_segments(mid)}
            data = retarget_line(notes["data"], seg_id, seg.get("speaker_name") or "", lines[seg_id] or "", lines)
            if data != notes["data"]:
                s.store.update_notes_data(notes["id"], data)
        return {"speaker_id": sid, "speakers": s.store.get_speakers(mid)}

    @app.put("/api/meetings/{mid}/segments/{seg_id}")
    def edit_segment(mid: str, seg_id: str, req: SegmentEdit):
        meeting_or_404(mid)
        s.store.update_segment_text(mid, seg_id, req.text)
        return {"ok": True}

    @app.put("/api/meetings/{mid}/notes")
    def edit_notes(mid: str, req: NotesEdit):
        meeting_or_404(mid)
        notes = s.store.latest_notes(mid)
        if not notes:
            raise HTTPException(404, "No notes yet")
        from modwen.notes.engine import refresh_warnings

        data = {**notes["data"], **req.data, "edited": True}
        data["warnings"] = refresh_warnings(data)
        s.store.update_notes_data(notes["id"], data)
        return s.store.latest_notes(mid)

    @app.get("/api/meetings/{mid}/export.md", response_class=PlainTextResponse)
    def export_md(mid: str, language: str | None = None, actions_only: bool = False, download: bool = False):
        m = meeting_or_404(mid)
        md = notes_markdown(m, s.store.latest_notes(mid), s.store.get_segments(mid), language=language,
                            actions_only=actions_only)
        headers = {}
        if download:
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in m["title"]).strip("-").lower()
            headers["Content-Disposition"] = f'attachment; filename="{safe or "meeting"}-{(m["started_at"] or "")[:10]}.md"'
        return PlainTextResponse(md, headers=headers, media_type="text/markdown; charset=utf-8")

    # ----- LLM profiles -----
    @app.get("/api/llm/profiles")
    def profiles():
        return {"active": s.cfg.llm.active, "fallback": s.cfg.llm.fallback,
                "profiles": {k: {**v.model_dump(exclude={"api_key"}), "has_api_key": bool(v.api_key)}
                             for k, v in s.cfg.llm.profiles.items()}}

    @app.post("/api/llm/test")
    def test_llm(req: ProfileTest):
        name = req.profile or s.cfg.llm.active
        try:
            ensure_local(s.cfg.llm.profile(name).base_url)
        except (KeyError, EgressBlocked) as exc:
            raise HTTPException(400, str(exc)) from exc
        return run_llm_test(s, name)

    @app.post("/api/llm/active")
    def set_active(req: ActiveProfile):
        if req.profile not in s.cfg.llm.profiles:
            raise HTTPException(400, f"Unknown profile {req.profile}")
        s.cfg.llm.active = req.profile
        if s.cfg.loaded_from:
            from modwen.config import save_config

            save_config(s.cfg)
        return {"active": req.profile}

    # ----- websocket -----
    @app.websocket("/ws/events")
    async def events(ws: WebSocket):
        host = host_of(ws.headers.get("host"))
        tok = ws.cookies.get("mw_token") or ws.query_params.get("token") or ""
        if host not in app.state.allowed_hosts or not origin_allowed(ws.headers.get("origin"), app.state.allowed_hosts) \
                or not secrets.compare_digest(tok, app.state.token):
            await ws.close(code=4401)
            return
        await ws.accept()
        q = s.bus.subscribe()
        try:
            await ws.send_json({"type": "hello", "recorder": s.recorder.status()})
            while True:
                event = await q.get()
                await ws.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            s.bus.unsubscribe(q)

    from modwen import api_setup

    api_setup.register(app, s)

    # ----- static UI -----
    if (UI_DIST / "index.html").exists():
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(UI_DIST / "index.html")

        @app.get("/assets/{path:path}", include_in_schema=False)
        def assets(path: str):
            target = (UI_DIST / "assets" / path).resolve()
            if not str(target).startswith(str((UI_DIST / "assets").resolve())) or not target.exists():
                raise HTTPException(404)
            return FileResponse(target)
    else:
        @app.get("/", include_in_schema=False, response_class=HTMLResponse)
        def index_missing():
            return ("<h1>Modwen</h1><p>The web UI is not built yet. Run <code>cd ui && npm install && "
                    "npm run build</code>, then restart. The API is running.</p>")

    return app


def import_audio(s: Services, audio, *, title: str, meeting_language: str = "auto", channel: str = "system") -> dict:
    """Store an in-memory 16 kHz signal as a new meeting and queue it for processing."""
    if channel not in ("system", "mic", "room"):
        raise HTTPException(400, "channel must be system, mic or room")
    data_key, wrapped = s.keystore.new_data_key()
    m = s.store.create_meeting(title, wrapped_key=wrapped, status="processing", mode="online" if channel != "room"
                               else "in_person", meeting_language=meeting_language, platform="import",
                               summary_language=s.cfg.notes.summary_language, llm_profile=s.cfg.llm.active,
                               hardware_tier=s.plan.tier, channels=[channel], timezone=s.cfg.notes.timezone,
                               duration_ms=int(audio.size / 16))
    write_channel(s.cfg.audio_dir / m["id"], channel, data_key, audio, s.cfg.capture.chunk_seconds)
    s.enqueue_processing(m["id"])
    return s.store.get_meeting(m["id"])  # type: ignore[return-value]


def run_llm_test(s: Services, name: str) -> dict:
    import time

    client = s.llm(name)
    health = client.health()
    if not health.get("ok"):
        return {"ok": False, "profile": name, "error": health.get("error"), "base_url": client.profile.base_url}
    t0 = time.monotonic()
    try:
        text = client.chat([
            {"role": "system", "content": "You write very short meeting notes."},
            {"role": "user", "content": "Summarize in one English sentence and one Arabic sentence: "
                                        "اتفقنا إن الـ release هيكون يوم الخميس، وأحمد هيعمل الـ QA قبلها."},
        ], max_tokens=200)
    except Exception as exc:
        return {"ok": False, "profile": name, "error": str(exc), "base_url": client.profile.base_url}
    return {"ok": True, "profile": name, "reply": text, "seconds": round(time.monotonic() - t0, 2),
            "models": health.get("models", []), "base_url": client.profile.base_url}
