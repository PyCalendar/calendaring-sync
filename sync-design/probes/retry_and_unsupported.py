# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An expired token against a real refusal, and python-caldav on a server without
sync-collection.

Usage:
  retry_and_unsupported.py retry BASE_URL CALENDAR_URL TOKEN_FILE
      Radicale with htpasswd users test:test and intruder:x and owner_only rights; TOKEN_FILE
      holds a token for test's calendar that the server has expired.
  retry_and_unsupported.py unsupported PORT_403 PORT_501
      Starts two local stand-ins that answer every REPORT with 403 + DAV:supported-report and
      with 501, and shows what python-caldav raises for each.
"""

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from caldav.davclient import DAVClient
from caldav.lib import error

from _common import caldav_sync_report, finish, outcome, result, sync_objects


def report(client, url, token):
    try:
        return str(caldav_sync_report(client, url, token).status)
    except error.AuthorizationError as e:
        return f"AuthorizationError {e.reason}, preconditions {getattr(e, 'preconditions', None)}"


if sys.argv[1] == "retry":
    base, calendar_url, token_file = sys.argv[2:5]
    expired = Path(token_file).read_text(encoding="utf-8").strip()
    for user, password in (("test", "test"), ("intruder", "x")):
        client = DAVClient(url=base, username=user, password=password)
        result(
            "Radicale",
            f"retry rule, user {user}",
            f"expired token: {report(client, calendar_url, expired)}; "
            f"retry with an empty token: {report(client, calendar_url, None)}",
        )
        calendar = client.calendar(url=calendar_url)
        for fallback in (False, True):
            result(
                "Radicale",
                f"user {user}, expired token, "
                f"get_objects_by_sync_token(disable_fallback={fallback})",
                outcome(
                    lambda cal=calendar, off=fallback: (
                        sync_objects(cal, sync_token=expired, disable_fallback=off).sync_token
                    )
                ),
            )
else:
    port_403, port_501 = int(sys.argv[2]), int(sys.argv[3])

    def handler(code):
        class Handler(BaseHTTPRequestHandler):
            def do_REPORT(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = (
                    b'<?xml version="1.0"?><D:error xmlns:D="DAV:"><D:supported-report/></D:error>'
                    if code == 403
                    else b"REPORT not implemented"
                )
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        return Handler

    for port, code in ((port_403, 403), (port_501, 501)):
        Thread(
            target=HTTPServer(("127.0.0.1", port), handler(code)).serve_forever, daemon=True
        ).start()

    def unsupported(port, label):
        client = DAVClient(url=f"http://127.0.0.1:{port}/", username="u", password="p")
        url = f"http://127.0.0.1:{port}/calendar/"
        calendar = client.calendar(url=url)
        for how, fn in (
            (
                "get_objects_by_sync_token(disable_fallback=True)",
                lambda: sync_objects(calendar, disable_fallback=True).sync_token,
            ),
            ("DAVClient.report()", lambda: caldav_sync_report(client, url).status),
        ):
            result(
                "stand-in",
                f"no sync-collection, {label}, {how}",
                outcome(fn),
            )

    unsupported(port_403, "403 with DAV:supported-report")
    unsupported(port_501, "501")

finish()
