"""Heuristic event detection from post captions. Free, local, no AI."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime

import dateparser

MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
WEEKDAYS = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day)?"

DATE_PATTERNS = [
    re.compile(rf"\b{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?", re.I),  # Oct 12th, 2026
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTHS}", re.I),         # 12th of October
    re.compile(rf"\b(?:this\s+|next\s+)?{WEEKDAYS}\b(?:,?\s+{MONTHS}\s+\d{{1,2}})?", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),                                 # 10/12
    re.compile(r"\b(?:tonight|tomorrow)\b", re.I),
]
TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b(?:[01]?\d|2[0-3]):[0-5]\d\b", re.I)
PRICE_RE = re.compile(r"\bfree\b|\$\s?\d+|\bno cover\b|\bcover\b", re.I)
KEYWORDS_RE = re.compile(
    r"\b(?:tonight|doors|rsvp|tickets?|lineup|line-up|live music|pop[- ]?up|party|event|open mic|"
    r"workshop|festival|market|meet[- ]?up|join us|pull up|come through|showcase|performance|"
    r"link in bio|dj|concert|screening)\b",
    re.I,
)
VENUE_RES = [
    re.compile(r"\d{2,5}\s+[A-Z][\w.]*(?:\s+[A-Z][\w.]*)*\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Dr|Drive|Ln|Way|Pl|Place)\b\.?"),
    re.compile(r"📍\s*([^\n#]{3,60})"),
    re.compile(r"(?:^|\s)@\s+([A-Z][^\n#,.!]{2,40})"),           # "@ The Venue"
    re.compile(r"\b(?:at|location:)\s+(?:the\s+)?([A-Z][\w'&]*(?:\s+[A-Z][\w'&]*){0,4})"),
]


@dataclass
class EventInfo:
    is_event: bool
    confidence: float
    when: str | None = None       # ISO datetime if parsed
    when_text: str | None = None  # raw phrase from the caption
    venue: str | None = None
    price: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _find_date(caption: str, posted_at: datetime) -> tuple[str | None, datetime | None]:
    time_match = TIME_RE.search(caption)
    for pattern in DATE_PATTERNS:
        m = pattern.search(caption)
        if not m:
            continue
        phrase = m.group(0)
        to_parse = f"{phrase} {time_match.group(0)}" if time_match else phrase
        parsed = dateparser.parse(
            re.sub(r"tonight", "today", to_parse, flags=re.I),  # dateparser doesn't know "tonight"
            languages=["en"],
            settings={
                "RELATIVE_BASE": posted_at.replace(tzinfo=None),
                "PREFER_DATES_FROM": "future",
                "PREFER_DAY_OF_MONTH": "first",
            },
        )
        if parsed:
            return to_parse, parsed
    return None, None


def _find_venue(caption: str) -> str | None:
    for pattern in VENUE_RES:
        m = pattern.search(caption)
        if m:
            venue = (m.group(1) if m.groups() else m.group(0)).strip(" .,-")
            if len(venue) >= 3:
                return venue
    return None


def extract(caption: str, posted_at: datetime) -> EventInfo:
    caption = caption or ""
    when_text, when = _find_date(caption, posted_at)
    has_time = bool(TIME_RE.search(caption))
    price_m = PRICE_RE.search(caption)
    venue = _find_venue(caption)
    keywords = {k.lower() for k in KEYWORDS_RE.findall(caption)}

    score = 0
    score += 2 if when else 0
    score += 1 if has_time else 0
    score += min(len(keywords), 2)
    score += 1 if price_m else 0
    score += 1 if venue else 0

    is_event = score >= 3 and (when is not None or has_time)
    return EventInfo(
        is_event=is_event,
        confidence=round(min(score / 6, 1.0), 2),
        when=when.isoformat(timespec="minutes") if when else None,
        when_text=when_text,
        venue=venue,
        price=price_m.group(0) if price_m else None,
    )
