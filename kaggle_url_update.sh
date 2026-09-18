#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 \"https://your-kaggle-endpoint\"" >&2
  exit 1
fi

kaggle_url="${1%/}"

if [[ ! "$kaggle_url" =~ ^https?://[^[:space:]\"\']+$ ]]; then
  echo "Error: provide a valid HTTP or HTTPS URL." >&2
  exit 1
fi

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
env_file="${KAGGLE_ENDPOINT_ENV_FILE:-$script_directory/.env}"
variable_name="KAGGLE_LLM_BASE_URL"
temporary_file="$(mktemp)"

cleanup() {
  rm -f -- "$temporary_file"
}
trap cleanup EXIT

if [[ -f "$env_file" ]]; then
  awk -v name="$variable_name" -v value="$kaggle_url" '
    BEGIN { updated = 0 }
    $0 ~ "^" name "=" {
      print name "=" value
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) {
        print name "=" value
      }
    }
  ' "$env_file" > "$temporary_file"
else
  printf '%s=%s\n' "$variable_name" "$kaggle_url" > "$temporary_file"
fi

mv -- "$temporary_file" "$env_file"
trap - EXIT

echo "Updated $variable_name in $env_file"
