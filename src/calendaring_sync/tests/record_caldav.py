# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Record the CalDAV adapter's exchanges with real servers, for the unit tests to replay.

Run ``python -m calendaring_sync.tests.record_caldav SERVER [SCENARIO...]``.
Radicale and Xandikos run embedded. Cyrus and Stalwart are calendaring-jmap's
Docker fixtures, and Baïkal is ``ckulka/baikal:nginx`` set up as the evidence
branch's ``servers/baikal_setup.py`` does; start them first, or give
``--url``, ``--username`` and ``--password``. Stalwart's ``listing_cut_short``
needs its result limit lowered first, with the evidence branch's
``servers/stalwart_admin.py max-results 2``.

Each scenario checks what it records, so a recording that doesn't show the
behaviour it's about fails instead of being saved.
"""

from __future__ import annotations

import argparse
import datetime
from contextlib import AbstractContextManager, nullcontext

import caldav

from calendaring_sync.tests._caldav_scenarios import RECORDINGS, SERVERS, Login, run, started
from calendaring_sync.tests._caldav_tape import Tape


def main() -> None:
    """Record the scenarios named for one server, or all of its recordings."""
    parser = argparse.ArgumentParser(description="Record the CalDAV adapter's exchanges.")
    parser.add_argument("server", choices=sorted(SERVERS))
    parser.add_argument("scenarios", nargs="*", help="scenarios to record; all by default")
    parser.add_argument("--url", help="the server's base URL")
    parser.add_argument("--username")
    parser.add_argument("--password")
    args = parser.parse_args()
    wanted = args.scenarios or [s for server, s in RECORDINGS if server == args.server]
    server = SERVERS[args.server]
    running: AbstractContextManager[Login] = (
        nullcontext((args.url, args.username, args.password)) if args.url else started(server)
    )
    with running as login:
        for scenario in wanted:
            tape = Tape()
            run(server, scenario, tape, login=login)
            tape.save(
                f"{args.server}-{scenario}",
                {
                    "server": args.server,
                    "scenario": scenario,
                    "caldav": caldav.__version__,
                    "recorded": datetime.date.today().isoformat(),
                },
            )
            print(f"{args.server}-{scenario}: {len(tape.exchanges)} exchanges")


if __name__ == "__main__":
    main()
