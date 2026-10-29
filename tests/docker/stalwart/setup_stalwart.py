# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Set up the Stalwart fixture for the integration tests, and stop on the first error.

Waits for Stalwart, then through its admin JMAP API lets weak passwords,
lifts its rate limits and its bans for failed logins and abuse (the tests
make many requests quickly, and some log in with a wrong password on
purpose), reloads its settings so they apply, creates the domain
``example.org`` and the user ``testuser``, and checks that the user can
reach the JMAP session. Run by ``start.sh``; it needs nothing beyond
Python's standard library. The integration tests load it to change
Stalwart's settings through :func:`admin`, :func:`set_singleton` and
:func:`reload_settings`.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any

PORT = 8809
API = f"http://127.0.0.1:{PORT}/jmap"
SESSION = f"http://127.0.0.1:{PORT}/.well-known/jmap"
ADMIN = ("admin", "adminpass")
# The admin account every management call names.
ADMIN_ACCOUNT = "d333333"
DOMAIN = "example.org"
USER = ("testuser", "testcaldav")
# How long to wait for Stalwart, and for the new user to reach JMAP, in seconds.
WAIT = 60
# Stalwart's limits on requests, and on failed logins and abuse before a ban, lifted.
UNLIMITED = {"count": 1_000_000, "period": 60_000}
# How many settings a reload may reject before setup gives up.
RELOAD_TRIES = 40


def _login(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def admin(method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one management method; a method error stops the setup."""
    body = {
        "using": ["urn:ietf:params:jmap:core"],
        "methodCalls": [[method, {"accountId": ADMIN_ACCOUNT, **arguments}, "0"]],
    }
    request = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={"Authorization": _login(*ADMIN), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as answer:
        name, result, _ = json.load(answer)["methodResponses"][0]
    if name == "error":
        raise SystemExit(f"{method} failed: {result}")
    return dict(result)


def set_singleton(method: str, values: dict[str, Any]) -> None:
    """Set a settings object, which exists once, creating it the first time."""
    result = admin(method, {"update": {"singleton": values}})
    if "singleton" not in (result.get("updated") or {}):
        result = admin(method, {"create": {"s": values}})
        if "s" not in (result.get("created") or {}):
            raise SystemExit(f"{method} failed: {result}")


def reload_settings() -> None:
    """Apply the settings; a setting in the image's defaults the reload rejects is removed."""
    for _ in range(RELOAD_TRIES):
        result = admin("x:Action/set", {"create": {"r": {"@type": "ReloadSettings"}}})
        if "r" in (result.get("created") or {}):
            return
        rejected = result["notCreated"]["r"]["objectId"]
        admin(f"x:{rejected['object']}/set", {"destroy": [rejected["id"]]})
    raise SystemExit("Stalwart never reloaded its settings")


def _created_or_found(method: str, values: dict[str, Any], find: str) -> str:
    """Create a record, or find the one already there by its ``find`` member; return its id."""
    result = admin(method, {"create": {"n": values}})
    if "n" in (result.get("created") or {}):
        return str(result["created"]["n"]["id"])
    error = result["notCreated"]["n"]
    if error.get("type") != "primaryKeyViolation":
        raise SystemExit(f"{method} failed: {error}")
    get = method.replace("/set", "/get")
    found = [r for r in admin(get, {"ids": None})["list"] if r.get(find) == values[find]]
    return str(found[0]["id"])


def _wait_for_session(login: str | None) -> dict[str, Any]:
    """The JMAP session, once Stalwart answers it; without a login, any answer will do."""
    deadline = time.monotonic() + WAIT
    while True:
        request = urllib.request.Request(SESSION, headers={"Authorization": login or ""})
        try:
            with urllib.request.urlopen(request, timeout=5) as answer:
                return dict(json.load(answer))
        except urllib.error.HTTPError as exc:
            if login is None and exc.code in (401, 307):
                return {}
        except (urllib.error.URLError, ConnectionError, json.JSONDecodeError):
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"Stalwart didn't answer {SESSION} within {WAIT} seconds")
        time.sleep(1)


def main() -> None:
    _wait_for_session(None)
    set_singleton("x:Authentication/set", {"minPasswordStrength": "zero"})
    set_singleton(
        "x:Http/set", {"rateLimitAnonymous": UNLIMITED, "rateLimitAuthenticated": UNLIMITED}
    )
    set_singleton("x:Security/set", {"authBanRate": UNLIMITED, "abuseBanRate": UNLIMITED})
    reload_settings()
    domain = _created_or_found("x:Domain/set", {"name": DOMAIN}, "name")
    user, password = USER
    _created_or_found(
        "x:Account/set",
        {
            "@type": "User",
            "name": user,
            "domainId": domain,
            "credentials": {"0": {"@type": "Password", "secret": password}},
        },
        "name",
    )
    session = _wait_for_session(_login(f"{user}@{DOMAIN}", password))
    if "urn:ietf:params:jmap:calendars" not in session.get("capabilities", {}):
        raise SystemExit(f"{user}@{DOMAIN} has no JMAP calendars: {session}")
    print(f"Stalwart is ready: {user}@{DOMAIN} / {password} at {SESSION}")


if __name__ == "__main__":
    main()
