# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Helpers shared by every probe: fixtures, request logging, report parsing, JMAP calls, results."""

import base64
import json
import re
import sys
import urllib.request
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, cast

import requests

if TYPE_CHECKING:
    from caldav.calendarobjectresource import Event
    from caldav.collection import SynchronizableCalendarObjectCollection
    from lxml.etree import _Element

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
XML = {"Content-Type": "application/xml; charset=utf-8"}
ICS = {"Content-Type": "text/calendar; charset=utf-8"}
JMAP_CORE = "urn:ietf:params:jmap:core"
JMAP_CALENDARS = "urn:ietf:params:jmap:calendars"
JMAP_TASKS = "urn:ietf:params:jmap:tasks"
COLLECTION = "<collection>"
QUOT = "&quot;"

_results = []


def result(server, check, text):
    """Record one finding; printed as a RESULT line at the end of the log."""
    _results.append(f"RESULT [{server}] {check}: {text}")


def check(server, label, ok, detail=""):
    """Record a pass/fail assertion as a RESULT line."""
    result(server, f"{'PASS' if ok else 'FAIL'} {label}", detail or ("ok" if ok else "failed"))
    return ok


def finish():
    """Print every recorded result; exit 1 if any check failed."""
    print("\n" + "\n".join(_results))
    sys.exit(1 if any(" FAIL " in r for r in _results) else 0)


def capture(pattern, text, default=None):
    """The first group of the first match, or default when there's no match."""
    m = re.search(pattern, text)
    return m.group(1) if m else default


def ics(uid, summary, dtstamp=True):
    """An event from fixtures/, with its uid and summary filled in."""
    name = "event.ics" if dtstamp else "event-without-dtstamp.ics"
    with open(FIXTURES / name, encoding="utf-8", newline="") as f:
        return Template(f.read()).substitute(uid=uid, summary=summary)


def propfind_body(prop):
    """A PROPFIND body asking for one DAV: property."""
    return (
        f'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:{prop}/></D:prop></D:propfind>'
    )


def multiget_body(hrefs):
    """A calendar-multiget body asking for getetag and calendar-data for each href."""
    refs = "".join(f"<D:href>{h}</D:href>" for h in hrefs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"<D:prop><D:getetag/><C:calendar-data/></D:prop>{refs}</C:calendar-multiget>"
    )


def sync_body(client, token=None):
    """python-caldav's own sync-collection request body for a DAVClient, asking for getetag."""
    return client._build_sync_collection_body(sync_token=token, props=["getetag"])


def caldav_sync_report(client, url, token=None):
    """A sync-collection report sent through python-caldav's DAVClient.report()."""
    return client.report(url, sync_body(client, token).decode(), depth=0)


# python-caldav types these calls as "result or coroutine" to cover its async client too. The
# probes only use the synchronous client, so these helpers state the type it actually returns.


def sync_objects(calendar, **kwargs) -> "SynchronizableCalendarObjectCollection":
    """Calendar.get_objects_by_sync_token() on a synchronous client."""
    return cast(
        "SynchronizableCalendarObjectCollection", calendar.get_objects_by_sync_token(**kwargs)
    )


def add_event(calendar, data) -> "Event":
    """Calendar.add_event() on a synchronous client."""
    return cast("Event", calendar.add_event(data))


def tree(response) -> "_Element":
    """A DAVResponse's parsed XML body."""
    if response.tree is None:
        raise ValueError(f"no XML body in a {response.status} response")
    return response.tree


def outcome(fn):
    """What fn returned, or which exception it raised and that exception's fields."""
    try:
        return f"returned {fn()!r}"[:300]
    # Every exception is caught on purpose: which one a client library raises is the finding.
    except Exception as e:  # noqa: BLE001
        fields = {k: str(v)[:140] for k, v in vars(e).items()}
        return f"raised {type(e).__name__} {fields}"


class DAV:
    """Raw HTTP against one CalDAV calendar, logging every request and response."""

    def __init__(self, base, calendar, user, password):
        self.base, self.calendar, self.auth = base, calendar, (user, password)

    def request(self, label, method, path, body=None, headers=None):
        r = requests.request(
            method,
            self.base + path,
            data=body.encode() if body else None,
            auth=self.auth,
            headers=headers or {},
        )
        print(f"\n===== {label} =====\n>>> {method} {path}")
        for k, v in (headers or {}).items():
            print(f">>> {k}: {v}")
        if body:
            print(">>> " + body.replace("\n", "\n>>> "))
        print(f"<<< {r.status_code} {r.reason}")
        for k, v in r.headers.items():
            if k.lower() in ("etag", "content-type", "location"):
                print(f"<<< {k}: {v}")
        if r.text:
            print("<<< " + r.text.strip().replace("\n", "\n<<< "))
        return r

    def href(self, name):
        return f"{self.calendar}{name}"

    def put(self, label, name, body, headers=None):
        return self.request(label, "PUT", self.href(name), body, {**ICS, **(headers or {})})

    def delete(self, label, name, headers=None):
        return self.request(label, "DELETE", self.href(name), headers=headers)

    def reset(self):
        self.request("reset: DELETE calendar", "DELETE", self.calendar)
        return self.request("reset: MKCALENDAR", "MKCALENDAR", self.calendar)

    def sync(self, label, token, limit=None):
        """sync-collection report. Returns (response, [(name, '200' | status)], token or "")."""
        tok = f"<D:sync-token>{token}</D:sync-token>" if token else "<D:sync-token/>"
        lim = f"<D:limit><D:nresults>{limit}</D:nresults></D:limit>" if limit else ""
        body = (
            f'<?xml version="1.0" encoding="utf-8"?><D:sync-collection xmlns:D="DAV:">{tok}'
            f"<D:sync-level>1</D:sync-level>{lim}<D:prop><D:getetag/></D:prop></D:sync-collection>"
        )
        r = self.request(label, "REPORT", self.calendar, body, {"Depth": "0", **XML})
        return r, members(r.text), capture(r"sync-token>([^<]*)<", r.text, "")

    def etag(self, name):
        r = requests.request(
            "PROPFIND",
            self.base + self.href(name),
            auth=self.auth,
            headers={"Depth": "0", **XML},
            data=propfind_body("getetag"),
        )
        tag = capture(r"<(?:\w+:)?getetag>([^<]*)<", r.text)
        return tag.replace(QUOT, '"') if tag else None


def response_elements(text):
    """(href tail, inner XML) for each response in a multistatus, in order, repeats included."""
    for resp in re.findall(r"<(?:\w+:)?response>(.*?)</(?:\w+:)?response>", text, re.DOTALL):
        href = capture(r"<(?:\w+:)?href>([^<]*)<", resp, "")
        yield (COLLECTION if href.endswith("/") else href.rsplit("/", 1)[-1]), resp


def responses(text):
    """{href tail: the response element's inner XML}; a repeated href keeps its last entry."""
    return dict(response_elements(text))


def member_status(resp):
    """'200' for a response with a propstat (changed), otherwise its status (404 for removed)."""
    if re.search(r"<(?:\w+:)?propstat>", resp):
        return "200"
    return capture(r"<(?:\w+:)?status>HTTP/1.1 (\d+)", resp, "?")


def members(text):
    """[(href tail, member_status)] in response order."""
    return [(name, member_status(resp)) for name, resp in response_elements(text)]


class JMAP:
    """Raw JMAP calls against one account's calendars, logging every call."""

    def __init__(self, base, user, password, needs_version):
        self.base = base
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.needs_version = needs_version
        self.session = self._http(base + "/.well-known/jmap")
        self.account = self.session["primaryAccounts"][JMAP_CALENDARS]
        self.calendar_id = None
        self.template = json.loads((FIXTURES / "event.json").read_text(encoding="utf-8"))

    def _http(self, url, body=None):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self.auth, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def call(self, label, method, args):
        """Returns (response name, arguments); the name is 'error' for a method-level error."""
        body = {
            "using": [JMAP_CORE, JMAP_CALENDARS],
            "methodCalls": [[method, {"accountId": self.account, **args}, "0"]],
        }
        name, out = self._http(self.base + "/jmap/", body)["methodResponses"][0][:2]
        print(
            f"\n===== {label} =====\n>>> {method} {json.dumps(args)}\n<<< {name} {json.dumps(out)}"
        )
        return name, out

    def state(self):
        return self.call("current state", "CalendarEvent/get", {"ids": []})[1]["state"]

    def event(self, uid, title):
        """The event from fixtures/event.json, with its uid, title and calendar filled in."""
        if self.calendar_id is None:
            calendars = self.call("calendars", "Calendar/get", {"ids": None})[1]["list"]
            self.calendar_id = calendars[0]["id"]
        ev = {**self.template, "uid": uid, "title": title, "calendarIds": {self.calendar_id: True}}
        if not self.needs_version:
            ev.pop("version")
        return ev

    def create(self, label, events):
        """events: {creation id: (uid, title)}. Returns {creation id: server id}."""
        created = {k: self.event(*v) for k, v in events.items()}
        out = self.call(label, "CalendarEvent/set", {"create": created})[1]
        return {k: v["id"] for k, v in (out.get("created") or {}).items()}
