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


def test_the_ui_knows_the_importing_status():
    """``GET /api/meetings/{id}`` returns an importing row (lists hide it): the type names it and the meeting page
    shows it as working, so a direct link doesn't open an empty, finished-looking page with Retry or edits."""
    api_ts = (UI / "api.ts").read_text(encoding="utf-8")
    union = re.search(r'status: ("[a-z]+"(?: \| "[a-z]+")*);', api_ts)
    assert union and '"importing"' in union.group(1)
    review = (UI / "pages" / "Review.tsx").read_text(encoding="utf-8")
    working = re.search(r"const processing = (.*);", review)
    assert working and 'meeting.status === "importing"' in working.group(1)


def test_a_failed_import_is_what_the_engine_says_it_is():
    """The engine owns the rule (``failed_import`` on every meeting it lists or returns); the UI must not keep its own
    looser one, or the two disagree on Retry and Import again."""
    api_ts = (UI / "api.ts").read_text(encoding="utf-8")
    assert "failed_import?: boolean;" in api_ts
    assert re.search(r"export const isFailedImport = \(m: Meeting\) => m\.failed_import === true;", api_ts)
    assert '"import:"' not in api_ts


def test_import_errors_stay_12_seconds_and_other_toasts_keep_their_time():
    app = (UI / "App.tsx").read_text(encoding="utf-8")
    assert 'ms ?? (kind === "error" ? 6000 : 2200)' in app  # every other toast: errors 6 s, success 2.2 s
    calls = re.findall(r"toast\(([^;]*?), \"error\", 12000\)", app)
    assert calls == ['t("importTooBig")', "importMessage(e, t, lang)"]  # exactly the two import-error toasts
    assert app.count("12000") == 2


def test_the_arabic_hours_plural_follows_the_last_two_digits():
    """3 to 10 take ساعات, and so do 103 to 110; everything else that can occur takes ساعة."""
    assert 'Number.isInteger(h) && h % 100 >= 3 && h % 100 <= 10 ? "ساعات" : "ساعة"' in SOURCE
