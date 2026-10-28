"""Desktop notifications ("Notes ready"). Windows toast / macOS Notification Center via desktop-notifier,
with an osascript fallback on macOS. Never blocks the caller."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import webbrowser

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg, open_url_base: str | None = None):
        self.cfg = cfg
        self.base = open_url_base
        self.sent: list[tuple[str, str]] = []  # for tests / diagnostics
        self.for_app: list[dict] = []  # desktop app: notifications it posts itself (see notify)
        self._lock = threading.Lock()

    def take_for_app(self) -> list[dict]:
        with self._lock:
            out, self.for_app = self.for_app, []
        return out

    def notify(self, title: str, body: str, meeting_id: str | None = None) -> None:
        self.sent.append((title, body))
        log.info("Notification for meeting %s", meeting_id or "-")  # never the text: logs hold no meeting content
        if not self.cfg.notify.desktop:
            return
        if os.environ.get("MODWEN_DESKTOP"):
            # Inside the desktop app only the app may post notifications (macOS requires an app bundle): queue it
            # for the app, which collects it with its next menu-bar update (/api/tray).
            with self._lock:
                self.for_app.append({"title": title, "body": body, "meeting_id": meeting_id})
                del self.for_app[:-20]
            return
        url = f"{self.base}/#/meetings/{meeting_id}" if self.base and meeting_id else self.base
        threading.Thread(target=self._send, args=(title, body, url), daemon=True).start()

    def _send(self, title: str, body: str, url: str | None) -> None:
        try:
            import asyncio

            import desktop_notifier
            from desktop_notifier import Button, DesktopNotifier

            sound = getattr(desktop_notifier, "DEFAULT_SOUND", None) if self.cfg.notify.sound else None

            async def run():
                notifier = DesktopNotifier(app_name="Modwen")
                buttons = [Button(title="Open", on_pressed=lambda: url and webbrowser.open(url))] if url else []
                await notifier.send(title=title, message=body, buttons=buttons,
                                    on_clicked=(lambda: webbrowser.open(url)) if url else None,
                                    sound=sound)
                await asyncio.sleep(30)  # keep callbacks alive briefly

            asyncio.run(run())
            return
        except Exception as exc:
            log.debug("desktop-notifier failed: %s", exc)
        if platform.system() == "Darwin":
            safe_t = title.replace('"', "'")
            safe_b = body.replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{safe_b}" with title "{safe_t}"'],
                           capture_output=True, timeout=10)
