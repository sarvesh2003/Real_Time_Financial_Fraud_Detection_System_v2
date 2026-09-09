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
