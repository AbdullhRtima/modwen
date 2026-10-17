"""Run a child process and stop it as soon as the parent (Modwen) is gone, however Modwen exits.

Usage: python -m modwen.runtime.babysit <parent_pid> -- <command> [args...]
Used for the managed llama-server so it never outlives Modwen (crash, closed terminal, Windows console close).
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time


def main() -> int:
    import psutil

    parent = int(sys.argv[1])
    cmd = sys.argv[sys.argv.index("--") + 1 :]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    child = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr, creationflags=flags)

    def stop(*_):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
        sys.exit(0)

    for sig in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), stop)
    while child.poll() is None:
        if not psutil.pid_exists(parent):
            stop()
        time.sleep(1)
    return child.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
