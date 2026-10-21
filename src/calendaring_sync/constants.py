# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Constants shared across calendaring-sync.

Every value with a meaning beyond one line of code is defined here, so it is
never repeated. Other modules import from this file.
"""

#: Version of the SQLite store's schema, kept in its ``store_meta`` table.
#: Version 1 is edited in place until the first release.
SCHEMA_VERSION = 1

#: Default and largest number of entries one
#: :meth:`~calendaring_sync.replica.Replica.read_feed` call returns. Reading
#: the feed in pages keeps each read short, so it never holds up a write
#: in another process or thread for long.
FEED_PAGE_SIZE = 1000

#: Seconds :class:`~calendaring_sync.store.sqlite.SQLiteStore` waits for
#: another process or thread before giving up.
DEFAULT_TIMEOUT = 5.0

#: Where to report a bug, named in the message of
#: :class:`~calendaring_sync.error.UnexpectedStoreError`.
ISSUES_URL = "https://github.com/pycalendar/calendaring-sync/issues"
