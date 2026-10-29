# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Set up the Cyrus fixture for the integration tests, and stop on the first error.

Waits until user1's JMAP session offers calendars, which also creates each
user's calendar home, then lets each user schedule through their Outbox,
so an event with participants can invite another user. Run by
``start.sh``; it needs nothing beyond Python's standard library.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

SESSION = "http://127.0.0.1:8802/.well-known/jmap"
CONTAINER = "calendaring-sync-cyrus"
USERS = ("user1", "user2", "user3", "user4", "user5")
# The image's password for every user.
PASSWORD = "x"
# How long to wait for Cyrus's JMAP session, in seconds.
WAIT = 120


def _session(user: str) -> dict[str, Any]:
    """The user's JMAP session, or an empty one while Cyrus isn't ready."""
    login = base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(SESSION, headers={"Authorization": f"Basic {login}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:
            return dict(json.load(answer))
    except (urllib.error.URLError, ConnectionError, json.JSONDecodeError):
        return {}


def main() -> None:
    deadline = time.monotonic() + WAIT
    while "urn:ietf:params:jmap:calendars" not in _session(USERS[0]).get("capabilities", {}):
        if time.monotonic() > deadline:
            raise SystemExit(f"Cyrus offered no JMAP calendars at {SESSION} within {WAIT} seconds")
        time.sleep(2)
    # A user's calendar mailboxes exist only after their first session.
    for user in USERS[1:]:
        _session(user)
    rights = "".join(
        f"sam user.{user}.#calendars.Outbox {user} lrswipkxtecdan789\r\n" for user in USERS
    )
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "/usr/cyrus/bin/cyradm",
            "--auth",
            "PLAIN",
            "-u",
            "admin",
            "-w",
            "admin",
            "--notls",
            "--port",
            "8143",
            "localhost",
        ],
        input=rights.encode(),
        check=True,
        capture_output=True,
    )
    print(f"Cyrus is ready: {', '.join(USERS)} / {PASSWORD} at {SESSION}")


if __name__ == "__main__":
    main()
