# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Change the throwaway Stalwart fixture's settings through its admin JMAP API.

Usage:
  stalwart_admin.py expire-setup      DataRetention.maxChangesHistory = 1, then reload
  stalwart_admin.py expire-purge      AccountMaintenance purge for testuser, as Stalwart's own
                                      tests/src/system/purge.rs does, truncating its change log
  stalwart_admin.py max-results N     WebDav.maxResults = N, then reload

The fixture's default config holds SpamDnsblServer entries whose expressions Stalwart's own
reload rejects ("bit_and"); a reload removes each one it names, in this container only, so the
new setting can apply.
"""

import base64
import json
import sys
import time
import urllib.request

AUTH = "Basic " + base64.b64encode(b"admin:adminpass").decode()


def call(name, args):
    body = {
        "using": ["urn:ietf:params:jmap:core"],
        "methodCalls": [[name, {"accountId": "d333333", **args}, "0"]],
    }
    req = urllib.request.Request(
        "http://localhost:8809/jmap",
        data=json.dumps(body).encode(),
        headers={"Authorization": AUTH, "Content-Type": "application/json"},
    )
    out = json.loads(urllib.request.urlopen(req).read())["methodResponses"][0]
    print(f">>> {name} {json.dumps(args)[:200]}\n<<< {out[0]} {json.dumps(out[1])[:400]}")
    return out[1]


def set_and_reload(obj, setting, value):
    """Set one singleton setting and reload, removing each config entry the reload rejects."""
    call(f"x:{obj}/set", {"update": {"singleton": {setting: value}}})
    for _ in range(40):
        r = call("x:Action/set", {"create": {"r": {"@type": "ReloadSettings"}}})
        if r.get("created"):
            print(f"SUMMARY [Stalwart] reload with {obj}.{setting} = {value}: ok")
            return
        bad = r["notCreated"]["r"]["objectId"]
        call(f"x:{bad['object']}/set", {"destroy": [bad["id"]]})
    sys.exit("reload never succeeded")


def testuser_id():
    accounts = call(
        "x:Account/get", {"ids": call("x:Account/query", {})["ids"], "properties": ["name"]}
    )["list"]
    return next(a["id"] for a in accounts if a["name"] == "testuser")


command = sys.argv[1]
if command == "expire-setup":
    set_and_reload("DataRetention", "maxChangesHistory", 1)
elif command == "expire-purge":
    call(
        "x:Task/set",
        {
            "create": {
                "t": {
                    "@type": "AccountMaintenance",
                    "accountId": testuser_id(),
                    "maintenanceType": "purge",
                }
            }
        },
    )
    time.sleep(10)
elif command == "max-results":
    set_and_reload("WebDav", "maxResults", int(sys.argv[2]))
else:
    sys.exit(__doc__)
