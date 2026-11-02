"""Decode MP4, MOV and M4A in a limited child process, so a hostile file can only cost that child.

A small file can make FFmpeg allocate gigabytes or crash. This parent starts one ``modwen.audio.decoder_child`` per
import, limits it, gives it only that upload's sealed key, and writes the audio it sends into the meeting's encrypted
chunks. The child reads the sealed file itself, so moov-last files can seek and no cleartext reaches disk. Everything
it sends is untrusted.

Limits: on Windows a Job Object holds memory, CPU and process count in the kernel. On macOS nothing usable does
(RLIMIT_AS and RLIMIT_DATA are refused at these sizes), so memory is a watchdog here plus FFmpeg's own single-allocation
ceiling: a race, not a hard limit. Wall-clock and silence timers run here on both.

A child that fails after it started means the file was the trouble, as far as anyone can tell: not supported, whose
"a format Modwen can't read" stays true. A child that can't be started or limited means the file wasn't tried, so that
is the general failure. This module must never fall back to decoding in the engine.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np

import modwen
from modwen.audio import SAMPLE_RATE
from modwen.audio.decoder_child import (HEAD, JOB_LIMIT_INFORMATION, MAX_FRAMES, REFUSAL, JobLimits)
from modwen.audio.importer import (LENGTH_SLACK_SECONDS, MAX_IMPORT_HOURS, NOT_SUPPORTED, REFUSALS, TOO_LONG_CODE,
                                   ImportRefused, TooLong)

log = logging.getLogger("modwen.import")

MEMORY_LIMIT = 1024 ** 3  # Windows job commitment (not RSS); macOS sampled footprint, with possible overshoot
CPU_SECONDS = 3600
WALL_SECONDS = 5400  # a 6-hour file decoded at 50 to 75 times real time on the test PC; a slow machine gets margin
SILENCE_SECONDS = 180  # with the parent waiting on the child, so a slow disk here doesn't count
MEMORY_POLL_SECONDS = 0.02
TICK_SECONDS = 0.25
MAX_CHILDREN = 2
STOP_SECONDS = 10
# Enough for Python, FFmpeg and temp folders. Above all, not MODWEN_TOKEN.
KEPT_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "PYTHONPATH",
            "PYTHONPYCACHEPREFIX", "VIRTUAL_ENV")

_slots = threading.BoundedSemaphore(MAX_CHILDREN)


class BadChild(Exception):
    """The child broke the protocol. It decodes hostile input, so it is treated as hostile itself."""


class WindowsJob:
    """One Job Object, holding one process. Closing the last handle kills it, so a crashed engine leaves no orphan."""

    JOB_TIME, ACTIVE_PROCESS, JOB_MEMORY, DIE_ON_UNHANDLED_EXCEPTION, KILL_ON_CLOSE = 0x4, 0x8, 0x200, 0x400, 0x2000
    PROCESS_ACCESS = 0x0001 | 0x0100 | 0x1000 | 0x00100000  # terminate, set quota, query, synchronize

    def __init__(self, memory: int, cpu_seconds: int):
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = ctypes.c_void_p
        k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = handle, [handle, ctypes.c_wchar_p]
        k.SetInformationJobObject.argtypes = [handle, ctypes.c_int, handle, ctypes.c_ulong]
        k.AssignProcessToJobObject.argtypes = [handle, handle]
        k.OpenProcess.restype, k.OpenProcess.argtypes = handle, [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.TerminateJobObject.argtypes = [handle, ctypes.c_uint]
        k.WaitForSingleObject.argtypes = [handle, ctypes.c_ulong]
        k.CloseHandle.argtypes = [handle]
        k.QueryInformationJobObject.argtypes = [handle, ctypes.c_int, handle, ctypes.c_ulong, handle]
        k.IsProcessInJob.argtypes = [handle, handle, ctypes.POINTER(ctypes.c_int)]
        self.process = None
        self.name = "Local\\ModwenDecoder-" + uuid.uuid4().hex
        self.job = k.CreateJobObjectW(None, self.name)
        if not self.job:
            raise OSError("no job object")
        limits = JobLimits()
        limits.job_memory = memory
        limits.basic.job_time = int(cpu_seconds * 10_000_000)
        limits.basic.active_processes = 1
        limits.basic.flags = (self.JOB_TIME | self.ACTIVE_PROCESS | self.JOB_MEMORY | self.DIE_ON_UNHANDLED_EXCEPTION |
                              self.KILL_ON_CLOSE)
        if not k.SetInformationJobObject(self.job, JOB_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("job limits refused")
        self.limits = limits

    def adopt(self, pid: int) -> None:
        self.process = self.k.OpenProcess(self.PROCESS_ACCESS, False, pid)
        if not self.process:
            raise OSError("cannot open decoder process")
        self.confirm()

    def confirm(self) -> None:
        limits, held = JobLimits(), ctypes.c_int()
        if (not self.k.QueryInformationJobObject(self.job, JOB_LIMIT_INFORMATION, ctypes.byref(limits),
                                                ctypes.sizeof(limits), None) or
                limits.basic.flags != self.limits.basic.flags or limits.job_memory != self.limits.job_memory or
                limits.basic.job_time != self.limits.basic.job_time or
                limits.basic.active_processes != self.limits.basic.active_processes or
                not self.k.IsProcessInJob(self.process, self.job, ctypes.byref(held)) or not held.value):
            raise OSError("job limits not confirmed")

    def terminate(self) -> None:
        self.k.TerminateJobObject(self.job, 1)

    def wait(self, seconds: float) -> None:
        if self.process:  # the launcher's exit isn't enough: the sealed file can't be deleted while this one reads it
            self.k.WaitForSingleObject(self.process, int(seconds * 1000))

    def close(self) -> None:
        if self.process:
            self.k.CloseHandle(self.process)
            self.process = None
        if self.job:
            self.k.CloseHandle(self.job)
            self.job = None


class RusageInfoV0(ctypes.Structure):
    """Apple sys/resource.h: 16-byte UUID followed by ten uint64_t fields (96 bytes)."""
    _fields_ = [("uuid", ctypes.c_ubyte * 16)] + [(name, ctypes.c_uint64) for name in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size", "resident_size",
        "phys_footprint", "proc_start_abstime", "proc_exit_abstime")]


def footprint(pid: int) -> int:
    """What the child holds, or an exception if that can't be read: the watchdog then stops it."""
    import psutil

    used = psutil.Process(pid).memory_info().rss
    if sys.platform == "darwin":
        info = RusageInfoV0()
        proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pid_rusage
        proc.argtypes, proc.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p], ctypes.c_int
        if proc(pid, 0, ctypes.byref(info)) != 0:
            raise OSError("no footprint")
        used = max(used, info.phys_footprint)
    return used


def child_command() -> list[str]:
    return [sys.executable, "-P", "-m", "modwen.audio.decoder_child"]  # -P: nothing in the working folder is imported


def child_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in KEPT_ENV if name in os.environ}
    # This same source, however the engine found it. -m would otherwise take whatever the venv has installed.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(modwen.__file__).resolve().parent.parent),
                                                      env.get("PYTHONPATH")]))
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
    return env


class Child:
    def __init__(self):
        self.process = self.job = self.pid = self.reason = None
        self.began = self.waiting = time.monotonic()
        self.done = threading.Event()
        self.watcher = threading.Thread(target=self.watch, name="decoder-watchdog", daemon=True)

    def start(self) -> None:
        """Start it and have it waiting, limited. Raises RuntimeError, as nothing has touched the file."""
        windows = sys.platform == "win32"
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if windows else 0
        try:
            self.job = WindowsJob(MEMORY_LIMIT, CPU_SECONDS) if windows else None
            env = child_env()
            if self.job:
                # Child joins before imports and closes its handle. If the parent has already died, opening the job
                # fails and it exits. This also avoids leaving a suspended process across a spawn/assignment race.
                env["MODWEN_DECODER_JOB"] = self.job.name
            self.process = subprocess.Popen(child_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0, env=env, creationflags=flags,
                                            close_fds=True)
            self.watcher.start()
            frame = self.read()
            if frame is None or frame[0] != b"H" or len(frame[1]) != 4:
                raise RuntimeError("the decoder did not start")
            self.pid = struct.unpack("<I", frame[1])[0]
            self.check_identity()
            if self.job:
                self.job.adopt(self.pid)
            self.process.stdin.write(json.dumps({"memory": MEMORY_LIMIT, "cpu": CPU_SECONDS}).encode() + b"\n")
            if self.read() != (b"P", b""):
                raise RuntimeError("the decoder could not confirm its limits")
        except (OSError, BadChild, ValueError) as exc:
            raise RuntimeError("the decoder could not be limited") from exc

    def check_identity(self) -> None:
        """A frame cannot nominate an arbitrary PID for assignment or termination."""
        import psutil

        try:
            child, launcher = psutil.Process(self.pid), psutil.Process(self.process.pid)
            if child.pid != launcher.pid and launcher.pid not in [p.pid for p in child.parents()]:
                raise RuntimeError("decoder is not a descendant")
            if child.create_time() < launcher.create_time():
                raise RuntimeError("decoder predates its launcher")
        except psutil.Error as exc:
            raise RuntimeError("decoder identity not confirmed") from exc

    def decode(self, sealed, writer) -> int:
        request = sealed.handoff()
        try:
            self.process.stdin.write(json.dumps(request).encode() + b"\n")
            confirmed = self.read()
        except (OSError, BadChild) as exc:
            raise RuntimeError("the decoder could not be limited") from exc
        if confirmed is None or confirmed[0] != b"P":
            raise RuntimeError("the decoder could not confirm its limits")
        most = int((MAX_IMPORT_HOURS * 3600 + LENGTH_SLACK_SECONDS) * SAMPLE_RATE)
        received = 0
        try:
            while True:
                frame = self.read()
                if frame is None:
                    raise BadChild("no answer")
                kind, payload = frame
                if kind == b"A":
                    if not payload or len(payload) % 4:
                        raise BadChild("audio frame")
                    audio = np.frombuffer(payload, dtype="<f4")
                    if not np.isfinite(audio).all():
                        raise BadChild("nonfinite audio")
                    received += audio.size
                    if received > most:
                        raise BadChild("too much audio")
                    writer.write(audio)
                elif kind == b"R":
                    self.refuse(payload)
                elif kind == b"D":
                    if len(payload) != 8 or struct.unpack("<Q", payload)[0] != received or not received:
                        raise BadChild("sample count")
                    writer.close()
                    return received
                elif kind != b"P":
                    raise BadChild("frame out of order")
        except BadChild as exc:
            log.warning("Decoder stopped: %s (%s)", self.reason or exc, self.exit_code())  # no file detail in either
            raise ImportRefused(NOT_SUPPORTED) from exc

    def refuse(self, payload: bytes) -> None:
        if len(payload) != REFUSAL.size:
            raise BadChild("refusal")
        code, hours = REFUSAL.unpack(payload)
        if code == TOO_LONG_CODE and math.isfinite(hours) and hours >= 0:
            raise TooLong(hours)
        if code not in REFUSALS:
            raise BadChild("refusal code")
        raise ImportRefused(REFUSALS[code])

    def read(self) -> tuple[bytes, bytes] | None:
        """One frame, or None if the child is gone. Its limits are checked before the payload is read."""
        self.waiting = time.monotonic()
        try:
            head = self.exactly(HEAD.size)
            if head is None:
                return None
            kind, size = HEAD.unpack(head)
            if kind not in MAX_FRAMES or size > MAX_FRAMES[kind]:
                raise BadChild("frame")
            payload = self.exactly(size)
            return None if payload is None else (kind, payload)
        finally:
            self.waiting = None

    def exactly(self, size: int) -> bytes | None:
        parts, left = [], size
        while left:
            part = self.process.stdout.read(left)
            if not part:
                return None
            parts.append(part)
            left -= len(part)
        return b"".join(parts)

    def watch(self) -> None:
        polling = sys.platform != "win32"  # a kernel limit there; a watchdog everywhere else
        while not self.done.wait(MEMORY_POLL_SECONDS if polling else TICK_SECONDS):
            now, waiting = time.monotonic(), self.waiting
            reason = None
            if now - self.began > WALL_SECONDS:
                reason = "wall-clock limit"
            elif waiting is not None and now - waiting > SILENCE_SECONDS:
                reason = "no frames"
            elif polling and self.pid is not None:
                try:
                    if footprint(self.pid) > MEMORY_LIMIT:
                        reason = "memory limit"
                except Exception:
                    reason = "memory not readable"
            if reason:
                self.reason = reason
                self.kill()
                return

    def kill(self) -> None:
        if self.job:
            self.job.terminate()
        elif self.pid and self.process and self.pid == self.process.pid:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass
        if self.process:
            try:
                self.process.kill()  # makes the blocking read return; before the handshake, also the launcher's job
            except OSError:
                pass

    def exit_code(self) -> str:
        code = self.process.poll()
        return "still running" if code is None else f"exit {code & 0xffffffff:#x}"

    def stop(self) -> None:
        """Kill, wait, close the job, and only then may the sealed file go: Windows won't delete it while it's read."""
        self.done.set()
        if self.watcher.is_alive():
            self.watcher.join(STOP_SECONDS)
        self.kill()
        if self.job:
            self.job.wait(STOP_SECONDS)
        if self.process:
            try:
                self.process.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("Decoder did not stop in time")
            for pipe in (self.process.stdin, self.process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        if self.job:
            self.job.close()


def decode_isolated(sealed, writer) -> int:
    """Decode the sealed upload into ``writer`` in a limited child. Returns samples written.

    Raises ImportRefused with a message for the user, or RuntimeError if the child couldn't be started or limited."""
    with _slots:
        child = Child()
        try:
            child.start()
            return child.decode(sealed, writer)
        finally:
            child.stop()
