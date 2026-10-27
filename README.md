<!--- SPDX-FileCopyrightText: 2026 calendaring-sync contributors -->
<!--- SPDX-License-Identifier: AGPL-3.0-or-later -->

# calendaring-sync

Calendar sync state: sync tokens, object versions, change tracking, and conflict detection.

Part of the [Python Calendaring Ecosystem](https://pycal.org).

[![Documentation](https://readthedocs.org/projects/calendaring-sync/badge/?version=stable)](https://calendaring-sync.readthedocs.io/en/stable/)
[![Tests](https://github.com/PyCalendar/calendaring-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/PyCalendar/calendaring-sync/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/PyCalendar/calendaring-sync/branch/main/graph/badge.svg)](https://codecov.io/gh/PyCalendar/calendaring-sync)
[![REUSE status](https://api.reuse.software/badge/github.com/PyCalendar/calendaring-sync)](https://api.reuse.software/info/github.com/PyCalendar/calendaring-sync)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL%203.0--or--later-blue.svg)](https://github.com/PyCalendar/calendaring-sync/blob/main/LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

calendaring-sync keeps the state a calendar sync needs between runs: the sync token the server last issued, a version for each object, and a local copy of what was last synced. It tells you what changed on each side and where local and remote changes conflict; your code decides what to do with each conflict. It doesn't talk to servers or run a sync loop itself: an adapter connects it to a server, whether one that ships with calendaring-sync or one you write.

## Installation

```bash
pip install calendaring-sync
```

The example below syncs a CalDAV calendar, so it needs the `caldav` extra: `pip install "calendaring-sync[caldav]"`. The [install guide](https://calendaring-sync.readthedocs.io/en/stable/how-to/install.html) lists the extra each adapter that ships with calendaring-sync needs.

## Usage

```python
from caldav.davclient import DAVClient

from calendaring_sync import Replica, SQLiteStore
from calendaring_sync.adapters.caldav import CalDAVAdapter

URL = "https://caldav.example.com/"
USERNAME = "alice"
PASSWORD = "secret"

with (
    SQLiteStore("sync.db") as store,
    DAVClient(url=URL, username=USERNAME, password=PASSWORD) as client,
):
    calendar = client.get_calendars()[0]
    replica = Replica(store)
    adapter = CalDAVAdapter(replica, calendar)
    adapter.sync()

    for entry in replica.read_feed():
        print(entry.kind, entry.object_id)
```

See the [quickstart](https://calendaring-sync.readthedocs.io/en/stable/tutorials/quickstart.html) for what this does, and the [how-to guides](https://calendaring-sync.readthedocs.io/en/stable/how-to/index.html) for local edits, conflicts, the change feed, and writing an adapter of your own.

## Documentation

Full documentation: https://calendaring-sync.readthedocs.io/en/stable/

## Changelog

See the [change log](https://calendaring-sync.readthedocs.io/en/stable/changelog.html).

## Contributing

See the [contributing guide](https://calendaring-sync.readthedocs.io/en/stable/contribute.html).

## Funding

This project is funded through [NGI0 Commons Fund](https://nlnet.nl/commonsfund), a fund established by [NLnet](https://nlnet.nl) with financial support from the European Commission's [Next Generation Internet](https://ngi.eu) program. Learn more at the [NLnet project page](https://nlnet.nl/project/Python-Webcalendaring).

[<img src="https://nlnet.nl/logo/banner.png" alt="NLnet foundation logo" width="20%" />](https://nlnet.nl)
[<img src="https://nlnet.nl/image/logos/NGI0_tag.svg" alt="NGI Zero Logo" width="20%" />](https://nlnet.nl/commonsfund)

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/PyCalendar/calendaring-sync/blob/main/LICENSE).
