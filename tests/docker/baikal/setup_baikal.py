# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Set up the Baïkal fixture for the integration tests, and stop on the first error.

Waits for Baïkal, then, unless the user ``test`` can already reach their
calendar home, runs its install wizard (SQLite, Basic authentication),
signs in to its admin pages, creates that user with the password ``test``,
and checks that they can reach it. Run by
``start.sh``; it needs nothing beyond Python's standard library.
"""

from __future__ import annotations

import base64
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "http://127.0.0.1:8811"
ADMIN_PASSWORD = "adminpass"
USER = ("test", "test")
# How long to wait for Baïkal to answer, in seconds.
WAIT = 60

_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def _open(request: urllib.request.Request | str) -> str:
    with _opener.open(request, timeout=30) as answer:
        return str(answer.read().decode())


def _submit(path: str, fields: dict[str, str]) -> None:
    """Post a form as the page sends it: its hidden fields, then ``fields``, as multipart."""
    page = _open(BASE + path)
    hidden = r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"'
    form = dict(re.findall(hidden, page))
    form.update(fields)
    boundary = uuid.uuid4().hex
    body = (
        "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            for name, value in form.items()
        )
        + f"--{boundary}--\r\n"
    )
    request = urllib.request.Request(
        BASE + path,
        data=body.encode(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _open(request)))
    error = re.search(r"Validation error.{0,200}", text)
    if error:
        raise SystemExit(f"{path}: {error.group(0)}")


def _reach_home() -> None:
    """List the user's calendar home with their login; raises if they can't."""
    user, password = USER
    home = urllib.request.Request(
        f"{BASE}/dav.php/calendars/{user}/",
        method="PROPFIND",
        headers={
            "Depth": "1",
            "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(),
        },
    )
    _open(home)


def _install() -> None:
    """Run the install wizard, sign in as the admin and create the user."""
    _submit(
        "/admin/install/",
        {
            "data[timezone]": "UTC",
            "data[card_enabled]": "1",
            "data[cal_enabled]": "1",
            "data[invite_from]": "noreply@example.org",
            "data[dav_auth_type]": "Basic",
            "data[admin_passwordhash]": ADMIN_PASSWORD,
            "data[admin_passwordhash_confirm]": ADMIN_PASSWORD,
        },
    )
    _submit(
        "/admin/install/",
        {"data[backend]": "sqlite", "data[sqlite_file]": "/var/www/baikal/Specific/db/db.sqlite"},
    )
    login = urllib.parse.urlencode({"auth": "1", "login": "admin", "password": ADMIN_PASSWORD})
    _open(urllib.request.Request(f"{BASE}/admin/", data=login.encode()))
    user, password = USER
    _submit(
        "/admin/?/users/new/1/",
        {
            "data[username]": user,
            "data[displayname]": user,
            "data[email]": f"{user}@example.org",
            "data[password]": password,
            "data[passwordconfirm]": password,
        },
    )


def main() -> None:
    deadline = time.monotonic() + WAIT
    while True:
        try:
            _open(f"{BASE}/admin/install/")
            break
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline:
                raise SystemExit(f"Baïkal didn't answer {BASE} within {WAIT} seconds") from None
            time.sleep(1)
    try:
        # A fixture already set up has the user, and its wizard is gone.
        _reach_home()
    except urllib.error.HTTPError:
        _install()
        _reach_home()
    user, password = USER
    print(f"Baïkal is ready: {user} / {password} at {BASE}/dav.php/calendars/{user}/")


if __name__ == "__main__":
    main()
