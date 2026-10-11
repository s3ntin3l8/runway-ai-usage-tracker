#!/usr/bin/env bash
set -euo pipefail
attempts=4
for attempt in $(seq 1 "$attempts"); do
  if printf '%s' "$REGISTRY_PASS" |
     docker login "$REGISTRY_HOST" -u "$REGISTRY_USER" --password-stdin; then
    exit 0
  fi
  if [ "$attempt" -lt "$attempts" ]; then
    echo "::warning::${REGISTRY_HOST} login attempt ${attempt}/${attempts} failed; retrying in $((attempt * 10))s"
    sleep $((attempt * 10))
  fi
done
echo "::error::${REGISTRY_HOST} login failed after ${attempts} attempts"
exit 1
