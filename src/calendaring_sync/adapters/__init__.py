# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The adapter base classes, and the adapters that ship with calendaring-sync.

Each shipped adapter is a module of its own that needs the extra of the
same name. Importing this package imports no protocol's client library;
import the adapter's module, such as :mod:`calendaring_sync.adapters.caldav`,
to use it.
"""
