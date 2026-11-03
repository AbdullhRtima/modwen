"""The production process boundary, generated hostile files and deliberately broken child peers.

No decoder fault is injected into pytest itself. Windows kernel-limit tests run in the real named job; an outer
watchdog kills the test peer if production cleanup fails. Native Mac behaviour still requires a Mac runner.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import psutil
import pytest

from modwen.audio import decoder as D, decoder_child as C, importer as I
from modwen.audio.chunks import ChunkWriter
from tests.test_import_media import FRAGMENTED, _encode_audio, make_media, top_boxes
from tests.test_import_preflight import (FULL, find, parse, put, ser, sound_entry, stbl, stco, stsc_bomb, trak)
from tests.test_import_safety import box, rewrite

av = pytest.importorskip("av")


class Writer:
    def __init__(self):
        self.total_samples, self.blocks, self.closed = 0, [], False

    def write(self, frames):
        self.blocks.append(frames.copy())
        self.total_samples += frames.size

    def close(self):
        self.closed = True


def decode(where, data, writer=None):
    writer = writer or Writer()
    with I.SealedTempFile(where) as sealed:
        sealed.append(data)
        return D.decode_isolated(sealed, writer), writer


@pytest.mark.parametrize("codec,fmt", [("aac", "mp4"), ("pcm_s16le", "mov")])
def test_f14_seven_point_one_is_packed_before_numpy(data_dir, codec, fmt):
    out = io.BytesIO()
    with av.open(out, "w", format=fmt) as container:
        stream = container.add_stream(codec, rate=48000, layout="7.1")
        for packet in _encode_audio(container, stream, 440, 2):
            container.mux(packet)
    samples, writer = decode(data_dir, out.getvalue())
    assert samples == pytest.approx(32000, abs=500)
    assert writer.closed and np.isfinite(np.concatenate(writer.blocks)).all()


def attacks(base, name):
    if name.startswith("moof"):
        tree = parse(base)
        find(tree, b"moov")[1].extend([[b"trak", []] for _ in range(999)])
        return ser(tree) + box(b"moof", b"") * int(name.split("_")[1])
    if name.startswith("co64"):
        tree = stsc_bomb(base)
        table = stbl(tree)
        old = next(b for b in table if b[0] == b"stco")
        at = table.index(old)
        old[:] = [b"co64", FULL + struct.pack(">I", 40) + struct.pack(">Q", 40) * 40]
        table.insert(at + int(name.endswith("last")), [b"stco", FULL + bytes(4)])
        return ser(tree)
    if name == "stz2":
        tree, n = parse(base), 1 << 25
        put(stbl(tree), b"stts", FULL + struct.pack(">III", 1, 1, 1024))
        put(stbl(tree), b"stsc", FULL + struct.pack(">IIII", 1, 1, n, 1))
        put(stbl(tree), b"stsz", FULL + bytes(8))
        stco(tree)
        stbl(tree).append([b"stz2", FULL + b"\0\0\0\x04" + struct.pack(">I", n) + bytes(n // 2)])
        return ser(tree)
    if name.startswith("sync"):
        tree = parse(base)
        h = find(tree, b"moov", b"trak", b"mdia", b"hdlr")
        h[1] = h[1][:8] + b"vide" + h[1][12:]
        put(stbl(tree), b"stsd", FULL + struct.pack(">I", 1) + box(b"hvc1", bytes(78)))
        inner = box(b"sgpd", b"\x01\0\0\0sync" + struct.pack(">II", 1, 1) + bytes([21]))
        inner += box(b"sbgp", FULL + b"sync" + struct.pack(">III", 1, 0x1f000000, 1))
        trak(tree).append([b"moof", box(b"traf", inner)] if name.endswith("moof") else [b"traf", inner])
        return ser(tree)
    if name in ("iinf", "iloc"):
        atom = box(name.encode(), (b"\x01\0\0\0" if name == "iinf" else b"\x02\0\0\0\0\0") +
                   struct.pack(">I", 1 << 27))
        return rewrite(base, b"stsd", lambda body: body[:8] +
                       struct.pack(">I", len(body[8:]) + len(atom)) + body[12:] + atom)
    tree, n = parse(base), 1 << 24
    table = stbl(tree)
    put(table, b"stts", FULL + struct.pack(">III", 1, n, 1))
    put(table, b"stsc", FULL + struct.pack(">IIII", 1, 1, n, 1))
    put(table, b"stsz", FULL + struct.pack(">II", 1, n))
    put(table, b"stsd", FULL + struct.pack(">I", 1) + sound_entry(b"sowt"))
    stco(tree)
    if name == "hdlr_meta":
        trak(tree).append([b"meta", b"hdlr" + bytes(4) + b"mhlr" + b"vide" + bytes(13)])
        return ser(tree) + box(b"free", bytes(n))
    assert name == "hdlr_short"
    trak(tree).extend([[b"hdlr", FULL + b"mhlr"], [b"zzzz", b""]])
    body = ser(tree)
    at = body.rindex(b"zzzz") - 4
    return body[:at] + b"vide" + body[at + 4:] + box(b"free", bytes(n))


ATTACKS = ["moof_2000", "moof_20000", "moof_100000", "co64_first", "co64_last", "stz2", "sync_traf",
           "sync_moof", "iinf", "iloc", "hdlr_meta", "hdlr_short"]


@pytest.mark.parametrize("name", ATTACKS)
def test_f15_to_f20_native_allocations_are_contained(data_dir, monkeypatch, name):
    # A shortened wall timer protects CI from regressions, but reaching it must fail this resource proof.
    stop, reasons = D.Child.stop, []

    def remember(self):
        reasons.append(self.reason)
        stop(self)

    monkeypatch.setattr(D.Child, "stop", remember)
    D.WALL_SECONDS, old = min(D.WALL_SECONDS, 40), D.WALL_SECONDS
    try:
        body = attacks(make_media(data_dir, seconds=2), name)
        if name == "moof_2000":
            assert decode(data_dir, body)[0] == pytest.approx(32000, abs=500)
        else:
            with pytest.raises(I.ImportRefused):
                decode(data_dir, body)
        assert decode(data_dir, make_media(data_dir, seconds=2))[0] > 0  # parent and next import survive
        assert not any(reasons)
    finally:
        D.WALL_SECONDS = old


@pytest.mark.parametrize("tag,codec", [(b"NONE", "pcm_s16be"), (b"ms\0\x01", "pcm_s16le")])
def test_f21_long_pcm_tags_are_not_counted_as_packets(data_dir, tag, codec):
    from tests.test_import_preflight import pcm_file

    body = pcm_file(data_dir, "mov", codec, 170)
    body = body.replace(b"twos" if codec.endswith("be") else b"sowt", tag, 1)
    assert decode(data_dir, body, ChunkWriter(data_dir / "out", "system", b"k" * 32))[0] == 170 * 16000


def test_f22_opaque_metadata_is_not_walked_as_a_container(data_dir):
    body = make_media(data_dir, seconds=2)
    opaque = box(b"stsz", FULL + struct.pack(">II", 8, 1 << 26))
    body = rewrite(body, b"trak", lambda t: t + box(b"udta", box(b"name", opaque)))
    assert decode(data_dir, body)[0] > 0


def test_f23_audible_protection_uses_the_unsupported_message(data_dir):
    body = rewrite(make_media(data_dir, seconds=2), b"stsd", lambda t: t.replace(b"mp4a", b"aavd"))
    with pytest.raises(I.ImportRefused) as caught:
        decode(data_dir, body)
    assert str(caught.value) == I.NOT_SUPPORTED


PEER = '''
import ctypes,json,os,struct,sys,time
from modwen.audio.decoder_child import send,join_job,job_holds
out=os.dup(1)
if sys.platform == 'win32':
    import msvcrt
    msvcrt.setmode(out,os.O_BINARY)
    if not join_job(): os._exit(3)
{early}
send(out,b'H',struct.pack('<I',os.getpid()))
limits=json.loads(sys.stdin.buffer.readline())
if sys.platform == 'win32' and not job_holds(limits['memory'],limits['cpu']): os._exit(3)
{confirm}
send(out,b'P')
request=json.loads(sys.stdin.buffer.readline())
send(out,b'P')
{action}
'''


def peer(monkeypatch, action="", early="", confirm=""):
    code = PEER.format(early=early, confirm=confirm, action=action)
    monkeypatch.setattr(D, "child_command", lambda: [sys.executable, "-P", "-c", code])


@pytest.mark.parametrize("action", [
    "ctypes.memset(0,1,1)", "os._exit(9)", "send(out,b'Z')", "os.write(out,b'A'+struct.pack('<I',131073))",
    "send(out,b'A',b'x')", "send(out,b'A',struct.pack('<f',float('nan')))",
    "send(out,b'D',struct.pack('<Q',1))", "send(out,b'R',struct.pack('<Bd',99,0))",
    "send(out,b'R',struct.pack('<Bd',5,float('inf')))", "os.write(out,b'A\\x04')",
    "os.write(out,b'A'+struct.pack('<I',4)+b'x')",
])
def test_child_crashes_and_invalid_frames_cannot_succeed(data_dir, monkeypatch, action):
    peer(monkeypatch, action=action)
    with pytest.raises(I.ImportRefused, match="supported"):
        decode(data_dir, b"sealed dummy")
    assert list(data_dir.glob("*.sealed")) == []


@pytest.mark.parametrize("early,confirm", [("os._exit(1)", ""), ("os.write(out,b'H\\x04');os._exit(1)", ""),
    ("send(out,b'H',struct.pack('<I',os.getppid()));time.sleep(1);os._exit(1)", ""), ("", "os._exit(1)")])
def test_startup_failures_never_receive_the_upload_key(data_dir, monkeypatch, early, confirm):
    peer(monkeypatch, early=early, confirm=confirm)
    with I.SealedTempFile(data_dir) as sealed:
        monkeypatch.setattr(sealed, "handoff", lambda: pytest.fail("key sent before confirmed limits"))
        with pytest.raises(RuntimeError):
            D.decode_isolated(sealed, Writer())


@pytest.mark.parametrize("timer", ["SILENCE_SECONDS", "WALL_SECONDS"])
def test_hangs_and_endless_heartbeats_are_killed(data_dir, monkeypatch, timer):
    monkeypatch.setattr(D, timer, 1.0)
    action = "time.sleep(20)" if timer == "SILENCE_SECONDS" else "while True: send(out,b'P');time.sleep(.02)"
    peer(monkeypatch, action=action)
    began = time.monotonic()
    with pytest.raises(I.ImportRefused):
        decode(data_dir, b"sealed dummy")
    assert time.monotonic() - began < 10


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel commitment limit")
def test_a_real_kernel_limit_breach_does_not_terminate_the_engine(data_dir, monkeypatch):
    peer(monkeypatch, action="memory=bytearray(2*1024**3);os._exit(11)")
    with pytest.raises(I.ImportRefused):
        decode(data_dir, b"sealed dummy")
    assert psutil.Process().is_running()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job CPU and process limits")
def test_cpu_and_descendant_limits_hold(data_dir, monkeypatch):
    monkeypatch.setattr(D, "CPU_SECONDS", 1)
    peer(monkeypatch, action="while True: pass")
    began = time.monotonic()
    with pytest.raises(I.ImportRefused):
        decode(data_dir, b"dummy")
    assert time.monotonic() - began < 20  # Windows checks CPU periodically; this is not an exact wall deadline
    peer(monkeypatch, action="""
import subprocess
try: subprocess.Popen([sys.executable,'-c','pass'])
except OSError: send(out,b'A',bytes(4));send(out,b'D',struct.pack('<Q',1))
else: os._exit(10)
""")
    assert decode(data_dir, b"dummy")[0] == 1


def test_disk_failure_kills_the_child_before_upload_cleanup(data_dir, monkeypatch):
    peer(monkeypatch, action="send(out,b'A',bytes(4));time.sleep(30)")
    start, pids = D.Child.start, []

    def remember(self):
        start(self)
        pids.append(self.pid)

    monkeypatch.setattr(D.Child, "start", remember)

    class FullDisk(Writer):
        def write(self, frames):
            raise OSError(28, "synthetic disk full")

    with pytest.raises(OSError):
        decode(data_dir, b"dummy", FullDisk())
    assert all(not psutil.pid_exists(pid) for pid in pids)
    assert list(data_dir.glob("*.sealed")) == []


def test_pipe_backpressure_does_not_queue_whole_audio(data_dir, monkeypatch):
    peer(monkeypatch, action="""
block=bytes(131072)
for _ in range(32): send(out,b'A',block)
send(out,b'D',struct.pack('<Q',32*32768))
""")
    start, pids = D.Child.start, []

    def remember(self):
        start(self)
        pids.append(self.pid)

    monkeypatch.setattr(D.Child, "start", remember)

    class SlowDisk(Writer):
        def write(self, frames):
            self.total_samples += frames.size
            self.blocks.append(psutil.Process(pids[0]).memory_info().rss)
            time.sleep(.02)

    samples, writer = decode(data_dir, b"dummy", SlowDisk())
    assert samples == 32 * 32768 and writer.closed
    assert max(writer.blocks) - min(writer.blocks) < 16 * 1024 ** 2


@pytest.mark.parametrize("stage", ["startup", "decoding"])
def test_parent_death_leaves_no_decoder(data_dir, stage):
    source = Path(__file__).resolve().parent.parent
    media = data_dir / "source.mp4"
    media.write_bytes(make_media(data_dir, seconds=2))
    script = '''
import json,os,sys,time
from pathlib import Path
from modwen.audio import decoder as D,importer as I
child=D.Child()
class Writer:
    def write(self,frames):
        print(json.dumps([os.getpid(),child.pid]),flush=True)
        time.sleep(30)
    def close(self):pass
child.start()
if sys.argv[1]=='startup':
    print(json.dumps([os.getpid(),child.pid]),flush=True)
    time.sleep(30)
else:
    with I.SealedTempFile(Path(sys.argv[2]).parent) as sealed:
        sealed.append(Path(sys.argv[2]).read_bytes())
        child.decode(sealed,Writer())
'''
    env = {**os.environ, "PYTHONPATH": str(source)}
    process = subprocess.Popen([sys.executable, "-P", "-c", script, stage, str(media)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    timed_out = []

    def safety_stop():
        timed_out.append(True)
        try:
            for member in reversed([psutil.Process(process.pid), *psutil.Process(process.pid).children(recursive=True)]):
                member.kill()
        except psutil.Error:
            pass

    timer = threading.Timer(15, safety_stop)
    timer.start()
    try:
        parent, decoder = json.loads(process.stdout.readline())
        psutil.Process(parent).kill()
        process.wait(timeout=10)
        deadline = time.monotonic() + 5
        while psutil.pid_exists(decoder) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not psutil.pid_exists(decoder)
        assert not timed_out
        I.remove_leftovers(data_dir)
        assert list(data_dir.glob("*.sealed")) == []
    finally:
        timer.cancel()
        if process.poll() is None:
            safety_stop()
        process.stdout.close()


def test_upload_reopen_owns_no_file_and_gets_no_storage_key(data_dir):
    with I.SealedTempFile(data_dir) as sealed:
        sealed.append(b"synthetic privacy marker")
        request = sealed.handoff()
        reopened = I.SealedTempFile.reopen(request["path"], bytes.fromhex(request["key"]), request["iv"], request["size"])
        assert reopened.read() == b"synthetic privacy marker"
        reopened.close()
        assert sealed.path.exists()
        assert b"synthetic privacy marker" not in sealed.path.read_bytes()
    assert not sealed.path.exists()


def test_child_environment_does_not_contain_engine_secrets(monkeypatch):
    monkeypatch.setenv("MODWEN_TOKEN", "secret-token")
    monkeypatch.setenv("MODWEN_KEYSTORE", "file")
    monkeypatch.setenv("UNRELATED_SECRET", "secret")
    env = D.child_env()
    assert not any(k in env for k in ("MODWEN_TOKEN", "MODWEN_KEYSTORE", "UNRELATED_SECRET"))
    assert "secret-token" not in json.dumps(env)


def test_platform_abi_layouts():
    assert ctypes.sizeof(D.RusageInfoV0) == 96
    assert D.RusageInfoV0.phys_footprint.offset == 72
    if sys.platform == "win32":
        assert ctypes.sizeof(C.JobLimits) == 144
        assert C.JobLimits.job_memory.offset == 120


def test_macos_footprint_uses_the_documented_struct_and_fails_closed(monkeypatch):
    # ABI/call wiring only; this is deliberately not a Mac runtime resource proof.
    monkeypatch.setattr(D, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=100)))

    class Proc:
        def __call__(self, pid, flavor, info):
            assert pid == 42 and flavor == 0
            ctypes.cast(info, ctypes.POINTER(D.RusageInfoV0)).contents.phys_footprint = 200
            return 0

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: SimpleNamespace(proc_pid_rusage=Proc()))
    assert D.footprint(42) == 200

    class Failed(Proc):
        def __call__(self, *args):
            return -1

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: SimpleNamespace(proc_pid_rusage=Failed()))
    with pytest.raises(OSError):
        D.footprint(42)


def test_posix_limits_fail_closed_without_allocation_guard(monkeypatch):
    resource = SimpleNamespace(RLIMIT_CORE=4, RLIMIT_CPU=0, setrlimit=lambda *a: None)
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(os, "nice", lambda value: 0, raising=False)
    monkeypatch.setattr(C, "_set_max_alloc", lambda: False)
    assert not C.posix_limits(3600)
    monkeypatch.setattr(C, "_set_max_alloc", lambda: True)
    assert C.posix_limits(3600)


def test_only_two_decoders_run_at_once(data_dir, monkeypatch):
    peer(monkeypatch, action="time.sleep(.2);send(out,b'A',bytes(4));send(out,b'D',struct.pack('<Q',1))")
    start, stop, lock, active, peak = D.Child.start, D.Child.stop, threading.Lock(), [0], [0]

    def counted_start(self):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        start(self)

    def counted_stop(self):
        try:
            stop(self)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(D.Child, "start", counted_start)
    monkeypatch.setattr(D.Child, "stop", counted_stop)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        assert [x[0] for x in pool.map(lambda _: decode(data_dir, b"dummy"), range(3))] == [1, 1, 1]
    assert peak[0] == 2 and active[0] == 0


def hogs():
    """As many normal-priority spinners as the PC has cores: the load that starved the child in F24."""
    python = getattr(sys, "_base_executable", sys.executable)  # a venv's launcher would leave its spinner behind
    return [subprocess.Popen([python, "-P", "-c", "while True: pass"]) for _ in range(psutil.cpu_count())]


@pytest.mark.skipif(sys.platform != "win32", reason="F24 was the below-normal class; macOS needs its own run")
def test_f24_an_intact_file_survives_every_core_being_busy(data_dir, monkeypatch):
    """Under a timer far below the real 180 s, so a child that doesn't get the CPU can't pass slowly."""
    monkeypatch.setattr(D, "SILENCE_SECONDS", 30)
    body, busy = make_media(data_dir, seconds=2), hogs()
    try:
        time.sleep(1)
        samples, _ = decode(data_dir, body)
    finally:
        for hog in busy:
            hog.kill()
            hog.wait()
    assert samples == pytest.approx(32000, abs=500)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority classes")
def test_f24_the_child_runs_at_normal_priority(monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    peer(monkeypatch, action="time.sleep(30)")
    monkeypatch.setattr(subprocess, "Popen", spy)
    child = D.Child()
    try:
        child.start()
    finally:
        child.stop()
    assert not seen["creationflags"] & (subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.IDLE_PRIORITY_CLASS)


def test_f24_posix_limits_leave_the_priority_alone(monkeypatch):
    resource = SimpleNamespace(RLIMIT_CORE=4, RLIMIT_CPU=0, setrlimit=lambda *_: None)
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(C, "_set_max_alloc", lambda: True)
    monkeypatch.setattr(os, "nice", lambda _: pytest.fail("a niced child starves as F24 did"), raising=False)
    assert C.posix_limits(60)


@pytest.mark.parametrize("action", ["time.sleep(60)", "while True: pass"], ids=["hung", "spinning"])
def test_f24_a_child_that_sends_nothing_is_still_stopped_by_the_silence_timer(data_dir, monkeypatch, action):
    monkeypatch.setattr(D, "SILENCE_SECONDS", 2.0)
    peer(monkeypatch, action=action)
    began = time.monotonic()
    with pytest.raises(I.ImportRefused, match="supported"):
        decode(data_dir, b"sealed dummy")
    assert 1.5 < time.monotonic() - began < 15


def test_f24_a_frozen_child_is_still_killed(data_dir, monkeypatch):
    """Frozen by the OS mid-import: no CPU and no frames, which a starved child also shows. The timer must still fire."""
    monkeypatch.setattr(D, "SILENCE_SECONDS", 2.0)
    peer(monkeypatch, action="send(out,b'A',struct.pack('<f',0.0));time.sleep(60)")
    frozen = []

    class Freezes(Writer):
        def write(self, frames):
            frozen.extend(psutil.Process().children(recursive=True))
            for process in frozen:
                process.suspend()

    began = time.monotonic()
    with pytest.raises(I.ImportRefused, match="supported"):
        decode(data_dir, b"sealed dummy", Freezes())
    assert frozen and time.monotonic() - began < 15


def float_pcm(codec, seconds=2.0, rate=48000, change=None):
    """A stereo float PCM MOV holding a 440 Hz tone. ``change`` edits the samples, shaped (channels, samples)."""
    n = int(seconds * rate)
    wave = (0.3 * np.sin(2 * np.pi * 440 * np.arange(n) / rate)).astype(np.float32)
    channels = np.stack([wave, wave])
    if change:
        change(channels)
    out = io.BytesIO()
    with av.open(out, "w", format="mov") as container:
        stream = container.add_stream(codec, rate=rate, layout="stereo")
        for at in range(0, n, 4096):
            block = channels[:, at:at + 4096].T.reshape(1, -1).copy()
            frame = av.AudioFrame.from_ndarray(block, format="flt", layout="stereo")
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return out.getvalue()


def poke(value, count=1):
    def change(channels):
        channels[0, 24000:24000 + count] = value
    return change


@pytest.mark.parametrize("codec", ["pcm_f32le", "pcm_f32be"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "+inf", "-inf"])
def test_f26_one_bad_sample_costs_that_sample_not_the_file(data_dir, codec, value):
    clean = np.concatenate(decode(data_dir, float_pcm(codec))[1].blocks)
    samples, writer = decode(data_dir, float_pcm(codec, change=poke(value)))
    audio = np.concatenate(writer.blocks)
    assert samples == clean.size and np.isfinite(audio).all()
    near = np.abs(np.arange(samples) - 8000) < 800  # sample 24000 at 48 kHz, plus the resampler's reach
    assert np.abs(audio - clean)[~near].max() < 1e-3
    assert np.abs(audio - clean)[near].max() < 0.3  # the other channel and the tone around it survive


def test_f26_a_burst_with_no_resampling_is_cleaned_too(data_dir):
    samples, writer = decode(data_dir, float_pcm("pcm_f32le", rate=16000, change=poke(float("nan"), 400)))
    assert samples == 32000 and np.isfinite(np.concatenate(writer.blocks)).all()


def test_f26_finite_values_that_overflow_the_downmix_are_cleaned(data_dir):
    def change(channels):
        channels[:, 24000:24100] = 3e38

    samples, writer = decode(data_dir, float_pcm("pcm_f32le", change=change))
    assert samples == pytest.approx(32000, abs=64) and np.isfinite(np.concatenate(writer.blocks)).all()


def edit_list(data, edits):
    entries = b"".join(struct.pack(">IiI", span, start, 1 << 16) for span, start in edits)
    return rewrite(data, b"edts", lambda _: box(b"elst", FULL + struct.pack(">I", len(edits)) + entries))


def lose_mdhd(data, count=None):
    """QA 28's shape: one changed byte in the mdhd's type, so FFmpeg skips the box, and optionally the elst's count."""
    out = bytearray(data)
    out[data.index(b"mdhd") + 2] = 0xef
    if count is not None:
        out[data.index(b"elst") + 8:data.index(b"elst") + 12] = count.to_bytes(4, "big")
    return bytes(out)


@pytest.mark.parametrize("kw,count", [(dict(movflags="faststart"), 0xfd000001), (dict(fmt="mov", codec="pcm_s16le"), 0x8f0001)],
                         ids=["faststart-0412", "pcm-0055"])
def test_f28_a_lost_mdhd_is_damage_not_a_shorter_recording(data_dir, kw, count):
    with pytest.raises(I.ImportRefused, match="damaged"):
        decode(data_dir, lose_mdhd(make_media(data_dir, seconds=6, **kw), count))


def test_f28_an_elst_count_alone_loses_nothing_and_still_imports(data_dir):
    body = bytearray(make_media(data_dir, seconds=6, movflags="faststart"))
    at = body.index(b"elst") + 8
    body[at:at + 4] = (0xfd000001).to_bytes(4, "big")
    assert decode(data_dir, bytes(body))[0] / 16000 == pytest.approx(6, abs=0.1)


def test_f28_a_zero_timescale_is_damage(data_dir):
    body = bytearray(make_media(data_dir, seconds=6, movflags="faststart"))
    at = body.index(b"mdhd") + 8
    at += 16 if body[at - 4] else 8
    assert int.from_bytes(body[at:at + 4], "big") == 48000
    body[at:at + 4] = bytes(4)
    with pytest.raises(I.ImportRefused, match="damaged"):
        decode(data_dir, bytes(body))


def test_f28_metadata_named_like_a_track_stays_opaque(data_dir):
    """A udta holding something called mdia, with no mdhd in it, is F22's shape: it isn't a track, so nothing is lost."""
    tree = parse(make_media(data_dir, seconds=2))  # moov last, so no offset moves
    find(tree, b"moov")[1].append([b"udta", ser([[b"mdia", ser([[b"free", b""]])]])])
    assert decode(data_dir, ser(tree))[0] == pytest.approx(32000, abs=500)


@pytest.mark.parametrize("edits,seconds", [
    ([(20, 0)], 20), ([(15, 0)], 15), ([(15, 5)], 15), ([(2, -1), (20, 0)], 20), ([(5, 0), (5, 10)], 10),
    ([(30, 0)], 20), ([(3, 0), (3, 5), (3, 10)], 9), ([(19.95, 0)], 19.95), ([(2, i * 2) for i in range(5)], 10),
    ([(.2, i) for i in range(40)], 4),  # the last edits start past the 20 s of media
], ids=["whole", "trim-end", "skip-start", "delay", "drop-middle", "past-end", "three", "tiny-trim", "five", "forty"])
def test_f28_intact_edit_lists_import_as_before(data_dir, edits, seconds):
    """Same cases as QA 28's matrix: the tkhd isn't updated in these, so a rule comparing it to the edits refuses them."""
    data = make_media(data_dir, seconds=20)
    mvhd = data.index(b"mvhd")
    scale = int.from_bytes(data[mvhd + 16:mvhd + 20], "big")
    changed = edit_list(data, [(int(span * scale), -1 if at < 0 else int(at * 48000)) for span, at in edits])
    assert decode(data_dir, changed)[0] / 16000 == pytest.approx(seconds, abs=0.3)


def every(value, channels=(0, 1)):
    def change(samples):
        samples[list(channels)] = value
    return change


@pytest.mark.parametrize("codec", ["pcm_f32le", "pcm_f32be"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "+inf", "-inf"])
def test_f29_audio_with_no_finite_sample_is_damage_not_silence(data_dir, codec, value):
    with pytest.raises(I.ImportRefused, match="damaged"):
        decode(data_dir, float_pcm(codec, change=every(value)))


def test_f29_holds_without_resampling_too(data_dir):
    with pytest.raises(I.ImportRefused, match="damaged"):
        decode(data_dir, float_pcm("pcm_f32le", rate=16000, change=every(float("nan"))))


def test_f29_one_lost_channel_still_imports_the_other(data_dir):
    samples, writer = decode(data_dir, float_pcm("pcm_f32le", change=every(float("nan"), [0])))
    audio = np.concatenate(writer.blocks)
    assert samples == pytest.approx(32000, abs=64) and np.isfinite(audio).all()
    assert np.abs(audio).max() == pytest.approx(0.15, abs=0.01)  # the right channel's tone, halved by the downmix


def test_f29_only_a_few_finite_samples_still_import(data_dir):
    def change(channels):
        channels[:, :] = float("nan")
        channels[:, 50000:50010] = 0.3

    assert decode(data_dir, float_pcm("pcm_f32le", change=change))[0] == pytest.approx(32000, abs=64)


# QA 31's F30 files: QA 28's mutations of this same 6-second fragmented file, as {offset from the first tfhd: byte}.
# Each loses the first of three fragments with every byte present, and imported 4.03 s before task 33.
QA_F30 = {
    "0025-tfhd-past-traf": {0: 0x7f, 1: 0xff, 2: 0xff, 3: 0xff},
    "0214-tfhd-past-traf-2^32": {0: 0xff, 1: 0xff, 2: 0xff, 3: 0xff},  # 0472 is the same
    "0296-tfhd-64-bit-size-past-traf": {3: 0x01},
    "0194-tfdt-past-traf": {8: 0xb3, 29: 0xf4, 38: 0x5d, 41: 0xf1},
    "0042-tfhd-flags-overread": {8: 0x96, 11: 0xeb, 21: 0xc2, 24: 0x53, 47: 0x82},
    "0315-tfhd-names-no-track": {11: 0xa6, 12: 0x77, 25: 0xfc, 31: 0xd9, 34: 0x83, 47: 0x84},
    "0121-tfhd-size-0-swallows-trun": {3: 0x00},  # 0350 is the same
}


def first_tfhd(data):
    at = data.index(b"tfhd", [at for at, _, kind in top_boxes(data) if kind == b"moof"][0]) - 4
    assert data[at:at + 16] == bytes.fromhex("0000001c746668640002003800000001")  # QA's base, on both av versions
    return at


def qa_f30(data, name):
    out, at = bytearray(data), first_tfhd(data)
    for offset, value in QA_F30[name].items():
        out[at + offset] = value
    return bytes(out)


@pytest.mark.parametrize("name", list(QA_F30))
def test_f30_a_broken_fragment_header_is_damage_not_a_shorter_recording(data_dir, name):
    data = make_media(data_dir, seconds=6, movflags=FRAGMENTED)
    assert decode(data_dir, data)[0] / 16000 == pytest.approx(6, abs=0.1)
    with pytest.raises(I.ImportRefused, match="damaged"):
        decode(data_dir, qa_f30(data, name))


@pytest.mark.parametrize("offset,value,seconds", [(8, 150, 6), (12, 0x77, None), (15, 0x02, None)],
                         ids=["version-only-loses-nothing", "track-1996488705-alone", "track-2-alone"])
def test_f30_what_ffmpeg_reads_decides(data_dir, offset, value, seconds):
    """FFmpeg skips a tfhd's version, so changing only that loses nothing and still imports. A tfhd naming no track
    followed by an intact trun is an error in FFmpeg itself, so it was refused before task 33 and still is."""
    data = make_media(data_dir, seconds=6, movflags=FRAGMENTED)
    out = bytearray(data)
    out[first_tfhd(data) + offset] = value
    if seconds is None:
        with pytest.raises(I.ImportRefused, match="damaged"):
            decode(data_dir, bytes(out))
    else:
        assert decode(data_dir, bytes(out))[0] / 16000 == pytest.approx(seconds, abs=0.1)


FRAG = "empty_moov+default_base_moof+frag_keyframe"


@pytest.mark.parametrize("fmt,flags,video", [
    ("mp4", FRAGMENTED, False), ("mp4", FRAGMENTED, True), ("mp4", FRAG, False), ("mp4", "cmaf", False),
    ("mp4", "dash", False), ("mp4", "dash+global_sidx", False), ("mp4", FRAG + "+separate_moof", True),
    ("mp4", "delay_moov+frag_keyframe", False), ("mp4", FRAG + "+omit_tfhd_offset", False), ("mov", FRAG, False),
    ("ismv", None, False), ("ismv", None, True),
], ids=["frag", "frag-video", "frag_keyframe", "cmaf", "dash", "global_sidx", "separate_moof-video", "delay_moov",
        "omit_tfhd_offset", "frag-mov", "ismv", "ismv-video"])
def test_f30_valid_fragmented_files_import_whole(data_dir, fmt, flags, video):
    data = make_media(data_dir, seconds=6, fmt=fmt, movflags=flags, video=video)
    assert b"moof" in data
    assert decode(data_dir, data)[0] / 16000 == pytest.approx(6, abs=0.1)


# Security 34's rules B, E and A: fragment shapes FFmpeg reads without an error while skipping a fragment's audio.
def children(data, start, end):
    """(offset, size, type) of each box between two offsets of an intact file."""
    at, out = start, []
    while at + 8 <= end:
        size = int.from_bytes(data[at:at + 4], "big")
        out.append((at, size, data[at + 4:at + 8]))
        at += size
    return out


def fragment(data, nth=0):
    """(moof offset, moof size, traf offset, traf size) of the nth fragment of an intact file."""
    moof, size, _ = [b for b in top_boxes(data) if b[2] == b"moof"][nth]
    traf, traf_size, _ = [b for b in children(data, moof + 8, moof + size) if b[2] == b"traf"][0]
    return moof, size, traf, traf_size


def in_fragment(data, kind, nth=0):
    moof, size, traf, traf_size = fragment(data, nth)
    return [at for at, _, name in children(data, moof + 8, moof + size) + children(data, traf + 8, traf + traf_size)
            if name == kind][0]


def set_size(data, kind, value, large=None, nth=0):
    out, at = bytearray(data), in_fragment(data, kind, nth)
    out[at:at + 4] = value.to_bytes(4, "big")
    if large is not None:
        out[at + 8:at + 16] = large.to_bytes(8, "big")
    return bytes(out)


def zero(data, at, count):
    return data[:at] + bytes(count) + data[at + count:]


def append_to_traf(data, blob):
    """``blob`` as the first traf's last child, with the moof, the traf and the trun's data offset grown to match."""
    out = bytearray(data)
    moof, size, traf, traf_size = fragment(data)
    trun = in_fragment(data, b"trun")
    assert int.from_bytes(data[trun + 8:trun + 12], "big") & 1  # the trun gives a data offset, from the moof
    for at, grow in ((moof, len(blob)), (traf, len(blob)), (trun + 16, len(blob))):
        out[at:at + 4] = (int.from_bytes(out[at:at + 4], "big") + grow).to_bytes(4, "big")
    return bytes(out[:traf + traf_size] + blob + out[traf + traf_size:])


def frag_0015(data):
    """QA 28's frag-0015 on 3.11: a zero run from the second moof's mfhd over the next mdat's header."""
    return zero(data, fragment(data, 1)[0] + 8, 2002)


def mdat_header_zeroed(data):
    mdat = [at for at, _, kind in top_boxes(data) if kind == b"mdat" and at > fragment(data, 1)[0]][0]
    return zero(data, mdat, 8)


LOSES_A_FRAGMENT = {
    "B-tfdt-size-4": lambda d: set_size(d, b"tfdt", 4),
    "B-tfdt-size-7": lambda d: set_size(d, b"tfdt", 7),
    "B-tfdt-size-2": lambda d: set_size(d, b"tfdt", 2),
    "B-tfhd-size-4": lambda d: set_size(d, b"tfhd", 4),
    "B-trun-size-6": lambda d: set_size(d, b"trun", 6),
    "B-tfdt-size-1-largesize-12": lambda d: set_size(d, b"tfdt", 1, 12),
    "B-mfhd-size-4": lambda d: set_size(d, b"mfhd", 4),
    "A-mfhd-size-0": lambda d: set_size(d, b"mfhd", 0),
    "A+E-frag-0015": frag_0015,
    "E-top-level-size-0-unknown": mdat_header_zeroed,
}


@pytest.mark.parametrize("name", list(LOSES_A_FRAGMENT))
def test_f30_rules_b_e_a_a_lost_fragment_is_damage_not_a_shorter_recording(data_dir, name):
    data = make_media(data_dir, seconds=6, movflags=FRAGMENTED)
    with pytest.raises(I.ImportRefused, match="damaged"):
        decode(data_dir, LOSES_A_FRAGMENT[name](data))


def test_f30_rule_b_a_64_bit_size_with_no_room_for_it_is_damage(data_dir):
    """Size 1 with fewer than 16 bytes left in its traf: FFmpeg stops reading that traf there, and the importer
    used to read the 64-bit size from beyond the traf."""
    data = make_media(data_dir, seconds=6, movflags=FRAGMENTED)
    with pytest.raises(I.ImportRefused, match="damaged"):
        decode(data_dir, append_to_traf(data, bytes.fromhex("00000001") + b"free" + bytes(4)))


@pytest.mark.parametrize("name", ["trun-size-0-frag-0264", "traf-size-0", "last-mdat-size-0", "padding-box-in-traf",
                                  "plain-file-trailing-size-0"])
def test_f30_rules_b_e_a_leave_readable_shapes_alone(data_dir, name):
    """A size of 0 on a last box reaches its parent's end, where it ends anyway, so FFmpeg loses nothing: frag-0264's
    last trun, the moof's last traf and the last mdat. Rule E is for fragmented files only."""
    data = make_media(data_dir, seconds=6, movflags=None if name.startswith("plain") else FRAGMENTED)
    changed = {
        "trun-size-0-frag-0264": lambda d: set_size(d, b"trun", 0),
        "traf-size-0": lambda d: set_size(d, b"traf", 0),
        "last-mdat-size-0": lambda d: zero(d, [at for at, _, kind in top_boxes(d) if kind == b"mdat"][-1], 4),
        "padding-box-in-traf": lambda d: append_to_traf(d, bytes.fromhex("00000010") + b"free" + bytes(8)),
        "plain-file-trailing-size-0": lambda d: d + bytes(8),
    }[name](data)
    assert changed != data
    assert decode(data_dir, changed)[0] / 16000 == pytest.approx(6, abs=0.1)


def test_l10_the_fragment_check_reuses_the_moov_it_is_given(data_dir, monkeypatch):
    """L10: the moov is found once for the fragment checks, not walked for again over the whole file."""
    data = make_media(data_dir, seconds=6, movflags=FRAGMENTED)
    walks, find = [], I._find_moov
    monkeypatch.setattr(I, "_find_moov", lambda sealed: walks.append(1) or find(sealed))
    with I.SealedTempFile(data_dir) as sealed:
        sealed.append(data)
        assert I.decode_media_into(sealed, Writer()) / 16000 == pytest.approx(6, abs=0.1)
    assert len(walks) == 2  # the moov check and is_fragmented; check_fragments no longer walks for its own
