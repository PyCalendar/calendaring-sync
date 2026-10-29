# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Record the CalDAV adapter's exchanges with real servers, for the unit tests to replay.

Run ``python -m calendaring_sync.tests.record_caldav SERVER [SCENARIO...]``.
Radicale and Xandikos run embedded; start the others with
``tests/docker/start.sh``, or give ``--url``, ``--username`` and
``--password``. Stalwart's ``listing_cut_short``
needs its result limit lowered first, with the evidence branch's
``servers/stalwart_admin.py max-results 2``.

Each scenario checks what it records, so a recording that doesn't show the
behaviour it's about fails instead of being saved.
"""

from __future__ import annotations

import caldav

from calendaring_sync.tests import _caldav_scenarios
from calendaring_sync.tests._scenarios import record


def main() -> None:
    """Record the scenarios named for one server, or all of its recordings."""
    record(_caldav_scenarios, ("caldav", caldav.__version__))


if __name__ == "__main__":
    main()
