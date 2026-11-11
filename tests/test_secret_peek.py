"""SecretStore.peek: read an existing secret without writing anything (fake keyring, synthetic data only)."""

from __future__ import annotations

import json
import sys

import pytest

from modwen import crypto
from modwen.config import Config


class FakeKeyring:
    def __init__(self, items=None, broken=False):
        self.items = dict(items or {})
        self.broken = broken
        self.writes: list[tuple] = []

    def get_password(self, service, name):
        if self.broken:
            raise RuntimeError("no backend")
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.writes.append((service, name, value))
        self.items[(service, name)] = value


@pytest.fixture()
def fake(monkeypatch):
    def install(**kw):
        k = FakeKeyring(**kw)
        monkeypatch.setitem(sys.modules, "keyring", k)
        return k

    monkeypatch.setattr(crypto, "_HANDED", {})
    return install


def test_peek_reads_the_legacy_service_without_migrating_it(data_dir, monkeypatch, fake):
    monkeypatch.setenv("MODWEN_KEYSTORE", "keyring")
    k = fake(items={(crypto.LEGACY_SERVICE, "master-key"): "old"})
    assert crypto.SecretStore(Config()).peek("master-key") == "old"
    assert k.writes == []  # get() would copy it to the new service name; peek must not
    assert crypto.SecretStore(Config()).get("master-key") == "old" and k.writes  # the difference is real


def test_peek_prefers_the_current_service_and_returns_none_when_missing(data_dir, monkeypatch, fake):
    monkeypatch.setenv("MODWEN_KEYSTORE", "keyring")
    k = fake(items={(crypto.SERVICE, "a"): "new", (crypto.LEGACY_SERVICE, "a"): "old"})
    s = crypto.SecretStore(Config())
    assert s.peek("a") == "new" and s.peek("nothing") is None
    assert k.writes == []


def test_peek_falls_back_to_the_file_when_keyring_is_unusable_and_never_creates_it(data_dir, monkeypatch, fake):
    monkeypatch.delenv("MODWEN_KEYSTORE", raising=False)
    k = fake(broken=True)
    s = crypto.SecretStore(Config())
    assert s.mode == "auto" and s.peek("master-key") is None
    assert sorted(p.name for p in data_dir.iterdir()) == []  # no file created
    (data_dir / ".secrets.json").write_text(json.dumps({"master-key": "from-file"}), encoding="utf-8")
    assert s.peek("master-key") == "from-file" and k.writes == []


def test_peek_uses_a_secret_handed_over_by_the_desktop_app(data_dir, monkeypatch, fake):
    monkeypatch.delenv("MODWEN_KEYSTORE", raising=False)
    k = fake()
    crypto.accept_handed_secrets({"master-key": "handed"})
    assert crypto.SecretStore(Config()).peek("master-key") == "handed" and k.writes == []


def test_peek_in_file_mode_does_not_touch_the_keyring(data_dir, monkeypatch, fake):
    k = fake(items={(crypto.SERVICE, "master-key"): "keychain"})  # the fixture sets MODWEN_KEYSTORE=file
    assert crypto.SecretStore(Config()).peek("master-key") is None and k.writes == []
