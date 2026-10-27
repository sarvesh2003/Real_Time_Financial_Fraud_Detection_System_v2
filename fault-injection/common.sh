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
    # A scaled service (e.g. go-enricher with N replicas) returns one
    # container ID per line - passing all of them jammed together into a
    # single downstream argument (docker update/run --net=container:) breaks.
    # All replicas share the same image/netns setup, so the first one is a
    # safe, unambiguous choice (same fix already applied for this exact bug
    # shape in mcp-server/server.py). Note: this means a fault applied via
    # this helper only affects that one replica, not all of them, if ever
    # pointed at a scaled service - never happens with today's defaults.
    compose ps -q "$1" | head -n1
}

# Local audit log (git-ignored; no tool shows it to the agent). One JSON
# line per inject/restore call, appended by every fault script's case statement.
CHANGELOG_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/changelog.jsonl"

log_change() {
    local action="$1"
    local fault_name family
    fault_name="$(basename "$0" .sh)"
    family="$(basename "$(dirname "$0")")"
    printf '{"timestamp":"%s","family":"%s","fault":"%s","action":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$family" "$fault_name" "$action" >> "$CHANGELOG_FILE"
}
