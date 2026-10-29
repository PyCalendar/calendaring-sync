#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

# Regenerates every log in logs/ from the probes. See README.md for requirements.
#
#   PYTHON=/path/to/python CALENDARING_JMAP=/path/to/calendaring-jmap ./run.sh [section...]
#
# Sections: radicale xandikos radicale-expiry unsupported cyrus stalwart stalwart-latest baikal
# model citations. With no arguments, every section runs. Each section starts its own server
# and stops it again, removing container volumes.

set -u
native() {  # a directory's path in the form the platform's own programs expect (pwd -W on Git Bash)
    (cd "$1" && (pwd -W 2> /dev/null || pwd))
}
HERE="$(native "$(dirname "${BASH_SOURCE[0]}")")"
PY="${PYTHON:-python}"
if [ -n "${CALENDARING_JMAP:-}" ]; then CALENDARING_JMAP="$(native "$CALENDARING_JMAP")"; fi
if [ -n "${CALENDARING_SYNC:-}" ]; then CALENDARING_SYNC="$(native "$CALENDARING_SYNC")"; fi
LOGS="$HERE/logs"
DATA="$HERE/data"
PROBES="$HERE/probes"
SERVERS="$HERE/servers"
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: pass URL paths through unchanged
mkdir -p "$LOGS" "$DATA"

# Two runs at once would start and stop the same servers under each other.
LOCK="$DATA/run.lock"
if ! mkdir "$LOCK" 2> /dev/null; then
    echo "another run.sh is using $DATA (remove $LOCK if it isn't)" >&2
    exit 1
fi
trap 'rmdir "$LOCK"' EXIT

wait_for() {  # wait_for URL [expected status]
    for _ in $(seq 1 90); do
        code=$(curl -s -o /dev/null -w "%{http_code}" "$1")
        if [ -n "${2:-}" ] && [ "$code" = "$2" ]; then return 0; fi
        if [ -z "${2:-}" ] && [ "$code" != "000" ]; then return 0; fi
        sleep 2
    done
    echo "timed out waiting for $1" >&2
    return 1
}

stop_port() {  # stops whatever listens on 127.0.0.1:PORT
    if command -v taskkill > /dev/null; then
        pid=$(netstat -ano | grep "127.0.0.1:$1 .*LISTENING" | awk '{print $NF}' | head -1)
        [ -n "$pid" ] && taskkill /PID "$pid" /F > /dev/null
    else
        pkill -f "127.0.0.1:$1" || true
    fi
}

probe() {  # probe LOG SCRIPT ARGS...
    log="$LOGS/$1"; shift
    "$PY" -B -E -s "$PROBES/$@" > "$log" 2>&1
    echo "$(basename "$log"): $(grep -c '^RESULT' "$log") results, $(grep -c '^RESULT .* FAIL ' "$log") failed"
}

fixture() {  # fixture cyrus|stalwart start|stop
    : "${CALENDARING_JMAP:?set CALENDARING_JMAP to a calendaring-jmap checkout}"
    (cd "$CALENDARING_JMAP/tests/docker/$1" && bash "$2.sh" > /dev/null 2>&1)
}

own_fixture() {  # own_fixture start|stop NAME: calendaring-sync's own fixture, the server its tests use
    : "${CALENDARING_SYNC:?set CALENDARING_SYNC to a calendaring-sync checkout}"
    (cd "$CALENDARING_SYNC/tests/docker" && bash "$1.sh" "$2" > /dev/null 2>&1)
}

radicale() {
    rm -rf "$DATA/radicale"
    "$PY" -m radicale --config "" --auth-type none --rights-type authenticated \
        --storage-filesystem-folder "$DATA/radicale" --server-hosts 127.0.0.1:5232 > "$LOGS/server-radicale.log" 2>&1 &
    wait_for http://127.0.0.1:5232/
    curl -s -o /dev/null -u test:test -X MKCALENDAR http://127.0.0.1:5232/test/client/
    probe caldav-sync-radicale.log caldav_sync.py Radicale http://127.0.0.1:5232 /test/probe/ test test
    probe caldav-client-radicale.log caldav_client.py Radicale http://127.0.0.1:5232/ http://127.0.0.1:5232/test/client/ test test
    probe fingerprint-radicale.log fingerprint.py caldav Radicale http://127.0.0.1:5232 /test/fp/ test test
    stop_port 5232
}

xandikos() {
    rm -rf "$DATA/xandikos" "$DATA/xandikos-state"
    "$PY" -m xandikos.web -d "$DATA/xandikos" --defaults -l 127.0.0.1 -p 8090 \
        --state-dir "$DATA/xandikos-state" > "$LOGS/server-xandikos.log" 2>&1 &
    wait_for http://127.0.0.1:8090/
    curl -s -o /dev/null -u test:test -X MKCALENDAR http://127.0.0.1:8090/user/calendars/client/
    probe caldav-sync-xandikos.log caldav_sync.py Xandikos http://127.0.0.1:8090 /user/calendars/probe/ test test
    probe caldav-client-xandikos.log caldav_client.py Xandikos http://127.0.0.1:8090/ http://127.0.0.1:8090/user/calendars/client/ test test
    probe fingerprint-xandikos.log fingerprint.py caldav Xandikos http://127.0.0.1:8090 /user/calendars/fp/ test test
    stop_port 8090
}

radicale_expiry() {
    rm -rf "$DATA/radicale-expiry"
    printf "test:test\nintruder:x\n" > "$DATA/htpasswd"
    "$PY" -m radicale --config "" --auth-type htpasswd --auth-htpasswd-filename "$DATA/htpasswd" \
        --auth-htpasswd-encryption plain --rights-type owner_only --storage-filesystem-folder "$DATA/radicale-expiry" \
        --storage-max-sync-token-age 2 --server-hosts 127.0.0.1:5233 > "$LOGS/server-radicale-expiry.log" 2>&1 &
    wait_for http://127.0.0.1:5233/
    probe expiry-caldav-radicale.log expiry.py caldav Radicale http://127.0.0.1:5233 /test/expiry/ test test before "$DATA/radicale-token"
    # Wait past the 2-second token age, then make Radicale issue new tokens so its cleanup runs.
    sleep 4
    for n in 1 2; do
        probe "expiry-caldav-radicale-tick$n.log" expiry.py caldav Radicale http://127.0.0.1:5233 /test/expiry/ test test tick "$DATA/radicale-token"
        sleep 3
    done
    probe expiry-caldav-radicale-after.log expiry.py caldav Radicale http://127.0.0.1:5233 /test/expiry/ test test after "$DATA/radicale-token"
    probe retry-radicale.log retry_and_unsupported.py retry http://127.0.0.1:5233/ http://127.0.0.1:5233/test/expiry/ "$DATA/radicale-token"
    stop_port 5233
}

unsupported() {
    probe unsupported-sync-collection.log retry_and_unsupported.py unsupported 5240 5241
}

cyrus() {
    fixture cyrus start
    curl -s -o /dev/null -u user1:x -X MKCALENDAR http://localhost:8802/dav/calendars/user/user1/client/
    probe caldav-sync-cyrus.log caldav_sync.py Cyrus http://localhost:8802 /dav/calendars/user/user1/probe/ user1 x
    probe caldav-client-cyrus.log caldav_client.py Cyrus http://localhost:8802/ http://localhost:8802/dav/calendars/user/user1/client/ user1 x
    probe jmap-sync-cyrus.log jmap_sync.py Cyrus http://localhost:8802 user1 x yes
    probe jmap-client-cyrus.log jmap_client.py Cyrus http://localhost:8802/.well-known/jmap user1 x
    probe jmap-instances-cyrus.log jmap_instances.py Cyrus http://localhost:8802 user1 x yes
    probe fingerprint-cyrus.log fingerprint.py caldav Cyrus http://localhost:8802 /dav/calendars/user/user1/fp/ user1 x
    probe fingerprint-jmap-cyrus.log fingerprint.py jmap Cyrus http://localhost:8802 user1 x yes
    probe expiry-caldav-cyrus.log expiry.py caldav Cyrus http://localhost:8802 /dav/calendars/user/user1/expiry/ user1 x before "$DATA/cyrus-token"
    probe expiry-jmap-cyrus.log expiry.py jmap Cyrus http://localhost:8802 user1 x yes before "$DATA/cyrus-state"
    docker exec cyrus-test /usr/cyrus/sbin/cyr_expire -X 0 -D 0 -E 0 -v -u user1 > "$LOGS/server-cyrus-cyr_expire.log" 2>&1
    probe expiry-caldav-cyrus-after.log expiry.py caldav Cyrus http://localhost:8802 /dav/calendars/user/user1/expiry/ user1 x after "$DATA/cyrus-token"
    probe expiry-jmap-cyrus-after.log expiry.py jmap Cyrus http://localhost:8802 user1 x yes after "$DATA/cyrus-state"
    fixture cyrus stop
}

stalwart() {
    # The version calendaring-sync's fixture runs; the 0.16.21 logs in logs/ came from
    # calendaring-jmap's fixture, and earlier links point at their lines.
    local LOGS="$LOGS/stalwart-1.0.1"
    mkdir -p "$LOGS"
    user="testuser@example.org"; home="/dav/cal/testuser%40example.org"
    own_fixture start stalwart
    curl -s -o /dev/null -u "$user:testcaldav" -X MKCALENDAR "http://localhost:8809$home/client/"
    probe caldav-sync-stalwart.log caldav_sync.py Stalwart http://localhost:8809 "$home/probe/" "$user" testcaldav
    probe caldav-client-stalwart.log caldav_client.py Stalwart http://localhost:8809/ "http://localhost:8809$home/client/" "$user" testcaldav
    probe jmap-sync-stalwart.log jmap_sync.py Stalwart http://localhost:8809 "$user" testcaldav no
    probe jmap-client-stalwart.log jmap_client.py Stalwart http://localhost:8809/.well-known/jmap "$user" testcaldav
    probe jmap-instances-stalwart.log jmap_instances.py Stalwart http://localhost:8809 "$user" testcaldav no
    probe fingerprint-stalwart.log fingerprint.py caldav Stalwart http://localhost:8809 "$home/fp/" "$user" testcaldav
    probe fingerprint-jmap-stalwart.log fingerprint.py jmap Stalwart http://localhost:8809 "$user" testcaldav no
    "$PY" -B -E -s "$SERVERS/stalwart_admin.py" expire-setup > "$LOGS/server-stalwart-expire-setup.log" 2>&1
    probe expiry-caldav-stalwart.log expiry.py caldav Stalwart http://localhost:8809 "$home/expiry/" "$user" testcaldav before "$DATA/stalwart-token"
    probe expiry-jmap-stalwart.log expiry.py jmap Stalwart http://localhost:8809 "$user" testcaldav no before "$DATA/stalwart-state"
    "$PY" -B -E -s "$SERVERS/stalwart_admin.py" expire-purge > "$LOGS/server-stalwart-expire-purge.log" 2>&1
    probe expiry-caldav-stalwart-after.log expiry.py caldav Stalwart http://localhost:8809 "$home/expiry/" "$user" testcaldav after "$DATA/stalwart-token"
    probe expiry-jmap-stalwart-after.log expiry.py jmap Stalwart http://localhost:8809 "$user" testcaldav no after "$DATA/stalwart-state"
    # Last, since it lowers the result limit for the whole server.
    "$PY" -B -E -s "$SERVERS/stalwart_admin.py" max-results 5 > "$LOGS/server-stalwart-max-results.log" 2>&1
    probe listing-cap-stalwart.log listing_cap.py Stalwart http://localhost:8809 "$home/cap/" "$user" testcaldav 5
    own_fixture stop stalwart
}

stalwart_latest() {
    : "${CALENDARING_JMAP:?set CALENDARING_JMAP to a calendaring-jmap checkout}"
    docker run -d --rm --name stalwart-latest-evidence -p 127.0.0.1:8810:8080 -e STALWART_RECOVERY_ADMIN=admin:adminpass \
        -v "$CALENDARING_JMAP/tests/docker/stalwart/config/config.json:/etc/stalwart/config.json:ro" \
        --tmpfs /opt/stalwart/data:size=300m stalwartlabs/stalwart:latest > /dev/null
    wait_for http://127.0.0.1:8810/.well-known/jmap 307
    {
        docker image inspect stalwartlabs/stalwart:latest --format 'image {{index .RepoDigests 0}}'
        curl -s -u admin:adminpass -L http://127.0.0.1:8810/.well-known/jmap | "$PY" -c \
            "import json,sys; c=json.load(sys.stdin)['capabilities']; print('RESULT [Stalwart latest] capabilities: calendars:', 'urn:ietf:params:jmap:calendars' in c, ', tasks:', 'urn:ietf:params:jmap:tasks' in c)"
    } > "$LOGS/jmap-capabilities-stalwart-latest.log" 2>&1
    docker stop stalwart-latest-evidence > /dev/null
    echo "jmap-capabilities-stalwart-latest.log: done"
}

baikal() {
    docker run -d --rm --name baikal-evidence -p 127.0.0.1:8811:80 ckulka/baikal:nginx > /dev/null
    wait_for http://127.0.0.1:8811/admin/install/ 200
    {
        docker image inspect ckulka/baikal:nginx --format 'image {{.Id}} version {{index .Config.Labels "org.opencontainers.image.version"}}'
        "$PY" -B -E -s "$SERVERS/baikal_setup.py"
    } > "$LOGS/server-baikal-setup.log" 2>&1
    curl -s -o /dev/null -u test:test -X MKCALENDAR http://127.0.0.1:8811/dav.php/calendars/test/client/
    probe caldav-sync-baikal.log caldav_sync.py Baikal http://127.0.0.1:8811 /dav.php/calendars/test/probe/ test test
    probe caldav-client-baikal.log caldav_client.py Baikal http://127.0.0.1:8811/ http://127.0.0.1:8811/dav.php/calendars/test/client/ test test
    probe fingerprint-baikal.log fingerprint.py caldav Baikal http://127.0.0.1:8811 /dav.php/calendars/test/fp/ test test
    docker stop baikal-evidence > /dev/null
}

model() {
    "$PY" -B -E -s "$PROBES/model.py" > "$LOGS/model.log" 2>&1
    echo "model.log: $(tail -1 "$LOGS/model.log")"
}

citations() {
    pages=()
    for p in design adapters; do pages+=("${CALENDARING_SYNC:?set CALENDARING_SYNC to a calendaring-sync checkout}/docs/explanation/$p.rst"); done
    pages+=("$CALENDARING_SYNC/docs/reference/server-compatibility.rst")
    "$PY" -B -E -s "$PROBES/cite_check.py" "${pages[@]}" > "$LOGS/citations.log" 2>&1
    echo "citations.log: $(tail -1 "$LOGS/citations.log")"
}

sections=("$@")
[ ${#sections[@]} -eq 0 ] && sections=(radicale xandikos radicale-expiry unsupported cyrus stalwart stalwart-latest baikal model citations)
for s in "${sections[@]}"; do
    echo "== $s"
    "${s//-/_}"
done

# Logs name this checkout's own data/ directory and the local user; keep them independent of
# where it was cloned and who ran it.
"$PY" -B -E -s - "$LOGS" "$DATA" <<'EOF'
import pathlib, re, sys
logs, data = sys.argv[1], sys.argv[2]
variants = {data, data.replace("/", "\\"), data.replace("/", "\\\\"), data.replace("\\", "\\\\")}
site_packages = re.compile(r"[^\s\"']*[\\/]site-packages(?=[\\/])")
local_user = re.compile(r"\bas user=\S+")
for log in pathlib.Path(logs).glob("*.log"):
    text = log.read_text(encoding="utf-8", errors="replace")
    for v in sorted(variants, key=len, reverse=True):
        text = text.replace(v, "<data>")
    text = site_packages.sub("<site-packages>", text)
    text = local_user.sub("as user=<user>", text)
    log.write_text(text, encoding="utf-8")
EOF
