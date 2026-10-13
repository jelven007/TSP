# Cell PostgreSQL

This compose project provides one PostgreSQL instance with separate control and
Cell data databases:

- `tsp_control`: users, memberships, routing, sessions, and strategy catalog.
- `tsp_data_local`: tenant business data protected by forced RLS.

## Bootstrap

Create a root-only `.pg.env` with independent random passwords:

```dotenv
TSP_PG_ADMIN_PASSWORD=<random-admin-password>
TSP_PG_RUNTIME_PASSWORD=<random-runtime-password>
```

Start PostgreSQL:

```bash
set -a
. ./.pg.env
set +a
docker compose -p tsp-pg -f compose.yml up -d
```

`bootstrap.sh` runs only for a new data volume. It creates `tsp_data_local` and
the restricted `tsp_runtime` role. The role is explicitly neither superuser nor
`BYPASSRLS`.

## Migrations

Run migrations with offline admin DSNs. Runtime containers must receive only
the `tsp_runtime` DSNs.

```bash
python -m app.cell.admin migrate --kind control --runtime-role tsp_runtime
python -m app.cell.admin migrate --kind data --runtime-role tsp_runtime --cell-id local
```

PostgreSQL binds to `127.0.0.1:5432`. Cell containers connect through the
external Docker network `tsp-pg_default`.
