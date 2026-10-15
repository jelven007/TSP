# HTTPS Edge

The edge stack binds public ports 80 and 443 and reaches the complete
multi-tenant API through its private Docker network.

## Production entry

`https.conf` forwards to `tsp-full-api:3018`, the API network alias from
[`deploy/full/compose.yml`](../full/compose.yml). The complete single-host
multi-tenant product went live on 2026-10-14; its recorded acceptance and
release evidence is in
[`docs/phase1-implementation.md`](../../docs/phase1-implementation.md).

The edge and API must share a Docker network. Set
`TSP_EDGE_PLATFORM_NETWORK` and `TSP_FULL_EDGE_NETWORK` to the same existing
network (both default to `app_default`). PostgreSQL and the compute Broker
are not exposed through this network. For a standalone deployment, explicitly
change the upstream to that deployment's `app:3018` and use its network.

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
and `server_name`. Update the registered `control.cells.api_url` for the full
deployment to the public HTTPS origin. Test migrated data and authenticated tenant
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
cutover. On rollback, restore the prior route and matching deployment URL;
do not overwrite business data written after cutover with an older snapshot.
