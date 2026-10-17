"""Resolve spoken deadlines (English + Arabic) to Gregorian dates relative to the meeting date.

Only unambiguous phrases are resolved (today, tomorrow, weekdays, "in N days", explicit dates).
Vague ones ("next week", "soon") return None, so the UI shows the spoken words instead of a guess.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EN_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
               "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
AR_WEEKDAYS = {
    "الاثنين": 0, "الإثنين": 0, "الاتنين": 0, "اثنين": 0, "التنين": 0,
    "الثلاثاء": 1, "الثلاثا": 1, "التلات": 1, "التلاتاء": 1,
    "الأربعاء": 2, "الاربعاء": 2, "الأربع": 2, "الاربع": 2, "الاربعا": 2,
    "الخميس": 3, "خميس": 3,
    "الجمعة": 4, "الجمعه": 4,
    "السبت": 5, "سبت": 5,
    "الأحد": 6, "الاحد": 6,
}
AR_LETTER = "؀-ۿ"
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
AR_MONTHS = {"يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "مايو": 5, "يونيو": 6, "يوليو": 7,
             "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12}
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
AR_NUMBER_WORDS = {"يومين": 2, "اسبوع": 7, "أسبوع": 7, "اسبوعين": 14, "أسبوعين": 14}


def meeting_date(started_at: str | None, tz: str = "system") -> date:
    zone = None if tz in ("system", "", None) else ZoneInfo(tz)
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at)
            return dt.astimezone(zone).date() if zone or dt.tzinfo else dt.date()
        except ValueError:
            pass
    return datetime.now(zone).date()


def normalize(text: str) -> str:
    t = text.strip().lower().translate(AR_DIGITS)
    return re.sub(r"[ً-ْـ]", "", t)  # strip Arabic diacritics / tatweel


def _next_weekday(base: date, wd: int) -> date:
    days = (wd - base.weekday()) % 7
    return base + timedelta(days=days or 7)


def resolve(raw: str | None, base: date) -> date | None:
    return find_deadline(raw, base)[0]


def find_deadline(raw: str | None, base: date) -> tuple[date | None, str | None]:
    """Return (date, the words that gave it) for the deadline phrase in ``raw``; (None, None) if none."""
    if not raw:
        return None, None
    t = normalize(raw)

    # ISO / numeric dates
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
        except ValueError:
            return None, None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:  # day/month (common in the region)
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else base.year
        y = y + 2000 if y < 100 else y
        try:
            cand = date(y, mo, d)
            return (cand if m.group(3) or cand >= base else date(y + 1, mo, d)), m.group(0)
        except ValueError:
            return None, None

    # Explicit day + month name ("23 Oct", "October 23", "23 أكتوبر")
    for name, mo in list(MONTHS.items()) + list(AR_MONTHS.items()):
        m = re.search(rf"(\d{{1,2}})\s*(?:of\s+)?{name}\b|{name}\.?\s+(\d{{1,2}})\b", t)
        if m:
            d = int(m.group(1) or m.group(2))
            try:
                cand = date(base.year, mo, d)
                return (cand if cand >= base else date(base.year + 1, mo, d)), m.group(0)
            except ValueError:
                return None, None

    # Relative words
    m = re.search(r"day after tomorrow|بعد بكر[ةها]|بعد غد", t)
    if m:
        return base + timedelta(days=2), m.group(0)
    m = re.search(r"\btomorrow\b|بكر[ةها]|بكرا|غدا|غدًا|الغد", t)
    if m:
        return base + timedelta(days=1), m.group(0)
    m = re.search(r"\btoday\b|end of (?:the )?day|\beod\b|اليوم|النهارده|النهاردة|انهارده", t)
    if m:
        return base, m.group(0)

    m = re.search(r"in (\d+) (day|days|week|weeks)\b", t)
    if m:
        n = int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return base + timedelta(days=n), m.group(0)
    m = re.search(r"(?:خلال|بعد)\s+(\d+)\s*(يوم|ايام|أيام|اسبوع|أسبوع|اسابيع|أسابيع)", t)
    if m:
        n = int(m.group(1)) * (7 if "سب" in m.group(2) else 1)
        return base + timedelta(days=n), m.group(0)
    m = re.search(r"(?:خلال|بعد)\s+(يومين|اسبوعين|أسبوعين|اسبوع|أسبوع)", t)
    if m:
        return base + timedelta(days=AR_NUMBER_WORDS[m.group(1)]), m.group(0)

    # Weekdays: the first one mentioned wins.
    hits: list[tuple[int, int, str]] = []
    for name, wd in EN_WEEKDAYS.items():
        m = re.search(rf"\b{name}\b", t)
        if m:
            hits.append((m.start(), wd, m.group(0)))
    for name, wd in AR_WEEKDAYS.items():
        # Whole word only (Arabic letters next to the name mean it's part of another word).
        m = re.search(rf"(?<![{AR_LETTER}]){name}(?![{AR_LETTER}])", t)
        if m:
            hits.append((m.start(), wd, m.group(0)))
    if hits:
        _, wd, phrase = min(hits)
        return _next_weekday(base, wd), phrase
    return None, None


def grounded(phrase: str, text: str) -> bool:
    """True when the deadline words actually occur in the transcript text they were taken from."""
    p, t = normalize(phrase), normalize(text)
    if not p:
        return False
    if p in t:
        return True
    words = [w for w in re.findall(rf"[a-z0-9{AR_LETTER}]+", p) if len(w) > 2 and w not in ("next", "the", "this")]
    return bool(words) and all(w in t for w in words)
