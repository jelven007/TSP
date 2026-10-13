# HTTPS Edge

The edge stack binds public ports 80 and 443. It keeps the legacy platform and
the Cell API on private Docker networks.

## Initial IP certificate

Install Certbot 5.4 or newer in `/opt/tsp-certbot`, create `.edge.env` with the
pinned Nginx image, copy `bootstrap.conf` to `active.conf`, and start the edge
service. Then request the short-lived IP certificate:

```bash
docker compose -p tsp-edge --env-file .edge.env -f compose.yml up -d edge
/opt/tsp-certbot/bin/certbot certonly \
  --preferred-profile shortlived \
  --webroot --webroot-path ./certbot/www \
  --ip-address 118.196.108.188 \
  --cert-name tsp-edge \
  --email jelven@126.com \
  --agree-tos --no-eff-email --non-interactive
```

After issuance, copy `https.conf` to `active.conf`, validate with `nginx -t`,
and reload the edge container.

## Renewal

Install the systemd service and timer from `systemd/`. The timer runs every
12 hours. `renew-cert.sh` performs Certbot renewal, validates the Nginx
configuration, and reloads Nginx.

IP certificates use Let's Encrypt's `shortlived` profile and expire after
roughly six days, so the renewal timer is required.
