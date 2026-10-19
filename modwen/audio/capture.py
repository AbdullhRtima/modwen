"""Audio capture sources. Each source delivers mono float32 16 kHz frames to a callback.

* ``MicSource``          - default (or chosen) microphone via PortAudio (sounddevice).
* ``MacTapSource``       - macOS 14.2+ system audio via Core Audio process taps (Swift helper ``modwen-tap``).
* ``WasapiLoopbackSource`` - Windows system audio via WASAPI loopback (pyaudiowpatch).
* ``PulseLoopbackSource``  - Linux system audio via the PulseAudio/PipeWire monitor (soundcard).
* ``DeviceSource``       - any input device, e.g. a "BlackHole 2ch" loopback device on macOS.
* ``FileSource``         - plays an audio file as if it were live (testing / demos).
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from modwen.audio import SAMPLE_RATE
from modwen.audio.chunks import load_audio_file, to_mono_16k

log = logging.getLogger(__name__)

FrameCallback = Callable[[str, np.ndarray], None]

NATIVE_DIR = Path(__file__).resolve().parent.parent / "native" / "macos"


class CaptureError(RuntimeError):
    pass


class CaptureSource:
    channel: str = "mic"
    description: str = ""
    realtime = True  # delivers audio at the speed of the clock (the recorder fills gaps it leaves)

    def start(self, callback: FrameCallback) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


# ----------------------------------------------------------------------------------------------
class DeviceSource(CaptureSource):
    """Read an input device with sounddevice (PortAudio)."""

    def __init__(self, channel: str, device: str | int | None = None):
        self.channel = channel
        self.device = device
        self._stream = None

    def _resolve(self):
        import sounddevice as sd

        if self.device is None:
            idx = sd.default.device[0]
            return idx, sd.query_devices(idx)
        if isinstance(self.device, int):
            return self.device, sd.query_devices(self.device)
        want = str(self.device).lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and want in d["name"].lower():
                return i, d
        raise CaptureError(f"Input device not found: {self.device}")

    def start(self, callback: FrameCallback) -> None:
        import sounddevice as sd

        idx, info = self._resolve()
        rate = int(info["default_samplerate"]) or 48000
        channels = max(1, min(2, int(info["max_input_channels"])))
        self.description = f"{info['name']} @ {rate} Hz"
        log.info("Opening %s for channel %s", self.description, self.channel)

        def cb(indata, frames, time_info, status):  # PortAudio thread
            if status:  # e.g. input overflow: audio was lost; the recorder pads the gap to keep channels in sync
                log.info("audio status %s: %s", self.channel, status)
            callback(self.channel, to_mono_16k(indata.copy(), rate))

        self._stream = sd.InputStream(device=idx, channels=channels, samplerate=rate, dtype="float32",
                                      blocksize=int(rate * 0.1), callback=cb)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class MicSource(DeviceSource):
    def __init__(self, device: str | int | None = None):
        super().__init__("mic", device)


# ----------------------------------------------------------------------------------------------
def mac_tap_binary() -> Path | None:
    for cand in (NATIVE_DIR / "build" / "modwen-tap", Path(sys.prefix) / "bin" / "modwen-tap"):
        if cand.exists():
            return cand
    found = shutil.which("modwen-tap")
    return Path(found) if found else None


class MacTapSource(CaptureSource):
    """System audio on macOS 14.2+ through a Core Audio process tap.

    The helper writes interleaved float32 PCM to stdout and a ``FORMAT <rate> <channels>`` line to stderr.
    """

    channel = "system"

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self, callback: FrameCallback) -> None:
        exe = mac_tap_binary()
        if not exe:
            raise CaptureError(
                "System-audio helper not built. Run: bash modwen/native/macos/build.sh "
                "(or set capture.system_backend: device with a BlackHole device)."
            )
        self._proc = subprocess.Popen([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        header = self._proc.stderr.readline().decode(errors="replace").strip() if self._proc.stderr else ""
        if not header.startswith("FORMAT"):
            err = header + (self._proc.stderr.read().decode(errors="replace") if self._proc.stderr else "")
            self.stop()
            raise CaptureError(f"modwen-tap failed to start: {err.strip() or 'no output'}. "
                               "Grant 'System Audio Recording' permission in System Settings > Privacy & Security.")
        _, rate_s, ch_s = header.split()[:3]
        rate, channels = int(float(rate_s)), int(ch_s)
        self.description = f"Core Audio tap @ {rate} Hz x{channels}"
        self._running = True
        frame_bytes = 4 * channels
        block = int(rate * 0.1) * frame_bytes

        def reader():
            assert self._proc and self._proc.stdout
            buf = b""
            while self._running:
                data = self._proc.stdout.read(block)
                if not data:
                    break
                buf += data
                usable = len(buf) - (len(buf) % frame_bytes)
                if usable:
                    arr = np.frombuffer(buf[:usable], dtype=np.float32).reshape(-1, channels)
                    buf = buf[usable:]
                    callback(self.channel, to_mono_16k(arr, rate))
            log.info("modwen-tap reader stopped")

        self._thread = threading.Thread(target=reader, name="mac-tap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


# ----------------------------------------------------------------------------------------------
class WasapiLoopbackSource(CaptureSource):
    """Windows: capture whatever plays on the default output device (Meet in Chrome, Teams desktop...)."""

    channel = "system"

    def __init__(self):
        self._pa = None
        self._stream = None

    def start(self, callback: FrameCallback) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:
            raise CaptureError("pyaudiowpatch not installed (uv sync --extra windows)") from exc
        self._pa = pyaudio.PyAudio()
        try:
            dev = self._pa.get_default_wasapi_loopback()
        except Exception as exc:
            raise CaptureError(f"No WASAPI loopback device: {exc}") from exc
        rate = int(dev["defaultSampleRate"])
        channels = int(dev["maxInputChannels"]) or 2
        self.description = f"WASAPI loopback: {dev['name']} @ {rate} Hz"

        def cb(in_data, frame_count, time_info, status):
            arr = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels)
            callback(self.channel, to_mono_16k(arr, rate))
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
                                     input_device_index=dev["index"], frames_per_buffer=int(rate * 0.1),
                                     stream_callback=cb)
        self._stream.start_stream()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa:
            self._pa.terminate()
            self._pa = None


class PulseLoopbackSource(CaptureSource):
    channel = "system"

    def __init__(self):
        self._running = False
        self._thread = None

    def start(self, callback: FrameCallback) -> None:
        try:
            import soundcard as sc
        except ImportError as exc:
            raise CaptureError("soundcard not installed (uv sync --extra linux)") from exc
        mic = sc.get_microphone(id=str(sc.default_speaker().name), include_loopback=True)
        self.description = f"Monitor of {sc.default_speaker().name}"
        self._running = True
        rate = 48000

        def loop():
            with mic.recorder(samplerate=rate, channels=2) as rec:
                while self._running:
                    callback(self.channel, to_mono_16k(rec.record(numframes=rate // 10), rate))

        self._thread = threading.Thread(target=loop, name="pulse-loopback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False


# ----------------------------------------------------------------------------------------------
class FileSource(CaptureSource):
    """Replays an audio file in (accelerated) real time as a capture channel."""

    def __init__(self, path: str | Path, channel: str = "system", speed: float = 1.0):
        self.path = Path(path)
        self.channel = channel
        self.speed = speed
        self.realtime = speed == 1.0
        self.description = f"file {self.path.name} x{speed}"
        self._running = False
        self._thread = None
        self.finished = threading.Event()

    def start(self, callback: FrameCallback) -> None:
        audio = load_audio_file(self.path)
        self._running = True
        block = SAMPLE_RATE // 10

        def loop():
            t0 = time.monotonic()
            for i in range(0, audio.size, block):
                if not self._running:
                    break
                callback(self.channel, audio[i : i + block])
                target = t0 + (i + block) / SAMPLE_RATE / self.speed
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            self.finished.set()

        self._thread = threading.Thread(target=loop, name=f"file-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False


# ----------------------------------------------------------------------------------------------
def system_source(cfg) -> CaptureSource | None:
    backend = cfg.capture.system_backend
    system = platform.system()
    if backend == "none":
        return None
    if backend == "device" or (backend == "auto" and cfg.capture.system_device):
        if not cfg.capture.system_device:
            raise CaptureError("capture.system_backend is 'device' but capture.system_device is not set")
        return DeviceSource("system", cfg.capture.system_device)
    if backend == "coreaudio_tap" or (backend == "auto" and system == "Darwin"):
        return MacTapSource()
    if backend == "wasapi" or (backend == "auto" and system == "Windows"):
        return WasapiLoopbackSource()
    if backend == "pulse" or (backend == "auto" and system == "Linux"):
        return PulseLoopbackSource()
    raise CaptureError(f"No system-audio backend for {system}")


def list_input_devices() -> list[dict]:
    try:
        import sounddevice as sd

        default_in = sd.default.device[0]
        return [{"index": i, "name": d["name"], "channels": d["max_input_channels"],
                 "rate": d["default_samplerate"], "default": i == default_in}
                for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception as exc:
        return [{"error": str(exc)}]


def is_bluetooth_name(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("airpods", "bluetooth", "hands-free", "headset", "buds", "bt "))
