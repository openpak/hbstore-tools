#!/bin/sh
# Batch: build + upload + publish every 4TU package updated since $1, through a tunnel to prod.
set -e
cd "$(dirname "$0")"
PORT=$((20200 + $$ % 700))
SSH_AUTH_SOCK=<ssh-agent-socket>
# The tunnel is respawned if it drops: one dead ssh once failed 127 uploads in a row.
(while :; do ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -N -L $PORT:127.0.0.1:20070 <prod-host>; sleep 2; done) &
TUN=$!
trap 'kill $TUN; pkill -f "N -L $PORT:127.0.0.1:20070"' EXIT
sleep 3
export NX_INTERNAL_KEY=$(ssh -o BatchMode=yes <prod-host> 'grep "^NX_INTERNAL_KEY=" ~/openpak/deploy/.env | cut -d= -f2-')
python3 -u hb2nsp.py "$@" http://127.0.0.1:$PORT
