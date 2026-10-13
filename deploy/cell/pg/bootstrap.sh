#!/usr/bin/env bash
set -Eeuo pipefail

: "${POSTGRES_USER:?POSTGRES_USER required}"
: "${POSTGRES_DB:?POSTGRES_DB required}"
: "${TSP_PG_RUNTIME_PASSWORD:?runtime password required}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=runtime_password="$TSP_PG_RUNTIME_PASSWORD" <<'SQL'
CREATE ROLE tsp_runtime
  WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE INHERIT
  PASSWORD :'runtime_password';

CREATE DATABASE tsp_data_local OWNER tsp_admin;

GRANT CONNECT ON DATABASE tsp_control TO tsp_runtime;
GRANT CONNECT ON DATABASE tsp_data_local TO tsp_runtime;
REVOKE CONNECT ON DATABASE tsp_control FROM PUBLIC;
REVOKE CONNECT ON DATABASE tsp_data_local FROM PUBLIC;
SQL
