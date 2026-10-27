#!/bin/bash
# Sustained, reversible broker outage for the isolated experiment stack.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

case "${1:-}" in
    inject) log_change inject; compose stop kafka ;;
    restore) log_change restore; compose start kafka ;;
    *) echo "Usage: $0 <inject|restore>"; exit 1 ;;
esac
