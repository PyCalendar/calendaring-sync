# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The events the tests and benchmarks build, one builder per format."""

from __future__ import annotations

from typing import Any


def ics(uid: str, summary: str, *lines: str) -> bytes:
    """A small event in iCalendar form, with ``lines`` added to its ``VEVENT``."""
    added = "".join(f"{line}\r\n" for line in lines)
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calendaring-sync tests//EN\r\n"
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261001T000000Z\r\n"
        f"DTSTART:20261101T100000Z\r\nSUMMARY:{summary}\r\n{added}END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def event(uid: str, summary: str, calendar: str, **members: Any) -> dict[str, Any]:
    """A small event in JSCalendar form, with ``members`` added or replaced."""
    return {
        "@type": "Event",
        "version": "2.0",
        "uid": uid,
        "title": summary,
        "start": "2026-11-02T10:00:00",
        "timeZone": "Etc/UTC",
        "duration": "PT1H",
        "calendarIds": {calendar: True},
        **members,
    }
