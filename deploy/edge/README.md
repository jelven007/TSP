# HTTPS Edge

The edge stack binds public ports 80 and 443. It keeps the legacy platform and
the Cell API on private Docker networks.

## Production entry

`https.conf` serves the full application at `app:3018`. The separate Cell UI
does not provide all existing product pages and must not replace the public
entry. The 2026-10-14 rollback restored image `app-app:32be445` and stopped the
Cell API and its four Workers. Local CA HTTPS remains enabled.

Future multi-tenant releases must preserve the full application's routes and
pass new-user registration, first-login market dashboard, tenant isolation
and feature coverage checks before public cutover. See
[`docs/multitenant-roadmap.md`](../../docs/multitenant-roadmap.md).

## Locally issued IP certificate

This deployment uses a private root CA and an IP SAN certificate, without a
domain or ACME dependency. Run the issuer as root on the server with OpenSSL 3,
`flock` and Docker installed. Create `.edge.env` with the pinned Nginx image
(1.27.3 or newer is required for dynamic upstream DNS resolution).

```bash
chmod 700 issue-local-cert.sh renew-cert.sh
./issue-local-cert.sh
# If active.conf is already bind-mounted, write in place to preserve its inode.
cat https.conf > active.conf
docker compose -p tsp-edge --env-file .edge.env -f compose.yml up -d edge
docker exec tsp-edge nginx -t
docker exec tsp-edge nginx -s reload
```

The CA key remains in `/etc/tsp-edge/ca/root-ca.key` (root only). Only the
`/etc/tsp-edge/tls/` directory is mounted read-only into Nginx. Keep an encrypted,
access-controlled backup of the CA key and certificate; never distribute the
CA private key. The issuer refuses to replace a missing or incomplete CA
silently. The root lasts ten years; server certificates last 365 days.

The default IP is `118.196.108.188`. For a different IP, set `TSP_EDGE_IP` in
`.edge.env` and in the issuer environment, and update `https.conf`'s redirect
and `server_name`. Update `control.cells.api_url` to the public HTTPS origin
before exposing Cell workspaces. Test migrated data and authenticated tenant
routes before the public cutover. HTTP redirects to HTTPS; `/edge-health`
remains available locally for the container health check.

## Client trust

Distribute only `/etc/tsp-edge/ca/root-ca.crt`. Verify its SHA-256 fingerprint
over a trusted channel before importing it into the device's trusted root
store. On macOS, import into Keychain Access and explicitly trust it for SSL;
on Windows, import into Trusted Root Certification Authorities. Firefox may
require import in its own Authorities settings. Restart the browser.

Browsers will show a certificate warning until this root is trusted. Do not
instruct users to disable certificate validation.

```bash
openssl x509 -in /etc/tsp-edge/ca/root-ca.crt -noout -fingerprint -sha256
curl --cacert /etc/tsp-edge/ca/root-ca.crt https://118.196.108.188/health/ready
```

## Renewal

Install the systemd service and timer from `systemd/`:

```bash
install -m 644 systemd/tsp-edge-cert-renew.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tsp-edge-cert-renew.timer
systemctl start tsp-edge-cert-renew.service
systemctl list-timers tsp-edge-cert-renew.timer
```

The timer checks daily and renews within 30 days of expiry. `renew-cert.sh`
verifies the IP, CA chain and matching key, validates Nginx, and reloads it.
Leaf rotation retains the same CA, so trusted clients need no reinstallation.
`./issue-local-cert.sh --force` rotates the server key and certificate
immediately; follow it with Nginx validation and reload. Concurrent issuance
is serialized with a filesystem lock.

Back up the active edge configuration and both PostgreSQL databases before
cutover. On rollback, restore the prior route and matching Cell directory URL;
do not overwrite business data written after cutover with an older snapshot.
