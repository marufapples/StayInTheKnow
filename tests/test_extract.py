from datetime import datetime, timezone

from sitk.extract import extract

POSTED = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)  # a Tuesday


def test_full_event_caption():
    info = extract(
        "Pop-up market this Saturday Oct 10th 2pm-7pm 📍 Riverside Park. Free entry, link in bio for RSVP!",
        POSTED,
    )
    assert info.is_event
    assert info.when.startswith("2026-10-10")
    assert "Riverside Park" in info.venue
    assert info.price.lower() == "free"


def test_tonight_with_time():
    info = extract("Live music tonight, doors at 8pm. $10 at the door", POSTED)
    assert info.is_event
    assert info.when.startswith("2026-10-06T20")
    assert info.price == "$10"


def test_plain_post_is_not_event():
    info = extract("Throwback to a great summer with the crew ☀️", POSTED)
    assert not info.is_event
    assert info.when is None


def test_empty_caption():
    assert not extract("", POSTED).is_event


def test_address_venue():
    info = extract("Open mic Friday 7pm at 123 Main St. Sign up at the door", POSTED)
    assert info.is_event
    assert info.venue
