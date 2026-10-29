#!/usr/bin/env bash
# Reset the OmniRoute dashboard password.
#
# Why this exists: `INITIAL_PASSWORD` seeds the password only on the FIRST boot of
# the data volume, and the packaged npm .env wins that race on a default install.
# `omniroute reset-password` reports success but can write to a different
# DATA_DIR than the running server reads, so the login keeps failing. Writing the
# bcrypt hash into the database the server actually uses is the reliable path.
#
#   scripts/omniroute-reset-password.sh <new-password> [container]
set -euo pipefail

PW="${1:?usage: $0 <new-password> [container]}"
CONTAINER="${2:-rigging-omniroute-1}"

if [ "${#PW}" -lt 8 ]; then
  echo "password must be at least 8 characters" >&2
  exit 2
fi

docker exec -e PW="$PW" "$CONTAINER" node -e '
const Database = require("/usr/local/lib/node_modules/omniroute/node_modules/better-sqlite3");
const bcrypt  = require("/usr/local/lib/node_modules/omniroute/node_modules/bcryptjs");
const db = new Database("/root/.omniroute/storage.sqlite");
const hash = bcrypt.hashSync(process.env.PW, 12);
const upsert = db.prepare("insert or replace into key_value(namespace,key,value) values(?,?,?)");
upsert.run("settings", "password", hash);
upsert.run("settings", "setupComplete", "true");
upsert.run("settings", "requireLogin", "true");
const check = db.prepare("select value from key_value where namespace=? and key=?").get("settings", "password");
console.log("hash verified:", bcrypt.compareSync(process.env.PW, check.value));
'

echo "restarting $CONTAINER (cold start reinstalls npm, ~2-3 min)…"
docker restart "$CONTAINER" >/dev/null

for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 http://localhost:20128/ || true)
  if [ "$code" != "000" ]; then
    echo "dashboard responding after $((i * 5))s (HTTP $code)"
    break
  fi
  sleep 5
done

echo "verifying login:"
curl -s -o /dev/null -w "  correct password -> HTTP %{http_code} (200 = ok)\n" \
  -X POST http://localhost:20128/api/auth/login -H "Content-Type: application/json" \
  -d "{\"password\":\"$PW\"}"
