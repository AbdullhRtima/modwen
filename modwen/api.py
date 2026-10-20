"""Local web API + static UI. Bound to 127.0.0.1 only.

Security (design §10): a random session token is required for every /api and /ws call (cookie set when
the tray/CLI opens the UI with ?token=...), and Host/Origin must be localhost (blocks DNS rebinding).
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from modwen import __version__
from modwen.audio.capture import CaptureError, list_input_devices
from modwen.audio.chunks import write_channel
from modwen.egress import EgressBlocked, ensure_local
from modwen.notes.render import notes_markdown
from modwen.pipeline.recorder import RecorderBusy
from modwen.services import Services

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
from modwen.network import allowed_hosts as _allowed_hosts, host_of, origin_allowed  # noqa: E402


class StartRequest(BaseModel):
    title: str = Field("Untitled meeting", max_length=200)
    mode: Literal["online", "in_person"] = "online"
    meeting_language: Literal["auto", "ar", "en"] | None = None
    sources: list[Literal["mic", "system"]] | None = None
    platform: str = Field("other", max_length=40)
    consent_confirmed: bool = False


class KeepRequest(BaseModel):
    keep: bool


class RenameRequest(BaseModel):
    name: str


class PersonLink(BaseModel):
    person_id: str | None = None  # an entry on the People page ...
    name: str | None = None  # ... or a name (found on the People page, or added to it)
    remember_voice: bool = False  # the person agreed: keep a voiceprint (§5.1)


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


class _Point(BaseModel):
    model_config = {"extra": "allow"}  # evidence ids, original text, confidence... are kept as they are
    text: str


class _Action(BaseModel):
    model_config = {"extra": "allow"}
    task: str  # an item without its text breaks the export and the review page
    owner: str | None = None
    due_date: str | None = None


class _Question(BaseModel):
    model_config = {"extra": "allow"}
    question: str


class NotesPatch(BaseModel):
    """The parts of the notes the user may edit, each optional; items keep their extra fields."""
    model_config = {"extra": "forbid"}
    summary: dict[str, str] | None = None
    key_points: list[_Point] | None = None
    decisions: list[_Point] | None = None
    action_items: list[_Action] | None = None
    open_questions: list[_Question] | None = None


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

    def editable_or_409(mid: str) -> dict:
        """Speakers, lines and notes are rewritten while a meeting is processed: edits then would be lost."""
        m = meeting_or_404(mid)
        if m["status"] in ("recording", "processing"):
            raise HTTPException(409, "Modwen is still working on this meeting. Try again when it has finished.")
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
        if req.sources is not None and not req.sources:
            raise HTTPException(400, "Choose at least one audio source (microphone or system audio).")
        try:
            m = s.recorder.start(req.title, mode=req.mode, meeting_language=req.meeting_language,
                                 sources=req.sources, platform_name=req.platform,
                                 consent={"confirmed": True, "method": "start_dialog"}, hardware_tier=s.plan.tier)
        except RecorderBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except CaptureError as exc:
            raise HTTPException(409, str(exc)) from exc
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
        recording_or_409(s.recorder.pause)
        return s.recorder.status()

    @app.post("/api/recorder/resume")
    def resume():
        recording_or_409(s.recorder.resume)
        return s.recorder.status()

    @app.post("/api/recorder/mark")
    def mark():
        return {"at_ms": recording_or_409(s.recorder.mark)}

    def recording_or_409(action):
        try:
            return action()
        except RuntimeError as exc:
            raise HTTPException(409, "Nothing is being recorded") from exc

    @app.get("/api/meetings/{mid}/live")
    def live(mid: str):
        return {"lines": s.recorder.live_lines(mid)}

    # ----- import a recording file -----
    @app.post("/api/meetings/import")
    async def import_recording(request: Request, title: str = "Imported recording", meeting_language: str = "auto",
                               channel: str = "system"):
        """The file is the raw request body. It is streamed into an encrypted temp file (never in the clear on
        disk, never whole in memory), then decoded block by block into the meeting's encrypted chunks."""
        from starlette.concurrency import run_in_threadpool

        from modwen.audio.importer import SealedTempFile

        if request.headers.get("content-type", "").startswith("multipart/"):
            raise HTTPException(415, "Send the file as the request body (form uploads are spooled to disk "
                                     "unencrypted, so Modwen refuses them)")
        if channel not in ("system", "mic", "room"):
            raise HTTPException(400, "channel must be system, mic or room")
        if meeting_language not in ("auto", "ar", "en"):
            raise HTTPException(400, "meeting_language must be auto, ar or en")
        sealed = SealedTempFile(s.cfg.data_dir)
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(sealed.append, chunk)
                if sealed.size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"The file is larger than {MAX_UPLOAD_BYTES // 1024**3} GB")
            if not sealed.size:
                raise HTTPException(400, "The file is empty")
            return await run_in_threadpool(import_sealed, s, sealed, title=title.strip() or "Imported recording",
                                           meeting_language=meeting_language, channel=channel)
        finally:
            sealed.close()

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
            s.meetings_changed(mid, "opened")
        return {"meeting": m, "speakers": s.store.get_speakers(mid), "segments": s.store.get_segments(mid),
                "notes": s.store.latest_notes(mid)}

    @app.delete("/api/meetings/{mid}")
    def delete(mid: str):
        m = meeting_or_404(mid)
        if m["status"] == "recording" and s.recorder.session and s.recorder.session.meeting_id == mid:
            raise HTTPException(409, "This meeting is being recorded. Stop it first.")
        if s.is_queued_or_running(mid):  # also a summary-only rewrite, which keeps the meeting "ready"
            raise HTTPException(409, "Modwen is still working on this meeting. Delete it when it has finished.")
        import shutil

        shutil.rmtree(s.cfg.audio_dir / mid, ignore_errors=True)
        s.store.delete_meeting(mid)  # also removes the wrapped key -> audio unrecoverable
        s.meetings_changed(mid, "deleted")
        return {"deleted": mid}

    @app.put("/api/meetings/{mid}/keep")
    def keep(mid: str, req: KeepRequest):
        """Pin a meeting so retention never deletes it (or unpin)."""
        meeting_or_404(mid)
        s.store.update_meeting(mid, keep=int(req.keep))
        s.meetings_changed(mid, "kept")
        return s.store.get_meeting(mid)

    @app.post("/api/meetings/{mid}/retry")
    def retry(mid: str):
        if s.is_queued_or_running(meeting_or_404(mid)["id"]):
            raise HTTPException(409, "This meeting is already being processed")
        s.store.update_meeting(mid, status="processing", error=None)
        s.meetings_changed(mid, "retry")
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
        """Name a speaker. The name goes on the People page too, so it can be renamed or deleted in one place."""
        old = speaker_or_404(mid, sid)["display_name"]
        r = link_person(mid, sid, PersonLink(name=req.name))
        return {"old": old, "new": r["person"]["display_name"], "speaker_id": r["speaker_id"]}

    def refresh_note_warnings(mid: str) -> None:
        notes = s.store.latest_notes(mid)
        if notes:
            from modwen.notes.engine import refresh_warnings

            s.store.update_notes_data(notes["id"], {**notes["data"], "warnings": refresh_warnings(notes["data"])})

    def speaker_or_404(mid: str, sid: str) -> dict:
        sp = next((x for x in s.store.get_speakers(mid) if x["id"] == sid), None)
        if sp is None:
            raise HTTPException(404, "Speaker not found")
        return sp

    @app.post("/api/meetings/{mid}/speakers/{sid}/person")
    def link_person(mid: str, sid: str, req: PersonLink):
        """Say who a speaker is (a People entry, found or added by name); with remember_voice, also save their
        voiceprint. Their name then follows the People entry: rename or delete it there."""
        editable_or_409(mid)
        sp = speaker_or_404(mid, sid)
        person = s.store.get_person(req.person_id) if req.person_id else None
        if req.person_id and person is None:
            raise HTTPException(404, "Person not found")
        if person is None:
            name = (req.name or "").strip()
            if not name:
                raise HTTPException(400, "Type a name")
            if name.lower() == "you" or (sp["is_self"] and s.store.self_person(create=False)
                                         and name.lower() == s.store.self_person()["display_name"].lower()):
                person = s.store.self_person()
            else:
                person = s.store.find_person(name) or s.store.upsert_person({"display_name": name})
        if req.remember_voice and person["is_self"]:
            raise HTTPException(400, "Your own voice is saved from Settings → Privacy → Remember my voice")
        sid = s.store.link_speaker(mid, sid, person["id"])
        s.store.update_speaker(mid, sid, name_source="manual", confidence=1.0)
        refresh_note_warnings(mid)
        voice = None
        if req.remember_voice:
            s.store.record_consent(person["id"], mid)
            voice = s.enroll_speaker(mid, sid, person["id"])
        return {"speaker_id": sid, "person": s.store.get_person(person["id"]), "voice": voice,
                "speakers": s.store.get_speakers(mid)}

    @app.post("/api/meetings/{mid}/speakers/{sid}/confirm")
    def confirm_voice(mid: str, sid: str):
        """The voice match (or suggestion) is right: keep the name and learn from this meeting."""
        editable_or_409(mid)
        sp = speaker_or_404(mid, sid)
        pid = sp.get("person_id") if sp.get("name_source") == "voiceprint" else None
        pid = pid or (sp.get("suggestion") or {}).get("person_id")
        if not pid:
            raise HTTPException(400, "This speaker was not recognised by voice")
        if not s.store.get_person(pid):
            raise HTTPException(404, "That person was deleted from the People page")
        return link_person(mid, sid, PersonLink(person_id=pid, remember_voice=not s.store.get_person(pid)["is_self"]))

    @app.post("/api/meetings/{mid}/speakers/{sid}/reject")
    def reject_voice(mid: str, sid: str):
        """Not this person: undo a voice match (or drop a suggestion) and never pair them in this meeting again."""
        editable_or_409(mid)
        sp = speaker_or_404(mid, sid)
        if sp.get("name_source") == "voiceprint" and sp.get("person_id"):
            pid = sp["person_id"]
            if sp["is_self"] and sp["channel"] != "mic":  # "you" by voice in a room: not you after all
                s.store.update_speaker(mid, sid, is_self=0)
            s.store.unlink_speaker(mid, sid)
            refresh_note_warnings(mid)
        elif sp.get("suggestion"):
            pid = sp["suggestion"]["person_id"]
            s.store.update_speaker(mid, sid, suggestion=None)
        else:
            raise HTTPException(400, "Nothing to undo for this speaker")
        lines = [g["id"] for g in s.store.get_segments(mid) if g["speaker_id"] == sid]
        s.store.add_voice_reject(mid, pid, lines)
        return {"speakers": s.store.get_speakers(mid)}

    @app.post("/api/meetings/{mid}/speakers/{sid}/merge")
    def merge(mid: str, sid: str, req: MergeRequest):
        editable_or_409(mid)
        try:
            kept = s.store.merge_speakers(mid, sid, req.into)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        refresh_note_warnings(mid)
        return {"merged": sid if kept != sid else req.into, "into": kept, "speakers": s.store.get_speakers(mid)}

    @app.post("/api/meetings/{mid}/speakers/redetect")
    def redetect(mid: str, req: RedetectRequest):
        meeting_or_404(mid)
        if req.num_speakers is not None and not 1 <= req.num_speakers <= 20:
            raise HTTPException(400, "num_speakers must be between 1 and 20")
        s.enqueue_redetect(mid, num_speakers=req.num_speakers)
        return {"queued": True}

    @app.post("/api/meetings/{mid}/segments/{seg_id}/speaker")
    def set_segment_speaker(mid: str, seg_id: str, req: SegmentSpeaker):
        editable_or_409(mid)
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
        editable_or_409(mid)
        s.store.update_segment_text(mid, seg_id, req.text)
        return {"ok": True}

    @app.put("/api/meetings/{mid}/notes")
    def edit_notes(mid: str, req: NotesEdit):
        editable_or_409(mid)
        notes = s.store.latest_notes(mid)
        if not notes:
            raise HTTPException(404, "No notes yet")
        from modwen.notes.engine import refresh_warnings

        try:
            patch = NotesPatch.model_validate(req.data).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise HTTPException(400, f"Invalid notes: {exc.errors()[0]['msg']} at "
                                     f"{'.'.join(str(x) for x in exc.errors()[0]['loc'])}") from exc
        data = {**notes["data"], **patch, "edited": True}
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
            headers["Content-Disposition"] = attachment_header(m["title"], (m["started_at"] or "")[:10], "md")
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
            # Always revalidate: after an update the browser must not keep the old UI (assets are hashed).
            return FileResponse(UI_DIST / "index.html", headers={"Cache-Control": "no-cache"})

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


MAX_UPLOAD_BYTES = 8 * 1024**3


def attachment_header(title: str, date: str, ext: str) -> str:
    """Content-Disposition for a download named after the meeting. Headers must be Latin-1, so an Arabic title
    goes in the RFC 5987 ``filename*`` (browsers use it) with an ASCII fallback."""
    from urllib.parse import quote

    def slug(text: str, ascii_only: bool) -> str:
        keep = (lambda c: c.isascii() and c.isalnum()) if ascii_only else str.isalnum
        return "-".join("".join(c if keep(c) else " " for c in text).split()).lower()

    fallback = f"{slug(title, True) or 'meeting'}-{date}.{ext}".replace("-.", ".")
    full = f"{slug(title, False) or 'meeting'}-{date}.{ext}".replace("-.", ".")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(full)}"


def import_sealed(s: Services, sealed, *, title: str, meeting_language: str, channel: str) -> dict:
    """Create a meeting from an uploaded recording (see ``modwen.audio.importer``) and queue it."""
    import shutil

    from modwen.audio.chunks import ChunkWriter
    from modwen.audio.importer import decode_into, probe

    try:
        info = probe(sealed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    data_key, wrapped = s.keystore.new_data_key()
    m = s.store.create_meeting(title, wrapped_key=wrapped, status="processing",
                               mode="online" if channel != "room" else "in_person", meeting_language=meeting_language,
                               platform="import", summary_language=s.cfg.notes.summary_language,
                               llm_profile=s.cfg.llm.active, hardware_tier=s.plan.tier, channels=[channel],
                               timezone=s.cfg.notes.timezone, duration_ms=int(info.duration * 1000))
    mdir = s.cfg.audio_dir / m["id"]
    try:
        decode_into(sealed, ChunkWriter(mdir, channel, data_key, s.cfg.capture.chunk_seconds))
    except Exception as exc:
        shutil.rmtree(mdir, ignore_errors=True)
        s.store.delete_meeting(m["id"])
        raise HTTPException(400, f"Could not read the audio file: {exc}") from exc
    s.enqueue_processing(m["id"])
    s.meetings_changed(m["id"], "imported")
    return s.store.get_meeting(m["id"])  # type: ignore[return-value]


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
    s.meetings_changed(m["id"], "imported")
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
