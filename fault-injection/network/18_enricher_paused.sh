#!/bin/bash
# Sustained, reversible enrichment-consumer pause.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

case "${1:-}" in
    inject) log_change inject; compose pause go-enricher ;;
    restore) log_change restore; compose unpause go-enricher ;;
    *) echo "Usage: $0 <inject|restore>"; exit 1 ;;
esac
