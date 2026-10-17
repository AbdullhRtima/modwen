"""Tray / menu-bar icon (pystray), matching the mockup: state at a glance, start/stop without a window.

Icon colours: grey = idle, red = recording, amber = processing, green = notes ready and not yet opened.
(Global hotkeys and a timer next to the menu-bar icon come with the Tauri desktop app.)
"""

from __future__ import annotations

import threading
import time
import webbrowser

COLORS = {"idle": (138, 147, 161), "recording": (220, 38, 38), "processing": (217, 119, 6), "ready": (21, 128, 61)}


def _icon_image(state: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=COLORS.get(state, COLORS["idle"]))
    for i, h in enumerate((10, 22, 32, 22, 10)):  # voice-wave mark
        x = 14 + i * 8
        d.rounded_rectangle((x, 32 - h // 2, x + 4, 32 + h // 2), radius=2, fill=(255, 255, 255))
    return img


def _fmt(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def run_tray(services, open_url: str, stop_server) -> None:
    import pystray
    from pystray import Menu, MenuItem as Item

    rec = services.recorder
    state = {"value": "idle", "llm_ok": False, "ready": [], "processing": 0}

    def ui(fragment: str = ""):
        return lambda icon, item: webbrowser.open(open_url + fragment)

    def recording(_item=None) -> bool:
        return rec.session is not None

    def title() -> str:
        m = services.store.get_meeting(rec.session.meeting_id) if rec.session else None
        return m["title"] if m else ""

    def header(_item) -> str:
        if recording():
            return f"● Recording · {title()}"
        if state["processing"]:
            return f"◐ Modwen · processing {state['processing']} meeting(s)"
        return "Modwen · idle"

    def stop_text(_item) -> str:
        st = rec.status()
        return f"■  Stop meeting  ({_fmt(st.get('elapsed_ms', 0))})"

    def stop(icon, item):
        mid = rec.stop()
        webbrowser.open(open_url + f"#/meetings/{mid}")

    def pause(icon, item):
        rec.resume() if rec.session and rec.session.paused else rec.pause()

    def pause_text(_item) -> str:
        return "▶  Resume" if rec.session and rec.session.paused else "❚❚ Pause"

    def ready_items():
        items = [Item(f"●  Notes ready: {m['title']}  →", ui(f"#/meetings/{m['id']}")) for m in state["ready"][:3]]
        return items + ([Menu.SEPARATOR] if items else [])

    def llm_text(_item) -> str:
        return f"AI model: {services.cfg.llm.active} · {'online' if state['llm_ok'] else 'offline'}"

    def quit_app(icon, item):
        if rec.session:
            rec.stop()
        services.llm_manager.stop()
        stop_server()
        icon.stop()

    idle = lambda item: not recording()  # noqa: E731
    menu = Menu(
        Item(header, None, enabled=False),
        Menu.SEPARATOR,
        # idle
        Item("▶  Start meeting…", ui("#/start"), visible=idle, default=True),
        Item("    Start in-person meeting…", ui("#/start/in_person"), visible=idle),
        # recording
        Item(stop_text, stop, visible=recording),
        Item(pause_text, pause, visible=recording),
        Item("★  Mark moment", lambda icon, item: rec.mark(), visible=recording),
        Menu.SEPARATOR,
        Item("Show live transcript", ui("#/live"), visible=recording),
        Item(lambda i: f"●  Notes ready ({len(state['ready'])})", Menu(ready_items),
             visible=lambda i: bool(state["ready"]) and idle(i)),
        Item("Open Modwen", ui(""), visible=idle),
        Item(llm_text, None, enabled=False, visible=idle),
        Item("Settings…", ui("#/settings"), visible=idle),
        Item("🔒 Local only", None, enabled=False),
        Menu.SEPARATOR,
        Item("Quit Modwen", quit_app),
    )
    icon = pystray.Icon("modwen", _icon_image("idle"), "Modwen", menu)

    def refresher():
        last_llm = 0.0
        while True:
            time.sleep(1.5)
            try:
                meetings = services.store.list_meetings(limit=30)
                state["ready"] = [m for m in meetings if m["status"] == "ready" and not m.get("opened_at")]
                state["processing"] = sum(1 for m in meetings if m["status"] == "processing")
                if time.time() - last_llm > 15:
                    state["llm_ok"] = bool(services.llm_status().get("ok"))
                    last_llm = time.time()
                new = ("recording" if recording() else "processing" if state["processing"]
                       else "ready" if state["ready"] else "idle")
                if new != state["value"]:
                    state["value"] = new
                    icon.icon = _icon_image(new)
                icon.title = f"Modwen · {new}"
                icon.update_menu()
            except Exception:
                pass

    threading.Thread(target=refresher, daemon=True).start()
    icon.run()
