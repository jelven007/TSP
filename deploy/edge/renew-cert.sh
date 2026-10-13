#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

/opt/tsp-certbot/bin/certbot renew \
  --quiet \
  --webroot \
  --webroot-path "$ROOT/certbot/www"

docker exec tsp-edge nginx -t
docker exec tsp-edge nginx -s reload
