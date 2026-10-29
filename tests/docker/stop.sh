#!/bin/bash
# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# Stop the named servers and remove their containers and volumes, so the
# next start is clean. Usage: stop.sh stalwart [cyrus] [baikal]

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
for server in "$@"; do
    (cd "$server" && docker compose down -v)
done
