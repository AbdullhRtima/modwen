"""The shared diarization worker pool: reset race, timeout and cancellation (no real processes started)."""

from __future__ import annotations

import concurrent.futures as cf
import threading
from concurrent.futures.process import BrokenProcessPool

import pytest

from modwen import diarize


def _done(value=None, exc=None):
    f = cf.Future()
    f.set_exception(exc) if exc else f.set_result(value)
    return f


class GoodPool:
    def shutdown(self, *a, **k):
        pass

    def submit(self, fn, *args):
        return _done("ok")


class Factory:
    """Stands in for ProcessPoolExecutor: counts how many pools get started."""

    def __init__(self):
        self.created = 0
        self.event = threading.Event()

    def __call__(self, *a, **k):
        self.created += 1
        self.event.set()
        return GoodPool()


@pytest.fixture()
def fake_pool(monkeypatch):
    factory = Factory()
    monkeypatch.setattr(cf, "ProcessPoolExecutor", factory)
    monkeypatch.setattr(diarize.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(diarize, "_POOL", None)
    yield factory
    monkeypatch.setattr(diarize, "_POOL", None)


def test_two_threads_seeing_one_broken_pool_start_one_new_pool(fake_pool, monkeypatch):
    lock, entered, both_in = threading.Lock(), [0], threading.Event()

    class BrokenPool:
        def submit(self, fn, *args):
            with lock:
                entered[0] += 1
                n = entered[0]
            if n == 1:
                both_in.wait(5)  # the first caller fails only once the second holds the same dead pool
            else:
                both_in.set()
                fake_pool.event.wait(5)  # the second fails only after the first has started a new pool
            return _done(exc=BrokenProcessPool("dead"))

    monkeypatch.setattr(diarize, "_POOL", BrokenPool())
    results = []
    threads = [threading.Thread(target=lambda: results.append(diarize._call(len, "x"))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == ["ok", "ok"]
    assert fake_pool.created == 1  # without the compare-and-swap the second reset orphans the first new pool


def test_call_raises_when_the_pool_stays_broken(fake_pool, monkeypatch):
    class Dead:
        def shutdown(self, *a, **k):
            pass

        def submit(self, fn, *args):
            return _done(exc=BrokenProcessPool("dead"))

    monkeypatch.setattr(cf, "ProcessPoolExecutor", lambda *a, **k: Dead())
    monkeypatch.setattr(diarize, "_POOL", Dead())
    with pytest.raises(BrokenProcessPool):
        diarize._call(len, "x")


def test_call_timeout_cancels_a_job_that_has_not_started(monkeypatch):
    pending = cf.Future()

    class Busy:
        def submit(self, fn, *args):
            return pending

    monkeypatch.setattr(diarize, "_POOL", Busy())
    with pytest.raises(TimeoutError):
        diarize._call(len, "x", timeout=0.05)
    assert pending.cancelled()
    monkeypatch.setattr(diarize, "_POOL", None)


def test_call_surfaces_cancellation_instead_of_hanging(monkeypatch):
    cancelled = cf.Future()
    cancelled.cancel()

    class Shut:
        def submit(self, fn, *args):
            return cancelled

    monkeypatch.setattr(diarize, "_POOL", Shut())
    with pytest.raises(cf.CancelledError):
        diarize._call(len, "x", timeout=1)
    monkeypatch.setattr(diarize, "_POOL", None)
