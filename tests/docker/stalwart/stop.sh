#!/bin/bash
# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# Stop Stalwart and remove its container and volumes, so the next start is clean.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
docker compose down -v
