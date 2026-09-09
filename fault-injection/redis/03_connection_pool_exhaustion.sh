#!/bin/bash
# Redis fault 3: Connection pool exhaustion
# Drops the server-side maxclients limit so low that go-server/go-enricher's
# Redis client pools start getting connections refused under normal load.
# Visible via: redis_connected_clients near the cap, app logs showing
# "ERR max number of clients reached" / connection errors.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-redis1}"
LOW_MAXCLIENTS="${LOW_MAXCLIENTS:-5}"
DEFAULT_MAXCLIENTS=10000

inject() {
    echo "Setting maxclients=$LOW_MAXCLIENTS on $TARGET..."
    redis_cli "$TARGET" CONFIG SET maxclients "$LOW_MAXCLIENTS"
    echo "Done. Check redis_connected_clients + app logs for connection refusals."
}

restore() {
    echo "Restoring maxclients=$DEFAULT_MAXCLIENTS on $TARGET..."
    # At maxclients=5, every slot is usually already held by go-server,
    # go-enricher, and redis-exporter — there's no room left for this very
    # command to open a new connection. If CONFIG SET can't get in, fall
    # back to restarting the node, which resets maxclients to Redis's
    # built-in default (10000) since it's never set via the startup command.
    # redis-cli exits 0 even when the server replies with an error string,
    # so check the actual reply text rather than the exit code.
    RESULT="$(redis_cli "$TARGET" CONFIG SET maxclients "$DEFAULT_MAXCLIENTS" 2>&1 || true)"
    echo "$RESULT"
    if [[ "$RESULT" != *OK* ]]; then
        echo "CONFIG SET was refused (no free connection slot) — restarting $TARGET instead..."
        compose restart "$TARGET"
    fi
}

case "${1:-}" in
    inject) inject ;;
    restore) restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=redis1 LOW_MAXCLIENTS=5)"; exit 1 ;;
esac
