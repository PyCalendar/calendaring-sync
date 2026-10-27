# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from calendaring_sync import MemoryStore, Replica

replica = Replica(MemoryStore())
URL = "https://jmap.example.com/.well-known/jmap"
USERNAME = "alice"
PASSWORD = "secret"

# start: sync
from calendaring_jmap import JMAPClient

from calendaring_sync.adapters.jmap import JMAPAdapter

with JMAPClient(url=URL, username=USERNAME, password=PASSWORD) as client:
    adapter = JMAPAdapter(replica, client, account_id=client.get_account_id())
    adapter.sync()
# end: sync
