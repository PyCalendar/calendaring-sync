# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation check against the published RFC and draft texts, downloaded on first use into cache/.

  cite_check.py                 check the curated claims below (each phrase inside its section)
  cite_check.py FILE [FILE...]  also find every "RFC NNNN §X.Y", ":rfc:`NNNN#section-X.Y`" and
                                "draft-ietf-NAME-NN §X.Y" or "section X.Y" in FILE and confirm
                                the section exists

Whitespace and page breaks are normalized before matching, so a phrase split across lines or
across a page footer still matches. Exit status 1 if anything fails.
"""

import os
import re
import sys
import urllib.request
from pathlib import Path

RFC_DIR = os.path.join(os.path.dirname(__file__), "..", "cache")
DRAFT_URL = "https://www.ietf.org/archive/id/{name}.txt"
RFC_URL = "https://www.rfc-editor.org/rfc/{name}.txt"
DRAFT = "draft-ietf-jmap-calendars-33"
# Any draft a page cites, by its full name and revision.
CITED_DRAFT = r"(draft-ietf-[a-z-]+-\d+)"
PAGE_BREAK = re.compile(
    r"\n[^\n]*\[Page \d+\]\n\x0c?\n?(?:RFC \d+|Internet-Draft)[^\n]*\n", re.DOTALL
)

# (document, section, phrase that must appear verbatim inside that section)
CLAIMS = [
    (
        "6578",
        "3.1",
        "differences between the ETag values returned in the synchronization report and those returned when actually fetching resource content",
    ),
    ("6578", "3.2", "the token MUST be a valid URI"),
    ("6578", "3.2", 'This report is only defined when the Depth header has value "0"'),
    ("6578", "3.2", "A given member URL MUST appear only once in the response"),
    (
        "6578",
        "3.2",
        "the DAV:response MUST contain at least one DAV:propstat element and MUST NOT contain any DAV:status element",
    ),
    (
        "6578",
        "3.2",
        "contain one DAV:status with a value set to '404 Not Found' and MUST NOT contain any DAV:propstat element",
    ),
    (
        "6578",
        "3.2",
        "that will not require clients to download resources that are already cached and have not changed",
    ),
    ("6578", "3.3", "clients MUST include a DAV:sync-level XML element in the request"),
    ("6578", "3.4", "it MUST NOT return any removed member URLs"),
    (
        "6578",
        "3.5.1",
        "In the case where a mapping between a member URL and the target collection was removed, then a new mapping with the same URI was created, the member URL MUST be reported as changed and MUST NOT be reported as removed",
    ),
    (
        "6578",
        "3.5.2",
        "If a member was added (and its mapped resource possibly modified), then removed between two synchronization report requests, it MUST be reported as removed",
    ),
    ("6578", "3.6", "indicate a status of 507 (Insufficient Storage) for the request-URI"),
    (
        "6578",
        "3.6",
        "the DAV:sync-token value returned in the response MUST represent the correct state for the partial set of changes returned",
    ),
    (
        "6578",
        "3.6",
        "Clients MUST handle the 507 status on the request-URI in the response to the report",
    ),
    (
        "6578",
        "3.7",
        "If a server is unable to truncate the result at or below the requested number, then it MUST fail the request",
    ),
    (
        "4791",
        "1.3",
        "MUST either be 403 (Forbidden), if the request should not be repeated because it will always fail, or 409 (Conflict)",
    ),
    (
        "4791",
        "4.1",
        "Calendar components with the same UID property value, in a given calendar collection, MUST be contained in the same calendar object resource",
    ),
    (
        "4791",
        "4.1",
        "MUST be unique in the scope of the calendar collection in which they are stored",
    ),
    ("4791", "5.3.2.1", "(CALDAV:no-uid-conflict)"),
    (
        "4791",
        "5.3.4",
        "with the exception that a strong entity tag MUST NOT be returned in the response",
    ),
    (
        "4791",
        "7.9",
        "MUST contain a DAV:response element for each calendar object resource referenced by the provided set of DAV:href elements",
    ),
    (
        "4791",
        "7.9",
        'the "Depth" header MUST be ignored by the server and SHOULD NOT be sent by the client',
    ),
    (
        "6638",
        "3.2.4.1",
        'Servers MAY reject requests to create a scheduling object resource with an iCalendar "UID" property value already in use by another scheduling object resource',
    ),
    ("3253", "3.1.5", "DAV:supported-report-set"),
    (
        "3986",
        "2.3",
        "Characters that are allowed in a URI but do not have a reserved purpose are called unreserved",
    ),
    (
        "9110",
        "8.8.3.2",
        '"Strong comparison": two entity tags are equivalent if both are not weak and their opaque-tags match character-by-character',
    ),
    ("9110", "13.1.1", "If-Match"),
    ("9110", "13.1.2", "If-None-Match"),
    ("5545", "3.8.4.7", "Unique Identifier"),
    ("8620", "1.6", "Terminology"),
    ("8620", "5.1", "state"),
    ("8620", "5.2", 'This is the "sinceState" argument echoed back'),
    (
        "8620",
        "5.2",
        "If a record has been created AND destroyed since the old state, the server SHOULD remove the id from the response entirely",
    ),
    ("8620", "5.2", "The client MUST invalidate its Foo cache"),
    (
        "8620",
        "5.2",
        'If true, the client may call "Foo/changes" again with the "newState" returned to get further updates',
    ),
    ("8620", "5.3", "stateMismatch"),
    ("4918", "15.6", "getetag"),
    ("6578", "3.7", "A client can limit the number of results returned by the server"),
    (
        "8620",
        "5.1",
        "A (preferably short) string representing the state on the server for *all* the data of this type in the account",
    ),
    (
        "8620",
        "1.6.3",
        "The id MUST be unique among all records of the *same type* within the *same account*",
    ),
    (
        DRAFT,
        "1.4.1",
        'An Account MUST NOT contain more than one CalendarEvent with the same uid unless all of the CalendarEvent objects have distinct, non-null values for their "recurrenceId" property',
    ),
    (DRAFT, "5.9", "uid MUST be set to a new globally unique identifier"),
    (DRAFT, "5.9", "version MUST be set to the JSCalendar version the event conforms to"),
]

_cache = {}


def text_of(doc):
    if doc not in _cache:
        name = doc if doc.startswith("draft") else f"rfc{doc}"
        path = os.path.join(RFC_DIR, name + ".txt")
        if not os.path.exists(path):
            os.makedirs(RFC_DIR, exist_ok=True)
            url = (DRAFT_URL if doc.startswith("draft") else RFC_URL).format(name=name)
            urllib.request.urlretrieve(url, path)
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        _cache[doc] = PAGE_BREAK.sub("\n", raw.replace("\r", ""))
    return _cache[doc]


def section(doc, sec):
    text = text_of(doc)
    head = re.compile(rf"^{re.escape(sec)}\.?\s+\S", re.MULTILINE)
    m = head.search(text)
    if not m:
        return None
    nxt = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S", re.MULTILINE).search(text, m.end())
    return text[m.start() : nxt.start() if nxt else len(text)]


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


failures = 0
for doc, sec, phrase in CLAIMS:
    body = section(doc, sec)
    ok = body is not None and norm(phrase) in norm(body)
    failures += not ok
    label = f"{'RFC ' + doc if doc.isdigit() else doc} §{sec}"
    print(f"{'PASS' if ok else 'FAIL'} {label}: {phrase[:90]}{'...' if len(phrase) > 90 else ''}")

for path in sys.argv[1:]:
    content = Path(path).read_text(encoding="utf-8")
    refs = set()
    for doc, secs in re.findall(r"RFC (\d{4}) ((?:§[\d.]*\d(?:, )?)+)", content):
        refs |= {(doc, s) for s in re.findall(r"§([\d.]*\d)", secs)}
    refs |= set(re.findall(r":rfc:`(\d{4})#section-([\d.]*\d)`", content))
    refs |= set(re.findall(rf"{CITED_DRAFT}(?: §|#section-| section )([\d.]*\d)", content))
    bare = set(re.findall(r":rfc:`(\d{4})`", content))
    for doc in sorted(bare):
        ok = bool(text_of(doc))
        failures += not ok
        print(
            f"{'PASS' if ok else 'FAIL'} {os.path.basename(path)} cites RFC {doc}: text {'found' if ok else 'MISSING'}"
        )
    for doc, sec in sorted(refs):
        ok = section(doc, sec) is not None
        failures += not ok
        print(
            f"{'PASS' if ok else 'FAIL'} {os.path.basename(path)} cites {doc} §{sec}: section {'exists' if ok else 'NOT FOUND'}"
        )

print(f"\n{'all citations verified' if not failures else f'{failures} failed'}")
sys.exit(1 if failures else 0)
