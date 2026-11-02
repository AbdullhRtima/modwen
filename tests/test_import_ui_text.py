"""The UI shows an import refusal only if ``serverText.ts`` knows it, and the general message otherwise. There is no
UI test framework, so these tests read the TypeScript: a reworded engine string must not silently lose its message."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from modwen.audio import importer

UI = Path(__file__).resolve().parent.parent / "ui" / "src"
SOURCE = (UI / "serverText.ts").read_text(encoding="utf-8")
BLOCK = SOURCE[SOURCE.index("const REFUSALS"):SOURCE.index("export function importMessage")]
PATTERNS = [re.compile(p) for p in re.findall(r"\[/(\^.*?\$)/,", BLOCK)]


def known(message: str) -> bool:
    return any(p.search(message) for p in PATTERNS)


def test_every_pattern_was_read():
    assert len(PATTERNS) == 6


@pytest.mark.parametrize("name", ["NOT_SUPPORTED", "NO_AUDIO", "DAMAGED", "EMPTY", "TOO_BIG"])
def test_each_refusal_has_a_pattern(name):
    assert known(getattr(importer, name))


@pytest.mark.parametrize("hours", [6.04, 7, 7.5, 10, 11, 96.2, 2_562_047_788.0])
def test_the_length_refusal_has_a_pattern_at_any_length(hours):
    assert known(importer.too_long_message(hours))


def test_the_length_pattern_reads_a_plain_number_only():
    assert not known("This recording is 2.56205e+09 hours long. Imports are limited to 6 hours. "
                     "Trim or split it, then import it again.")


def test_each_pattern_matches_exactly_one_refusal():
    messages = [importer.NOT_SUPPORTED, importer.NO_AUDIO, importer.DAMAGED, importer.EMPTY, importer.TOO_BIG,
                importer.too_long_message(7)]
    assert sorted(sum(bool(p.search(m)) for m in messages) for p in PATTERNS) == [1] * 6


def test_the_general_failure_is_not_a_refusal():
    assert not known(importer.IMPORT_FAILED)  # the UI's own importFailed shows, in the interface language


def test_upload_limit_matches_the_engine():
    from modwen.api import MAX_UPLOAD_BYTES

    assert MAX_UPLOAD_BYTES == 8 * 1024 ** 3
    assert "MAX_IMPORT_BYTES = 8 * 1024 ** 3" in (UI / "api.ts").read_text(encoding="utf-8")
