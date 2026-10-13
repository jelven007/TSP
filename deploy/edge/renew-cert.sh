#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$ROOT/issue-local-cert.sh"

docker exec tsp-edge nginx -t
docker exec tsp-edge nginx -s reload
