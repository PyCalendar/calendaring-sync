# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a server adds to or takes off an event you organize, and if it's still your own write.

Usage:
  schedule_parameters.py SERVER BASE_URL HOME USER PASSWORD ADDRESS

Creates an event organized by ADDRESS, the account's own address, with three attendees: the
organizer, one marked SCHEDULE-FORCE-SEND (RFC 6638 section 7.2) and one marked
SCHEDULE-AGENT=CLIENT (section 7.1); then changes its summary at the same SEQUENCE. After each
write it GETs the event and logs every content line sent but not stored, and stored but not
sent, and checks that the CalDAV adapter's fingerprint (calendaring-sync must be installed)
is the same for both.
"""

import sys
import uuid

from calendaring_sync.adapters.caldav import _fields

from _common import DAV, check, finish, result


def event(uid, summary, address):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calendaring-sync probes//EN\r\n"
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261101T100000Z\r\nDTSTART:20261201T100000Z\r\n"
        f"DTEND:20261201T110000Z\r\nSEQUENCE:0\r\nSUMMARY:{summary}\r\n"
        f"ORGANIZER;CN=Organizer:mailto:{address}\r\n"
        f"ATTENDEE;CN=Organizer;PARTSTAT=ACCEPTED:mailto:{address}\r\n"
        "ATTENDEE;CN=Asked;SCHEDULE-FORCE-SEND=REQUEST;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:"
        "mailto:asked@example.net\r\n"
        "ATTENDEE;CN=Client;SCHEDULE-AGENT=CLIENT;PARTSTAT=NEEDS-ACTION:mailto:client@example.net\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def lines(text):
    return {line for line in text.replace("\r\n ", "").replace("\n ", "").splitlines() if line}


def main(server, base, home, user, password, address):
    dav = DAV(base, f"{home}schedule/", user, password)
    dav.reset()
    uid = f"schedule-{uuid.uuid4()}"
    headers = {"If-None-Match": "*"}
    for step, summary in (("create", "plan"), ("change", "moved")):
        sent = event(uid, summary, address)
        dav.put(f"{step} PUT", "meeting.ics", sent, headers)
        stored = dav.request(f"{step} GET", "GET", dav.href("meeting.ics")).text
        headers = {"If-Match": dav.etag("meeting.ics") or "*"}
        for line in sorted(lines(sent) - lines(stored)):
            result(server, f"{step}: sent, not stored", line)
        for line in sorted(lines(stored) - lines(sent)):
            result(server, f"{step}: stored, not sent", line)
        check(
            server,
            f"{step}: the server's copy fingerprints as what was sent",
            _fields(stored.encode()).fingerprint == _fields(sent.encode()).fingerprint,
        )
    finish()


if __name__ == "__main__":
    main(*sys.argv[1:])
