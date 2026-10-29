#!/bin/bash
# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# Start the named servers for the integration tests and set each one up;
# any failure stops the script. Usage: start.sh stalwart [cyrus] [baikal]

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
for server in "$@"; do
    (cd "$server" && docker compose up -d --wait && python "setup_$server.py")
done
