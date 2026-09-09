#!/bin/bash
# Shared helpers for fault injection scripts.
# Every fault script sources this to get a consistent way to talk to the stack.

COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker-compose.yaml"

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

redis_cli() {
    local node="$1"
    shift
    compose exec -T "$node" redis-cli "$@"
}

container_id() {
    compose ps -q "$1"
}

# Change log for the MCP server's list_recent_changes tool. One JSON line
# per inject/restore call, appended by every fault script's case statement.
CHANGELOG_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/changelog.jsonl"

log_change() {
    local action="$1"
    local fault_name family
    fault_name="$(basename "$0" .sh)"
    family="$(basename "$(dirname "$0")")"
    printf '{"timestamp":"%s","family":"%s","fault":"%s","action":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$family" "$fault_name" "$action" >> "$CHANGELOG_FILE"
}
