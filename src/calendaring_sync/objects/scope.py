# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A :term:`scope` as the store keeps it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Scope:
    """One scope.

    Attributes:
        id: The id the :term:`adapter` gave the scope.
        token: The scope's :term:`sync token`, or ``None`` before its first
            :term:`full listing` finishes and after a :term:`reset`.
        fingerprint_scheme: The :term:`fingerprint scheme` of the scope's
            records.
        media_type: The media type of the scope's :term:`content`.
        last_sync_at: When an :term:`ingest` of the scope last finished, or
            ``None``.
        last_full_listing_at: When a full listing of the scope last finished,
            or ``None``.
        listing_token: The token of the full listing in progress, or
            ``None``. A page of any other listing is refused, so two
            processes listing the scope at once can't mix their results.
    """

    id: str
    token: str | None
    fingerprint_scheme: str
    media_type: str
    last_sync_at: datetime | None = None
    last_full_listing_at: datetime | None = None
    listing_token: str | None = None
