"""The decoder child: one per MP4, MOV or M4A import, run with ``python -m modwen.audio.decoder_child``.

It imports everything first, reports its real pid (a venv's python.exe on Windows is a launcher) and waits, so the
parent can put that process under limits before it reads a byte of the file. Then it gets this one upload's sealed key
on stdin, never in argv or the environment, confirms the limits hold, and sends mono 16 kHz audio up the stdout pipe.
It holds no master or data key, and writes nothing to disk. See ``modwen.audio.decoder`` for the parent.

Frames, child to parent: ``<cI`` type and length, then the payload.
    H  u32 pid             ready for the request
    P  empty               alive; the first one says the limits are confirmed
    A  little-endian f32   audio, at most MAX_AUDIO bytes
    D  u64 samples         finished
    R  u8 code, f64 hours  refused, by number, so no text crosses the boundary

The top of this module stays stdlib only: the parent imports it for the framing.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import sys

HEAD = struct.Struct("<cI")
REFUSAL = struct.Struct("<Bd")
MAX_AUDIO = 128 * 1024
MAX_FRAMES = {b"H": 4, b"P": 0, b"A": MAX_AUDIO, b"D": 8, b"R": REFUSAL.size}
MAX_REQUEST = 64 * 1024
LIMITS_NOT_CONFIRMED = 3
ALLOCATION_CEILING = 512 * 1024 ** 2  # any one FFmpeg allocation; macOS has no kernel memory limit to catch it


class JobIo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("reads", "writes", "others", "read_bytes", "write_bytes",
                                                        "other_bytes")]


class JobBasic(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", ctypes.c_ulong),
                ("min_working_set", ctypes.c_size_t), ("max_working_set", ctypes.c_size_t),
                ("active_processes", ctypes.c_ulong), ("affinity", ctypes.c_size_t),
                ("priority_class", ctypes.c_ulong), ("scheduling_class", ctypes.c_ulong)]


class JobLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [("basic", JobBasic), ("io", JobIo), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t)]


JOB_LIMIT_INFORMATION = 9


def send(fd: int, kind: bytes, payload: bytes = b"") -> None:
    data = memoryview(HEAD.pack(kind, len(payload)) + payload)
    while data:  # a blocking pipe: this waits for the parent, which is the back-pressure
        data = data[os.write(fd, data):]


class PipeWriter:
    """Stands where ``ChunkWriter`` stands in the engine."""

    def __init__(self, fd: int):
        self.fd, self.total_samples = fd, 0

    def write(self, frames) -> None:
        data = frames.astype("<f4").tobytes()
        for at in range(0, len(data), MAX_AUDIO):
            send(self.fd, b"A", data[at:at + MAX_AUDIO])
        self.total_samples += frames.size

    def close(self) -> None:
        pass


def job_holds(memory: int, cpu: int) -> bool:
    """True if the job this process is directly in is the one the parent made. IsProcessInJob can't show that: the venv
    launcher and ``uv run`` have jobs of their own."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                                                   ctypes.c_void_p]
    limits = JobLimits()
    if not kernel32.QueryInformationJobObject(None, JOB_LIMIT_INFORMATION, ctypes.byref(limits),
                                              ctypes.sizeof(limits), None):
        return False
    flags = 0x4 | 0x8 | 0x200 | 0x400 | 0x2000
    return (limits.job_memory == memory and limits.basic.flags == flags and
            limits.basic.active_processes == 1 and limits.basic.job_time == cpu * 10_000_000)


def join_job() -> bool:
    """Join the parent's named job before loading native libraries, then close our handle.

    The parent keeps the only job handle: its death kills us. If it died before startup, OpenJobObject fails and we
    exit without parsing anything. No handle has to be inherited through the Windows venv launcher.
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    kernel.OpenJobObjectW.argtypes, kernel.OpenJobObjectW.restype = [ctypes.c_ulong, ctypes.c_int,
                                                                  ctypes.c_wchar_p], handle
    kernel.GetCurrentProcess.restype = handle
    kernel.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel.CloseHandle.argtypes = [handle]
    name = os.environ.pop("MODWEN_DECODER_JOB", "")
    job = kernel.OpenJobObjectW(0x0001, False, name)  # JOB_OBJECT_ASSIGN_PROCESS
    if not job:
        return False
    try:
        return bool(kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()))
    finally:
        kernel.CloseHandle(job)


def _set_max_alloc() -> bool:
    """Reach av_max_alloc in the libavutil PyAV already loaded. The name differs by platform and wheel."""
    from pathlib import Path

    import av

    root = Path(av.__file__).resolve().parent
    for found in [*root.glob(".dylibs/libavutil*"), *root.parent.glob("av.libs/*avutil*")]:
        try:
            library = ctypes.CDLL(str(found))
            library.av_max_alloc.argtypes, library.av_max_alloc.restype = [ctypes.c_size_t], None
            library.av_max_alloc(ALLOCATION_CEILING)
            return True
        except (OSError, AttributeError):
            continue
    return False


def _life_line() -> None:
    """The parent holds stdin open until it is done, so end of file means it is gone. POSIX only: on Windows a thread
    blocked on stdin deadlocks DLL loads, and closing the job does this job there."""
    import threading

    def wait() -> None:
        while sys.stdin.buffer.read(4096):
            pass
        os._exit(1)

    threading.Thread(target=wait, daemon=True).start()


def posix_limits(cpu_seconds: int) -> bool:
    """The parent's memory watchdog is the main limit here; these are what the child can set on itself."""
    import resource

    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
        os.nice(10)
    except (OSError, ValueError):
        return False
    if not _set_max_alloc():
        return False
    return True


def main() -> int:
    out = os.dup(1)
    os.dup2(os.open(os.devnull, os.O_WRONLY), 1)  # nothing a library prints can land in the frames
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(out, os.O_BINARY)
        if not join_job():
            return LIMITS_NOT_CONFIRMED
    import av  # noqa: F401
    import soxr  # noqa: F401

    from modwen.audio import importer

    send(out, b"H", struct.pack("<I", os.getpid()))
    limits = json.loads(sys.stdin.buffer.readline(MAX_REQUEST))
    held = job_holds(limits["memory"], limits["cpu"]) if sys.platform == "win32" else posix_limits(limits["cpu"])
    if not held:
        return LIMITS_NOT_CONFIRMED
    send(out, b"P")  # no upload path or key has crossed the boundary yet
    request = json.loads(sys.stdin.buffer.readline(MAX_REQUEST))
    if sys.platform != "win32":
        _life_line()
    sealed = importer.SealedTempFile.reopen(request["path"], bytes.fromhex(request["key"]), request["iv"],
                                            request["size"])
    beat = lambda: send(out, b"P")  # noqa: E731
    beat()
    codes = {text: code for code, text in importer.REFUSALS.items()}
    try:
        samples = importer.decode_media_into(sealed, PipeWriter(out), beat)
    except importer.TooLong as refused:
        send(out, b"R", REFUSAL.pack(importer.TOO_LONG_CODE, refused.hours))
    except importer.ImportRefused as refused:
        send(out, b"R", REFUSAL.pack(codes[str(refused)], 0.0))
    else:
        send(out, b"D", struct.pack("<Q", samples))
    return 0


if __name__ == "__main__":
    # Any failure is the parent's to read as an exit with no answer. _exit skips native teardown, where a file that
    # upset FFmpeg could still crash after the answer has gone.
    code = 1
    try:
        code = main()
    finally:
        os._exit(code)
