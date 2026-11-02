#!/bin/sh
# Batch: build + upload + publish every 4TU package updated since $1, through a tunnel to prod.
set -e
cd "$(dirname "$0")"
PORT=$((20200 + $$ % 700))
PAKPORT=$((21200 + $$ % 700))
# The production SSH target and agent come from a private, untracked env file (see the private
# deploy notes): OPENPAK_PROD_HOST=user@host and SSH_AUTH_SOCK=<agent holding its key>.
DEPLOY_ENV=${OPENPAK_DEPLOY_ENV:-$HOME/REPOS/Openpak/.local/deploy.env}
if [ -r "$DEPLOY_ENV" ]; then . "$DEPLOY_ENV"; fi
PROD=${OPENPAK_PROD_HOST:?set OPENPAK_PROD_HOST to the production SSH target}
export SSH_AUTH_SOCK
# The tunnel is respawned if it drops: one dead ssh once failed 127 uploads in a row.
# It also forwards Pakka, for the post-sync broken-set report below.
(while :; do ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -N -L $PORT:127.0.0.1:20070 -L $PAKPORT:127.0.0.1:24800 "$PROD"; sleep 2; done) &
TUN=$!
trap 'kill $TUN; pkill -f "N -L $PORT:127.0.0.1:20070"' EXIT
sleep 3
export NX_INTERNAL_KEY=$(ssh -o BatchMode=yes "$PROD" 'grep "^NX_INTERNAL_KEY=" ~/openpak/deploy/.env | cut -d= -f2-')
python3 -u hb2nsp.py "$@" http://127.0.0.1:$PORT
# Report the broken set to Pakka's eShop broken watch (PROD runs the
# watcher): an empty or missing set clears previously reported entries.
# Best effort: a failed post must not fail the sync.
if [ "$1" = "sync" ] && command -v curl >/dev/null 2>&1; then
  DATA=${HBSTORE_DATA:-$HOME/REPOS/Openpak/scratch/hbstore}
  BROKEN="$DATA/out/sync-broken.json"
  BODY='{"broken": {}}'
  if [ -r "$BROKEN" ]; then BODY=$(printf '{"broken": %s}' "$(cat "$BROKEN")"); fi
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 -X POST \
    "http://127.0.0.1:$PAKPORT/v1/eshop-broken" \
    -H "Content-Type: application/json" -d "$BODY") || CODE="000"
  [ "$CODE" = "200" ] || echo "warn: broken-set post to Pakka failed (http $CODE)" >&2
fi
