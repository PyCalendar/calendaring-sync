#!/bin/bash
# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# Start Stalwart for the integration tests and set it up; any failure stops the script.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
docker compose up -d --wait
python setup_stalwart.py
