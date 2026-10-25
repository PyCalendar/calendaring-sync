# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Record the JMAP adapter's exchanges with real servers, for the unit tests to replay.

Run ``python -m calendaring_sync.tests.record_jmap SERVER [SCENARIO...]``.
Cyrus is calendaring-jmap's Docker fixture, and Stalwart this repo's
(``tests/docker/stalwart/start.sh``); start them first, or give ``--url``
(the session URL), ``--username`` and ``--password``. Every scenario
deletes the account's events first, so use a test account.

Each scenario checks what it records, so a recording that doesn't show the
behaviour it's about fails instead of being saved.
"""

from __future__ import annotations

import calendaring_jmap

from calendaring_sync.tests import _jmap_scenarios
from calendaring_sync.tests._scenarios import record


def main() -> None:
    """Record the scenarios named for one server, or all of its recordings."""
    record(_jmap_scenarios, ("calendaring-jmap", calendaring_jmap.__version__))


if __name__ == "__main__":
    main()
