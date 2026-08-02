#!/usr/bin/env bash
# questarr-forget-stale.sh — remove stale "downloaded" game records from Questarr
#
# When downloads complete (or vanish), Questarr marks the game status=owned and
# keeps a game_downloads record. If the underlying files don't actually exist
# (e.g. they landed in a container's ephemeral FS due to a path bug), Questarr's
# cron will RE-mark them owned on the next tick unless the game_downloads record
# is also removed. This script removes BOTH the download records and resets the
# game status back to "wanted" for the given statuses.
#
# Usage:
#   QUESTARR_URL=http://questarr:5000 QUESTARR_USER=admin QUESTARR_PASS=secret \
#     bash questarr-forget-stale.sh
#
# Optional:
#   RESET_STATUSES="owned completed"   # default: "owned completed"
#   DRY_RUN=1                          # list what would change, change nothing
set -euo pipefail

: "${QUESTARR_URL:?set QUESTARR_URL e.g. http://questarr:5000}"
: "${QUESTARR_USER:?set QUESTARR_USER}"
: "${QUESTARR_PASS:?set QUESTARR_PASS}"
RESET_STATUSES="${RESET_STATUSES:-owned completed}"
BASE="${QUESTARR_URL%/}"

echo "==> logging in to $BASE as $QUESTARR_USER ..."
TOKEN=$(curl -sS --fail-with-body -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"username":"%s","password":"%s"}' "$QUESTARR_USER" "$QUESTARR_PASS")" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
echo "    got token (${#TOKEN} chars)"

AUTH="Authorization: Bearer $TOKEN"
JSON='-H Content-Type: application/json'

changed=0
for status in $RESET_STATUSES; do
  echo "==> games with status=$status"
  games=$(curl -sS "$BASE/api/games/status/$status" -H "$AUTH" || echo '[]')
  count=$(printf '%s' "$games" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
  echo "    found $count game(s)"
  [ "$count" -eq 0 ] && continue

  printf '%s' "$games" | python3 -c '
import json,sys
for g in json.load(sys.stdin):
    print(g["id"] + "\t" + (g.get("title") or "?"))
' | while IFS=$'\t' read -r gid title; do
    echo "    -- [$title] ($gid)"
    # 1. remove linked download records
    dls=$(curl -sS "$BASE/api/games/$gid/downloads" -H "$AUTH" || echo '[]')
    printf '%s' "$dls" | python3 -c '
import json,sys
for d in json.load(sys.stdin):
    print(d.get("id","") + "\t" + (d.get("status") or "?"))
' | while IFS=$'\t' read -r did dlstatus; do
        [ -z "$did" ] && continue
        if [ -n "${DRY_RUN:-}" ]; then
            echo "       DRY-RUN would remove download $did (status=$dlstatus)"
        else
            curl -sS -o /dev/null -X DELETE "$BASE/api/games/$gid/downloads/$did" -H "$AUTH" \
              && echo "       removed download $did (status=$dlstatus)"
        fi
    done
    # 2. reset game status to wanted
    if [ -n "${DRY_RUN:-}" ]; then
        echo "       DRY-RUN would set status -> wanted"
    else
        curl -sS -o /dev/null -X PATCH "$BASE/api/games/$gid/status" $JSON \
          -H "$AUTH" -d '{"status":"wanted"}' \
          && echo "       status -> wanted"
    fi
    changed=$((changed+1))
  done
done

echo "==> done. ${DRY_RUN:+(DRY RUN) }$changed game(s) processed."
echo
echo "NOTE: this only clears Questarr's records. It does NOT touch files. If a"
echo "real download later reappears, Questarr will mark it owned again (correct)."