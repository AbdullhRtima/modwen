"""The checks behind ``scripts/verify-macos-import.sh``: MP4, MOV and M4A import on this Mac, one Python at a time.

    python scripts/verify_macos_import.py env    --py 3.12 --out SECTION
    python scripts/verify_macos_import.py pytest --py 3.12 --work DIR --out SECTION [-- extra pytest args]
    python scripts/verify_macos_import.py engine --py 3.12 --work DIR --out SECTION [--only a,b]

Each check appends one line to SECTION: PASS, FAIL or NOT RUN, its name, and what was measured. Nothing in a line names
a person, a home folder, this checkout's path or this computer. Every file is generated here from tones: no recording,
no model, and the only server is the engine itself on 127.0.0.1 with fake speech-to-text and a fake local LLM.

It runs on Windows too, for the team's own dry runs of the harness; that never stands in for a Mac result.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import ctypes
import getpass
import http.client
import io
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MAC = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"
MIB = 2 ** 20
GUARD_BYTES = 2304 * MIB  # a backstop above the 1,024 MiB limit: if it fires, the product's own limit didn't
TOKEN = "verify-synthetic-token"
IMPORT_FILES = ["tests/test_import_isolation.py", "tests/test_import_media.py", "tests/test_import_preflight.py",
                "tests/test_import_safety.py", "tests/test_import_ui_text.py"]
# Skipped off Windows by design (skipif in tests/test_import_isolation.py). Each has an engine check below instead.
WINDOWS_ONLY = {
    "test_a_real_kernel_limit_breach_does_not_terminate_the_engine": "engine: limits.memory-breach-fast",
    "test_cpu_and_descendant_limits_hold": "engine: limits.cpu, limits.descendants",
    "test_f24_an_intact_file_survives_every_core_being_busy": "engine: timers.busy-cores",
    "test_f24_the_child_runs_at_normal_priority": "engine: timers.priority",
}


# ----- privacy -----
def _private() -> list[tuple[re.Pattern, str]]:
    pairs = []
    for path, name in ((ROOT, "<checkout>"), (Path.home(), "~")):
        for text in {str(path), str(path).replace("\\", "/"), str(path.resolve()), str(path.resolve()).replace("\\", "/")}:
            pairs.append((re.compile(re.escape(text), re.I), name))
    for text, name in ((_safe(getpass.getuser), "<user>"), (_safe(socket.gethostname), "<host>")):
        if text and len(text) >= 3:
            pairs.append((re.compile(re.escape(text), re.I), name))
    return pairs


def _safe(fn):
    try:
        return fn()
    except Exception:
        return ""


PRIVATE = _private()


def clean(text: str) -> str:
    text = str(text)
    for pattern, name in PRIVATE:
        text = pattern.sub(name, text)
    return " ".join(text.split())


class Section:
    """Result lines for one Python, appended as they happen, so an interrupted run still reports what it did."""

    def __init__(self, path: Path, py: str):
        self.path, self.py = path, py

    def add(self, check: str, status: str, detail: str = "") -> None:
        line = f"{status:<8} py{self.py:<5} {check:<30} {clean(detail)}"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def note(self, text: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("         " + clean(text) + "\n")


# ----- env -----
def cmd_env(args) -> int:
    import platform

    import av
    import numpy
    import psutil
    import soxr

    import modwen

    rows = {
        "python": platform.python_version(),
        "implementation": f"{platform.python_implementation()} {platform.machine()}",
        "av": av.__version__,
        "ffmpeg": str(getattr(av, "ffmpeg_version_info", "unknown")),
        "libavcodec": ".".join(map(str, av.library_versions.get("libavcodec", ()))),
        "libavformat": ".".join(map(str, av.library_versions.get("libavformat", ()))),
        "numpy": numpy.__version__, "soxr": soxr.__version__, "psutil": psutil.__version__,
        "modwen source": "this checkout" if Path(modwen.__file__).resolve().is_relative_to(ROOT) else
                         "NOT this checkout",
    }
    try:
        import pytest
        rows["pytest"] = pytest.__version__
    except ImportError:
        rows["pytest"] = "missing"
    with open(args.out, "a", encoding="utf-8") as f:
        for key, value in rows.items():
            f.write(f"  py{args.py:<5} {key:<16} {clean(value)}\n")
    return 0


# ----- pytest -----
def cmd_pytest(args) -> int:
    out = Section(Path(args.out), args.py)
    work = Path(args.work).resolve()
    junit, log = work / f"junit-{args.py}.xml", work / f"pytest-{args.py}.log"
    junit.unlink(missing_ok=True)
    extra = args.extra
    cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", f"--junitxml={junit}",
           *IMPORT_FILES, *extra]
    began = time.monotonic()
    with open(log, "wb") as sink:
        process = subprocess.Popen(cmd, cwd=ROOT, stdout=sink, stderr=subprocess.STDOUT, **_session())
        try:
            code = process.wait(timeout=4 * 3600)
        except subprocess.TimeoutExpired:
            code = None
        except BaseException:  # Ctrl-C: pytest runs in its own session, so stop it here
            _kill_tree(process.pid)
            raise
        finally:
            if process.poll() is None:
                _kill_tree(process.pid)
                process.wait()
    seconds = time.monotonic() - began
    label = "partial run, extra pytest args: " + " ".join(extra) + "; " if extra else ""
    if not junit.exists():
        out.add("tests.import-suites", "FAIL", f"{label}pytest wrote no results (exit {code}); see the local log")
        return 1
    cases = []
    for case in ET.parse(junit).iter("testcase"):
        node = case.get("classname", "").replace(".", "/") + ".py::" + case.get("name", "")
        state, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            found = case.find(tag)
            if found is not None:
                state, message = tag, (found.get("message") or found.text or "").strip().splitlines()[:1]
                message = message[0] if message else ""
                break
        cases.append((node, state, message))
    count = {s: sum(1 for c in cases if c[1] == s) for s in ("passed", "failure", "error", "skipped")}
    unexpected = [c for c in cases if c[1] == "skipped" and c[0].split("::")[-1].split("[")[0] not in WINDOWS_ONLY]
    ok = code == 0 and not count["failure"] and not count["error"] and count["passed"] > 0 and not unexpected
    out.add("tests.import-suites", "PASS" if ok else "FAIL",
            f"{label}{count['passed']} passed, {count['failure']} failed, {count['error']} errors, "
            f"{count['skipped']} skipped ({len(unexpected)} not expected) in {seconds:.0f} s; exit "
            f"{'timeout after 4 h' if code is None else code}; files: {', '.join(Path(f).name for f in IMPORT_FILES)}")
    for node, state, message in cases:
        if state in ("failure", "error"):
            out.note(f"{state}: {node}: {message[:240]}")
    new = [c for c in cases if re.search(r"(^|_)f(29|30)_", c[0].split("::")[-1])]
    good = sum(1 for c in new if c[1] == "passed")
    out.add("tests.f29-f30", "PASS" if new and good == len(new) else ("NOT RUN" if not new else "FAIL"),
            f"{good} of {len(new)} non-finite-sample and broken-fragment tests passed "
            f"(real decoder child; the -k 'f29 or f30' set)")
    for node, state, message in cases:
        if state == "skipped":
            name = node.split("::")[-1].split("[")[0]
            instead = WINDOWS_ONLY.get(name, "NOT EXPECTED: nothing covers it")
            out.add("tests.skipped", "NOT RUN", f"{node}: {message[:160]} -> {instead}")
    return 0 if ok else 1


def _session() -> dict:
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}


def _kill_tree(pid: int) -> None:
    import psutil

    try:
        parent = psutil.Process(pid)
        for p in [*parent.children(recursive=True), parent]:
            try:
                p.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


# ----- memory -----
class _RusageInfoV0(ctypes.Structure):  # Apple sys/resource.h, read here independently of modwen's own copy
    _fields_ = [("uuid", ctypes.c_ubyte * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size", "resident_size",
        "phys_footprint", "proc_start_abstime", "proc_exit_abstime")]


_libproc = None


def held(pid: int) -> int:
    """Physical footprint on macOS (the number the product's watchdog reads), else RSS."""
    import psutil

    global _libproc
    used = psutil.Process(pid).memory_info().rss
    if MAC:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pid_rusage
            _libproc.argtypes, _libproc.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p], ctypes.c_int
        info = _RusageInfoV0()
        if _libproc(pid, 0, ctypes.byref(info)) == 0:
            used = max(used, info.phys_footprint)
    return used


MEASURE = "footprint" if MAC else "RSS"


# ----- the engine -----
class FakeLLM(BaseHTTPRequestHandler):
    def do_GET(self):
        self._send(200, {"object": "list", "data": [{"id": "qwen3-8b", "object": "model"}]})

    def do_POST(self):
        import httpx

        from tests.conftest import FakeLLM as Fake

        global _fake
        _fake = _fake or Fake()
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        reply = _fake.handler(httpx.Request("POST", "http://127.0.0.1" + self.path, content=body))
        self._send(reply.status_code, reply.content)

    def _send(self, status, data):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


_llm = _fake = None


def llm_port() -> int:
    global _llm
    if _llm is None:
        _llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=_llm.serve_forever, daemon=True).start()
    return _llm.server_port


def _cmd(p) -> list[str]:
    try:
        return p.cmdline()
    except Exception:
        return []


def _ppid(p):
    try:
        return p.ppid()
    except Exception:
        return None


class Engine:
    """A real ``python -m modwen serve`` on loopback with its own data folder, file keystore and fake STT."""

    def __init__(self, work: Path, tag: str, hostile: str | None = None):
        from modwen.config import Config, save_config

        self.work = Path(tempfile.mkdtemp(prefix=f"{tag}-", dir=work))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        cfg = Config()
        cfg.loaded_from = str(self.work / "config.yaml")
        cfg.app.open_browser = False
        cfg.app.check_updates = False
        cfg.app.setup_complete = True
        cfg.notify.desktop = False
        cfg.notify.sound = False
        cfg.stt.live.provider = "none"
        cfg.stt.final.provider = "fake"
        cfg.stt.vad = "energy"
        cfg.diarization.provider = "none"
        cfg.llm.managed.enabled = False
        cfg.llm.fallback = []
        cfg.llm.profiles["local_qwen"].base_url = f"http://127.0.0.1:{llm_port()}/v1"
        save_config(cfg, self.work / "config.yaml")
        temp = self.work / "ostemp"
        temp.mkdir()
        self.env = {**os.environ, "PYTHONPATH": str(ROOT), "MODWEN_DATA_DIR": str(self.work),
                    "MODWEN_CONFIG": str(self.work / "config.yaml"), "MODWEN_KEYSTORE": "file",
                    "MODWEN_TOKEN": TOKEN, "MODWEN_DESKTOP": "1", "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1", "TMPDIR": str(temp), "TEMP": str(temp), "TMP": str(temp)}
        serve = ["serve", "--port", str(self.port), "--no-tray", "--no-browser"]
        if hostile:
            self.cmd = [sys.executable, "-u", str(_hostile_wrapper(work)), hostile, *serve]
        else:
            self.cmd = [sys.executable, "-u", "-m", "modwen", *serve]
        self.log = open(self.work / "console.log", "ab", buffering=0)
        self.proc = None
        self.peak = 0
        self.guard_fired = False
        self._stop = threading.Event()
        threading.Thread(target=self._guard, daemon=True).start()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        self.proc = subprocess.Popen(self.cmd, cwd=self.work, env=self.env, stdout=self.log, stderr=self.log,
                                     **_session())
        end = time.monotonic() + 90
        while time.monotonic() < end:
            try:
                if self.api("GET", "/api/health", timeout=3)[0] == 200:
                    return self
            except Exception:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"engine exited {self.proc.returncode} while starting")
            time.sleep(.1)
        raise RuntimeError("engine did not answer /api/health within 90 s")

    def members(self):
        import psutil

        try:
            root = psutil.Process(self.proc.pid)
            return [root, *root.children(recursive=True)]
        except Exception:
            return []

    def real_engine_pid(self):
        real = [p for p in self.members() if "serve" in _cmd(p)]
        parents = {_ppid(p) for p in real}
        return next((p.pid for p in real if p.pid not in parents), None)

    def children(self):
        """Decoder processes: the real child, a hostile stand-in, and on Windows a venv launcher in front."""
        return [p for p in self.members() if any("modwen.audio.decoder_child" in c or "verify-peer-" in c
                                                 for c in _cmd(p))]

    def decoders(self):
        kids = self.children()
        parents = {_ppid(p) for p in kids}
        return [p for p in kids if p.pid not in parents]

    def launchers(self):
        """One per decoder for its whole life: what the engine itself started."""
        engine = self.real_engine_pid()
        return [p for p in self.children() if _ppid(p) == engine]

    def _guard(self):
        while not self._stop.wait(.005):
            if self.proc is None:
                continue
            total = 0
            for p in self.decoders():
                try:
                    total += held(p.pid)
                except Exception:
                    pass
            self.peak = max(self.peak, total)
            if total > GUARD_BYTES:
                self.guard_fired = True
                for p in self.children():
                    try:
                        p.kill()
                    except Exception:
                        pass

    def api(self, method, path, body=None, timeout=900, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        c.request(method, path, body=body, headers={"x-modwen-token": TOKEN,
                                                    "Content-Type": "application/octet-stream", **(headers or {})})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def post(self, file: Path, name=None, timeout=1800):
        name = name or file.name
        query = urllib.parse.urlencode({"filename": name, "title": name.rsplit(".", 1)[0]})
        with open(file, "rb") as f:
            return self.api("POST", "/api/meetings/import?" + query, f, timeout,
                            {"Content-Length": str(file.stat().st_size)})

    def health(self):
        began = time.monotonic()
        try:
            status = self.api("GET", "/api/health", timeout=10)[0]
        except Exception:
            status = "no answer"
        return status, time.monotonic() - began

    def rows(self):
        with sqlite3.connect(self.work / "modwen.db") as db:
            return db.execute("select id,title,status,error,duration_ms from meetings").fetchall()

    def sealed(self):
        return list(self.work.glob("import-*.sealed"))

    def clean_state(self):
        return CleanState(sealed=len(self.sealed()), decoders=len(self.children()),
                          importing=sum(r[2] == "importing" for r in self.rows()))

    def wait(self, mid, seconds=180):
        end = time.monotonic() + seconds
        meeting = None
        while time.monotonic() < end:
            meeting = self.api("GET", "/api/meetings/" + mid)[1]
            if meeting["meeting"]["status"] in ("ready", "failed"):
                break
            time.sleep(.25)
        return meeting

    def audio(self, mid):
        """The stored samples, decrypted with this engine's test keystore."""
        target = self.work / f"{mid}.npy"
        code = ("import sqlite3,sys,numpy as np;from pathlib import Path;from modwen.config import load_config;"
                "from modwen.crypto import KeyStore;from modwen.audio.chunks import read_channel;"
                "w=Path(sys.argv[1]);m=sys.argv[2];k=KeyStore(load_config());"
                "key=sqlite3.connect(w/'modwen.db').execute('select wrapped_key from meetings where id=?',(m,)).fetchone()[0];"
                "np.save(w/(m+'.npy'),read_channel(w/'audio'/m,'system',k.unwrap(key)))")
        subprocess.run([sys.executable, "-c", code, str(self.work), mid], env=self.env, cwd=self.work, check=True, capture_output=True)
        return __import__("numpy").load(target)

    def console(self) -> str:
        return (self.work / "console.log").read_bytes().decode(errors="replace")

    def stop_reasons(self) -> list[str]:
        return re.findall(r"Decoder stopped: ([a-z -]+?) \(", self.console())

    def kill(self):
        """SIGKILL (or TerminateProcess) the real engine, as a crash would."""
        import psutil

        pid = self.real_engine_pid()
        if pid:
            psutil.Process(pid).kill()
        self.proc.wait(timeout=15)

    def stop(self):
        self._stop.set()
        if self.proc is not None and self.proc.poll() is None:
            for p in reversed(self.members()):
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        if not self.log.closed:
            self.log.close()


class CleanState(dict):
    """What an import may leave behind: all three must be 0 once it has finished."""

    def __str__(self):
        return "left: " + ", ".join(f"{k} {v}" for k, v in self.items())


class Health:
    """Polls /api/health every 0.25 s while an import runs."""

    def __init__(self, engine):
        self.engine, self.worst, self.failed, self._stop = engine, 0.0, 0, threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.wait(.25):
            status, seconds = self.engine.health()
            self.worst = max(self.worst, seconds)
            self.failed += status != 200

    def done(self) -> str:
        self._stop.set()
        self.thread.join(15)
        return f"health max {self.worst:.2f} s, failures {self.failed}"


HOSTILE = {
    # CPU-bound with no frames and no heartbeat: only the silence timer, or the CPU limit, can stop it
    "spin": "def dec(sealed, writer, beat):\n    x = 0\n    while True:\n        x += 1\n",
    # memory as fast as numpy can write it, 64 MiB at a time up to 3 GiB: the watchdog's race at its worst
    "alloc": "import numpy as np\ndef dec(sealed, writer, beat):\n    keep = []\n    for _ in range(48):\n"
             "        keep.append(np.ones(8 * 1024 * 1024))\n    return 0\n",
}


def _write_peer(gen: Path, kind: str, body: str) -> None:
    (gen / f"verify-peer-{kind}.py").write_text(
        f"# verify-peer-{kind}\nfrom modwen.audio import importer\n" + body + "importer.decode_media_into = dec\n"
        "import runpy\nrunpy.run_module('modwen.audio.decoder_child', run_name='__main__')\n")


def _hostile_wrapper(work: Path) -> Path:
    """An engine whose decoder child is the real ``decoder_child`` (handshake, limits, timers, and on macOS the
    sandbox) with only its decode step replaced. Kind ``cpu`` is ``spin`` under a 10 s CPU limit instead of the real
    3,600 s. The peer goes in as ``-c`` source, read when the child starts: the sandbox lets it read no folder of ours."""
    gen = work / "gen"
    gen.mkdir(exist_ok=True)
    for kind, body in HOSTILE.items():
        _write_peer(gen, kind, body)
    path = gen / "verify-engine.py"
    path.write_text(
        "import os, runpy, sys\n"
        "kind = sys.argv[1]\nsys.argv = ['modwen'] + sys.argv[2:]\n"
        "peer = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'verify-peer-%s.py' % "
        "('spin' if kind == 'cpu' else kind))\n"
        "from modwen.audio import decoder\n"
        "decoder.child_command = lambda: [sys.executable, '-P', '-c', open(peer).read()]\n"
        "if kind == 'cpu':\n    decoder.CPU_SECONDS = 10\n"
        "runpy.run_module('modwen', run_name='__main__', alter_sys=True)\n")
    return path


# ----- media, all generated here -----
def tone_file(path: Path, seconds: int, codec: str, fmt: str, layout: str = "mono", rate: int = 48000) -> Path:
    """A 440 Hz tone, 4 s on and 2 s off so the energy VAD finds 'speech'."""
    import av
    import numpy as np

    if path.exists():
        return path
    part = path.with_suffix(".part")
    with av.open(str(part), "w", format=fmt) as out:
        stream = out.add_stream(codec, rate=rate, layout=layout)
        channels = stream.layout.nb_channels
        pts, n = 0, 4800
        while pts < seconds * rate:
            t = np.arange(pts, pts + n) / rate
            block = (0.2 * np.sin(2 * np.pi * 440 * t) * (t % 6 < 4)).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(np.tile(block, (channels, 1)), format="fltp", layout=stream.layout.name)
            frame.sample_rate, frame.pts = rate, pts
            for packet in stream.encode(frame):
                out.mux(packet)
            pts += n
        for packet in stream.encode(None):
            out.mux(packet)
    part.rename(path)
    return path


def seven_one(codec: str, fmt: str) -> bytes:
    import av

    from tests.test_import_media import _encode_audio

    out = io.BytesIO()
    with av.open(out, "w", format=fmt) as container:
        stream = container.add_stream(codec, rate=48000, layout="7.1")
        for packet in _encode_audio(container, stream, 440, 2):
            container.mux(packet)
    return out.getvalue()


def dominant_hz(samples) -> float:
    import numpy as np

    spectrum = np.abs(np.fft.rfft(samples[:16000 * 2]))
    return float(np.argmax(spectrum[1:]) + 1) * 16000 / min(len(samples), 32000)


def show(result) -> str:
    from modwen.audio import importer as I

    short = {I.NOT_SUPPORTED: "NOT_SUPPORTED", I.DAMAGED: "DAMAGED", I.NO_AUDIO: "NO_AUDIO", I.EMPTY: "EMPTY",
             I.IMPORT_FAILED: "IMPORT_FAILED", I.TOO_BIG: "TOO_BIG"}
    status, body = result
    if status == 200 and isinstance(body, dict):
        return f"200 {body.get('duration_ms', 0) / 1000:.2f}s"
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return f"{status} {short.get(detail, str(detail)[:60])}"


def seconds_of(result) -> float | None:
    status, body = result
    return body.get("duration_ms", 0) / 1000 if status == 200 and isinstance(body, dict) else None


# ----- engine checks -----
class Checks:
    def __init__(self, work: Path, out: Section):
        self.work, self.out = work, out
        self.gen = work / "media"
        self.gen.mkdir(parents=True, exist_ok=True)
        self.runs = work / "engines"
        self.runs.mkdir(exist_ok=True)

    def file(self, name: str, data: bytes) -> Path:
        path = self.gen / name
        path.write_bytes(data)
        return path

    def short(self) -> Path:
        from tests.test_import_media import make_media
        return self.file("intact-2s.mp4", make_media(self.gen, seconds=2))

    def long_aac(self) -> Path:
        return tone_file(self.gen / "aac-30min.m4a", 1800, "aac", "mp4")

    def long_pcm(self) -> Path:
        return tone_file(self.gen / "pcm-10min.mov", 600, "pcm_s16le", "mov")

    def engine(self, tag, hostile=None) -> Engine:
        return Engine(self.runs, tag, hostile)

    # -- basics --
    def basic_import(self):
        """An ordinary file through the whole pipeline: import, fake STT, notes, ready."""
        from tests.test_import_media import make_media

        path = self.file("speech-12s.mp4", make_media(self.gen, seconds=12))
        with self.engine("basic") as e:
            r = e.post(path)
            meeting = e.wait(r[1]["id"]) if r[0] == 200 else None
            status = meeting and meeting["meeting"]["status"]
            hz = dominant_hz(e.audio(r[1]["id"])) if r[0] == 200 else 0
            ok = r[0] == 200 and abs(seconds_of(r) - 12) < 0.1 and status == "ready" and abs(hz - 440) < 5
            self.out.add("engine.basic-import", "PASS" if ok else "FAIL",
                         f"12 s AAC MP4: {show(r)}, meeting {status}, stored tone {hz:.0f} Hz, {e.clean_state()}")

    def mac_apis(self):
        """The two native hooks the macOS limits depend on, read directly."""
        from modwen.audio import decoder as D, decoder_child as C

        found = C._set_max_alloc()
        try:
            mine = D.footprint(os.getpid())
            ok = found and mine > 0
            detail = f"av_max_alloc reached in PyAV's libavutil: {found}; footprint() of this process {mine / MIB:.0f} MiB"
        except Exception as exc:
            ok, detail = False, f"av_max_alloc {found}; footprint() raised {type(exc).__name__}: {exc}"
        self.out.add("limits.native-hooks", "PASS" if ok else "FAIL", detail)

    # -- 7.1 audio, hostile MP4 box shapes, unusual sample tags, opaque metadata, protected audio --
    def seven_point_one(self):
        with self.engine("f14") as e:
            for codec, fmt in (("aac", "mp4"), ("pcm_s16le", "mov"), ("flac", "mp4")):
                path = self.file(f"f14-{codec}-71.{fmt}", seven_one(codec, fmt))
                r = e.post(path)
                hz = dominant_hz(e.audio(r[1]["id"])) if r[0] == 200 else 0
                ok = r[0] == 200 and abs(seconds_of(r) - 2) < 0.05 and abs(hz - 440) < 5
                self.out.add(f"f14.7.1-{codec}", "PASS" if ok else "FAIL",
                             f"{show(r)}, stored tone {hz:.0f} Hz, {e.clean_state()}")

    # Each hostile box shape's outcome on Windows through the real API; the Mac must match it
    ATTACK_EXPECTED = {"moof_2000": "200 2.00s", "moof_20000": "400 NOT_SUPPORTED", "moof_100000": "400 NOT_SUPPORTED",
                       "co64_first": "400 DAMAGED", "co64_last": "400 DAMAGED", "stz2": "400 DAMAGED",
                       "sync_traf": "400 NO_AUDIO", "sync_moof": "400 NO_AUDIO", "iinf": "400 NOT_SUPPORTED",
                       "iloc": "400 NOT_SUPPORTED", "hdlr_meta": "400 NO_AUDIO", "hdlr_short": "400 NO_AUDIO"}

    def f15_to_f20(self):
        from tests.test_import_isolation import ATTACKS, attacks
        from tests.test_import_media import make_media

        base = make_media(self.gen, seconds=2)
        good = self.short()
        with self.engine("f15") as e:
            for name in ATTACKS:
                path = self.file(f"attack-{name}.mp4", attacks(base, name))
                e.peak, e.guard_fired = 0, False
                began = time.monotonic()
                r = e.post(path)
                spent = time.monotonic() - began
                after = e.post(good, "after.mp4")
                expected = self.ATTACK_EXPECTED[name]
                ok = (show(r) == expected and not e.guard_fired and e.health()[0] == 200 and
                      seconds_of(after) is not None and not any(e.clean_state().values()))
                self.out.add(f"f15-f20.{name}", "PASS" if ok else "FAIL",
                             f"{show(r)} (Windows {expected}) in {spent:.1f} s, peak child {MEASURE} "
                             f"{e.peak / MIB:.0f} MiB, outer guard fired {e.guard_fired}, next import {show(after)}, "
                             f"{e.clean_state()}")
            reasons = e.stop_reasons()
            self.out.note(f"decoder stop reasons logged for the hostile box-shape files: {reasons or 'none'}")

    def f21_to_f23(self):
        from tests.test_import_preflight import FULL, pcm_file
        from tests.test_import_media import make_media
        from tests.test_import_safety import box, rewrite

        cases = []
        for tag, codec, old in ((b"NONE", "pcm_s16be", b"twos"), (b"ms\0\x01", "pcm_s16le", b"sowt")):
            body = pcm_file(self.gen, "mov", codec, 170).replace(old, tag, 1)
            cases.append((f"f21.pcm-tag-{tag[:2].decode()}", self.file(f"f21-{codec}.mov", body), "200 170.00s"))
        opaque = box(b"stsz", FULL + struct.pack(">II", 8, 1 << 26))
        body = rewrite(make_media(self.gen, seconds=2), b"trak", lambda t: t + box(b"udta", box(b"name", opaque)))
        cases.append(("f22.opaque-udta", self.file("f22.mp4", body), "200 2.00s"))
        body = rewrite(make_media(self.gen, seconds=2), b"stsd", lambda t: t.replace(b"mp4a", b"aavd"))
        cases.append(("f23.protected-aavd", self.file("f23.mp4", body), "400 NOT_SUPPORTED"))
        with self.engine("f21") as e:
            for check, path, expected in cases:
                r = e.post(path)
                self.out.add(check, "PASS" if show(r) == expected else "FAIL",
                             f"{show(r)} (expected {expected}), {e.clean_state()}")

    # -- limits --
    def memory_breach_fast(self):
        """A child that writes memory as fast as it can: the watchdog polls every 20 ms, so this measures overshoot."""
        with self.engine("alloc", hostile="alloc") as e:
            r = e.post(self.short(), "alloc.mp4")
            reasons = e.stop_reasons()
            alive, state = e.health()[0], e.clean_state()
            over = e.peak - 1024 * MIB
            if MAC:
                ok = show(r) == "400 NOT_SUPPORTED" and "memory limit" in reasons and not e.guard_fired
            else:  # Windows: the job refuses the allocation, so the child dies on its own
                ok = show(r) == "400 NOT_SUPPORTED" and not e.guard_fired and e.peak < 1100 * MIB
            self.out.add("limits.memory-breach-fast", "PASS" if ok else "FAIL",
                         f"{show(r)}, stop reason {reasons or 'none logged'}, peak child {MEASURE} {e.peak / MIB:.0f} MiB "
                         f"(limit 1024 MiB, overshoot {over / MIB:+.0f} MiB), outer guard at {GUARD_BYTES // MIB} MiB "
                         f"fired {e.guard_fired}, health {alive}, {state}")

    def cpu_limit(self):
        """The real CPU limit, shortened to 10 s by the wrapper: RLIMIT_CPU on macOS, the job's CPU time on Windows."""
        with self.engine("cpu", hostile="cpu") as e:
            cpu = {}
            began = time.monotonic()
            with cf.ThreadPoolExecutor(1) as pool:
                task = pool.submit(e.post, self.short(), "cpu.mp4")
                while not task.done():
                    for d in e.decoders():
                        try:
                            cpu[d.pid] = sum(d.cpu_times()[:2])
                        except Exception:
                            pass
                    time.sleep(.1)
                r = task.result()
            spent = time.monotonic() - began
            most = max(cpu.values(), default=0)
            ok = show(r) == "400 NOT_SUPPORTED" and spent < 60 and most >= 8 and not any(e.clean_state().values())
            self.out.add("limits.cpu", "PASS" if ok else "FAIL",
                         f"spinning child under a 10 s CPU limit: {show(r)} after {spent:.1f} s, child CPU seen "
                         f"{most:.1f} s, stop reason {e.stop_reasons() or 'none (the OS ended it)'}, {e.clean_state()}")

    ESCAPES = ("fork", "exec", "connect to the engine's port", "write in the data folder", "read the database",
               "read the key file")
    ESCAPE = """import os, socket, subprocess
real = importer.decode_media_into
def fork():
    if os.fork() == 0:
        os._exit(0)
TRIES = (fork, lambda: subprocess.run(['/bin/sh', '-c', 'exit 0']),
         lambda: socket.create_connection(('127.0.0.1', {port}), timeout=5).close(),
         lambda: open({mark!r}, 'w').write('x'), lambda: open({db!r}, 'rb').read(1),
         lambda: open({secrets!r}, 'rb').read(1))
def dec(sealed, writer, beat):
    got = 0
    for bit, attempt in enumerate(TRIES):
        try:
            attempt()
        except OSError:
            continue
        got |= 1 << bit
    if got:
        os._exit(64 + got)  # no answer: the engine logs this exit code
    return real(sealed, writer, beat)
"""

    def escape(self):
        """macOS: a child that tries to get out (the RLIMIT_NPROC and the sandbox) and then decodes an intact file."""
        if not MAC:
            self.out.add("limits.descendants", "NOT RUN", "macOS only; on Windows the job's process limit holds them")
            return
        e = self.engine("escape", hostile="escape")
        mark = e.work / "escaped.txt"
        _write_peer(self.runs / "gen", "escape", self.ESCAPE.format(
            port=e.port, mark=str(mark), db=str(e.work / "modwen.db"), secrets=str(e.work / ".secrets.json")))
        with e:
            r = e.post(self.short(), "escape.mp4")
            codes = [int(c, 16) - 64 for c in re.findall(r"\(exit (0x[0-9a-f]+)\)", e.console())]
            got = {name for bit, name in enumerate(self.ESCAPES) for code in codes if 0 < code < 64 and code >> bit & 1}
            state, wrote = e.clean_state(), mark.exists()
        imported = show(r) == "200 2.00s"
        for check, names in (("limits.descendants", self.ESCAPES[:2]), ("limits.sandbox", self.ESCAPES[2:])):
            done = sorted((got | ({"write in the data folder"} if wrote else set())) & set(names))
            ok = imported and not done and not any(state.values())
            self.out.add(check, "PASS" if ok else "FAIL",
                         f"decoder child tried: {', '.join(names)}; managed: {', '.join(done) or 'none'}; then the "
                         f"intact file: {show(r)}, {state}")

    # -- timers --
    def busy_cores(self):
        """An intact file imports with every core busy at normal priority, under the real 180 s silence timer."""
        import psutil

        python = getattr(sys, "_base_executable", sys.executable)
        with self.engine("busy") as e:
            hogs = [subprocess.Popen([python, "-c", "while True: pass"]) for _ in range(psutil.cpu_count())]
            try:
                time.sleep(1)
                h = Health(e)
                began = time.monotonic()
                r = e.post(self.short(), "busy.mp4")
                spent = time.monotonic() - began
                health = h.done()
            finally:
                for hog in hogs:
                    hog.kill()
                    hog.wait()
            ok = show(r) == "200 2.00s"
            self.out.add("timers.busy-cores", "PASS" if ok else "FAIL",
                         f"{len(hogs)} busy processes: {show(r)} in {spent:.1f} s, {health}, {e.clean_state()}")

    def priority_and_concurrency(self):
        """Four long imports at once: at most two decoders, the rest wait; the child runs at the engine's priority."""
        long = self.long_pcm()
        with self.engine("conc") as e:
            h = Health(e)
            most, seen = 0, {}
            with cf.ThreadPoolExecutor(4) as pool:
                tasks = [pool.submit(e.post, long, f"c{i}.mov") for i in range(4)]
                while not all(t.done() for t in tasks):
                    most = max(most, len(e.launchers()))
                    engine = e.real_engine_pid()
                    for d in e.decoders():
                        try:
                            seen[d.pid] = (int(d.nice()), int(__import__("psutil").Process(engine).nice()))
                        except Exception:
                            pass
                    time.sleep(.05)
                results = [show(t.result()) for t in tasks]
            health = h.done()
            ok = all(x == "200 600.00s" for x in results) and most == 2 and not any(e.clean_state().values())
            self.out.add("engine.two-at-once", "PASS" if ok else "FAIL",
                         f"four 10-min PCM imports: {results}, most decoders at once {most} (limit 2), {health}, "
                         f"{e.clean_state()}")
            same = bool(seen) and all(child == parent for child, parent in seen.values())
            self.out.add("timers.priority", "PASS" if same else "FAIL",
                         f"decoder vs engine priority (nice) seen {sorted(set(seen.values())) or 'none'}")

    def frozen_child(self):
        """Frozen by the OS mid-import (SIGSTOP): the real 180 s timer must kill it; another import runs meanwhile."""
        with self.engine("frozen") as e:
            with cf.ThreadPoolExecutor(1) as pool:
                task = pool.submit(e.post, self.long_aac(), "frozen.m4a", 3600)
                end = time.monotonic() + 300
                while time.monotonic() < end and not (list(e.work.glob("audio/*/*.enc")) and e.decoders()):
                    time.sleep(.01)
                frozen = e.decoders()
                for p in frozen:
                    p.suspend()
                began = time.monotonic()
                h = Health(e)
                alongside = e.post(self.short(), "alongside.mp4")
                r = task.result(timeout=3600)
                spent = time.monotonic() - began
                health = h.done()
            time.sleep(1)
            left = [p.pid for p in frozen if p.is_running() and p.status() != "zombie"]
            ok = (bool(frozen) and show(r) == "400 NOT_SUPPORTED" and 170 < spent < 260 and
                  show(alongside) == "200 2.00s" and not left and not any(e.clean_state().values()))
            self.out.add("timers.frozen-child", "PASS" if ok else "FAIL",
                         f"{show(r)} {spent:.0f} s after the freeze (timer 180 s), stop reason "
                         f"{e.stop_reasons() or 'none'}, import alongside {show(alongside)}, frozen left {len(left)}, "
                         f"{health}, {e.clean_state()}")

    def spinning_child(self):
        """A child that spins with no frames: the real 180 s silence timer must stop it."""
        with self.engine("spin", hostile="spin") as e:
            h = Health(e)
            began = time.monotonic()
            r = e.post(self.short(), "spin.mp4", 3600)
            spent = time.monotonic() - began
            reasons = e.stop_reasons()
            ok = show(r) == "400 NOT_SUPPORTED" and "no frames" in reasons and 170 < spent < 260
            self.out.add("timers.spinning-child", "PASS" if ok else "FAIL",
                         f"{show(r)} after {spent:.0f} s (timer 180 s), stop reason {reasons or 'none'}, {h.done()}, "
                         f"{e.clean_state()}")

    # -- crash and recovery --
    def engine_kill(self):
        """The engine killed mid-decode: the child must die with it, and a restart must clean up and say so."""
        from modwen.services import IMPORT_INTERRUPTED

        e = self.engine("kill")
        try:
            e.start()
            with cf.ThreadPoolExecutor(1) as pool:
                pool.submit(e.post, self.long_aac(), "killed.m4a", 3600)
                end = time.monotonic() + 300
                while time.monotonic() < end and not (list(e.work.glob("audio/*/*.enc")) and e.decoders()):
                    time.sleep(.01)
                kids = e.children()
                sealed_before = len(e.sealed())
                e.kill()
                end = time.monotonic() + 10
                while time.monotonic() < end and any(p.is_running() and p.status() != "zombie" for p in kids):
                    time.sleep(.05)
                left = [p.pid for p in kids if p.is_running() and p.status() != "zombie"]
            e.proc = None
            e.start()
            rows = e.rows()
            failed = [r for r in rows if r[1] == "killed.m4a"]
            after = e.post(self.short(), "after-restart.mp4")
            ok = (bool(kids) and sealed_before == 1 and not left and len(failed) == 1 and failed[0][2] == "failed" and
                  failed[0][3] == IMPORT_INTERRUPTED and not e.sealed() and seconds_of(after) is not None)
            self.out.add("engine.kill-and-restart", "PASS" if ok else "FAIL",
                         f"decoders before kill {len(kids)}, left alive 10 s after {len(left)}; sealed upload before "
                         f"{sealed_before}, after restart {len(e.sealed())}; record "
                         f"{(failed[0][2], 'interrupted' if failed[0][3] == IMPORT_INTERRUPTED else failed[0][3]) if failed else 'missing'}; "
                         f"import after restart {show(after)}")
        finally:
            e.stop()

    # -- valid media --
    def valid_media(self):
        from tests.test_import_isolation import FRAG, edit_list
        from tests.test_import_media import FRAGMENTED, make_media

        frag = [("frag", "mp4", FRAGMENTED, False), ("frag-video", "mp4", FRAGMENTED, True),
                ("frag_keyframe", "mp4", FRAG, False), ("cmaf", "mp4", "cmaf", False), ("dash", "mp4", "dash", False),
                ("global_sidx", "mp4", "dash+global_sidx", False),
                ("separate_moof-video", "mp4", FRAG + "+separate_moof", True),
                ("delay_moov", "mp4", "delay_moov+frag_keyframe", False),
                ("omit_tfhd_offset", "mp4", FRAG + "+omit_tfhd_offset", False), ("frag-mov", "mov", FRAG, False),
                ("ismv", "ismv", None, False), ("ismv-video", "ismv", None, True)]
        edits = [("whole", [(20, 0)], 20), ("trim-end", [(15, 0)], 15), ("skip-start", [(15, 5)], 15),
                 ("delay", [(2, -1), (20, 0)], 20), ("drop-middle", [(5, 0), (5, 10)], 10), ("past-end", [(30, 0)], 20),
                 ("three", [(3, 0), (3, 5), (3, 10)], 9), ("tiny-trim", [(19.95, 0)], 19.95),
                 ("five", [(2, i * 2) for i in range(5)], 10), ("forty", [(.2, i) for i in range(40)], 4)]
        with self.engine("valid") as e:
            for name, fmt, flags, video in frag:
                path = self.file(f"valid-{name}.{fmt}", make_media(self.gen, seconds=6, fmt=fmt, movflags=flags,
                                                                     video=video))
                r = e.post(path)
                ok = seconds_of(r) is not None and abs(seconds_of(r) - 6) < 0.1
                self.out.add(f"valid.fragmented-{name}", "PASS" if ok else "FAIL", f"{show(r)} (expected 6 s)")
            data = make_media(self.gen, seconds=20)
            at = data.index(b"mvhd")
            scale = int.from_bytes(data[at + 16:at + 20], "big")
            for name, entries, seconds in edits:
                body = edit_list(data, [(int(span * scale), -1 if start < 0 else int(start * 48000))
                                        for span, start in entries])
                r = e.post(self.file(f"valid-edit-{name}.mp4", body))
                ok = seconds_of(r) is not None and abs(seconds_of(r) - seconds) < 0.3
                self.out.add(f"valid.edit-list-{name}", "PASS" if ok else "FAIL", f"{show(r)} (expected {seconds} s)")
            self.out.note(f"after the valid-media imports: {e.clean_state()}")

    def leftovers(self):
        """After everything: no decoder process from this run anywhere, and no sealed upload in any engine folder."""
        import psutil

        alive = []
        for p in psutil.process_iter(["cmdline"]):
            line = " ".join(p.info.get("cmdline") or [])
            if "verify-peer-" in line or ("modwen.audio.decoder_child" in line and _child_of_run(p, self.runs)):
                alive.append(p.pid)
        sealed = list(self.runs.glob("*/import-*.sealed"))
        self.out.add("engine.no-leftovers", "PASS" if not alive and not sealed else "FAIL",
                     f"decoder processes left {len(alive)}, sealed uploads left {len(sealed)}")

    ORDER = ["mac_apis", "basic_import", "seven_point_one", "f15_to_f20", "f21_to_f23", "memory_breach_fast",
             "cpu_limit", "escape", "busy_cores", "priority_and_concurrency", "engine_kill", "valid_media", "frozen_child",
             "spinning_child", "leftovers"]


def _child_of_run(p, runs: Path) -> bool:
    try:
        return str(runs) in (p.environ().get("MODWEN_DATA_DIR", "") + p.cwd())
    except Exception:
        return True  # can't tell: count it, so nothing is missed


def cmd_engine(args) -> int:
    out = Section(Path(args.out), args.py)
    work = Path(args.work).resolve() / f"engine-{args.py}"
    work.mkdir(parents=True, exist_ok=True)
    checks = Checks(work, out)
    only = [x for x in (args.only or "").split(",") if x]
    failed = False
    for name in Checks.ORDER:
        if only and name not in only:
            continue
        began = time.monotonic()
        try:
            getattr(checks, name)()
        except Exception as exc:
            failed = True
            out.add(f"check.{name}", "FAIL", f"the check itself broke: {type(exc).__name__}: {exc}"[:300])
        print(f"  ({name}: {time.monotonic() - began:.0f} s)", flush=True)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["env", "pytest", "engine"])
    parser.add_argument("--py", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", default=str(ROOT / ".tmp" / "macos-verify"))
    parser.add_argument("--only", default="")
    argv = sys.argv[1:]
    cut = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:cut])
    args.extra = argv[cut + 1:]  # after "--": passed to pytest as they are
    return {"env": cmd_env, "pytest": cmd_pytest, "engine": cmd_engine}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
