#!/usr/bin/env bash
set -euo pipefail
umask 077

# The CA directory is never mounted into Nginx or copied to client devices.
PKI_DIR="${TSP_EDGE_PKI_DIR:-/etc/tsp-edge}"
EDGE_IP="${TSP_EDGE_IP:-118.196.108.188}"
CERT_DAYS="${TSP_EDGE_CERT_DAYS:-365}"
RENEW_DAYS="${TSP_EDGE_RENEW_DAYS:-30}"
FORCE=false
if [[ "${1:-}" == "--force" && $# == 1 ]]; then
  FORCE=true
elif (( $# > 0 )); then
  echo "Usage: $0 [--force]" >&2
  exit 2
fi

[[ "$EDGE_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || {
  echo "TSP_EDGE_IP must be an IPv4 address" >&2
  exit 2
}
IFS=. read -r -a OCTETS <<< "$EDGE_IP"
for octet in "${OCTETS[@]}"; do
  (( 10#$octet <= 255 )) || exit 2
done
[[ "$CERT_DAYS" =~ ^[1-9][0-9]{0,2}$ && "$RENEW_DAYS" =~ ^(0|[1-9][0-9]?)$ ]] || exit 2
(( CERT_DAYS <= 365 && RENEW_DAYS < CERT_DAYS )) || exit 2

install -d -m 700 "$PKI_DIR" "$PKI_DIR/ca" "$PKI_DIR/tls"
exec 9>"$PKI_DIR/.issue.lock"
flock -x 9

CA_KEY="$PKI_DIR/ca/root-ca.key"
CA_CERT="$PKI_DIR/ca/root-ca.crt"
TLS_KEY="$PKI_DIR/tls/privkey.pem"
TLS_CERT="$PKI_DIR/tls/fullchain.pem"
STAGE="$(mktemp -d "$PKI_DIR/.issue.XXXXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

key_matches() {
  local certificate="$1" key="$2" cert_digest key_digest
  cert_digest="$(openssl x509 -in "$certificate" -pubkey -noout |
    openssl pkey -pubin -outform DER | openssl dgst -sha256)"
  key_digest="$(openssl pkey -in "$key" -pubout -outform DER | openssl dgst -sha256)"
  [[ "$cert_digest" == "$key_digest" ]]
}

if [[ ! -e "$CA_KEY" && ! -e "$CA_CERT" ]]; then
  # Refuse to silently replace a CA that existing client devices might trust.
  if [[ -e "$TLS_CERT" || -e "$TLS_KEY" ]]; then
    echo "CA missing while a server certificate exists; restore the original CA" >&2
    exit 1
  fi
  openssl req -x509 -newkey rsa:4096 -nodes -sha256 -days 3650 \
    -subj "/CN=TSP Local Root CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" \
    -keyout "$STAGE/root-ca.key" -out "$STAGE/root-ca.crt" 2>"$STAGE/openssl.log"
  install -m 600 "$STAGE/root-ca.key" "$CA_KEY"
  install -m 644 "$STAGE/root-ca.crt" "$CA_CERT"
elif [[ ! -s "$CA_KEY" || ! -s "$CA_CERT" ]]; then
  echo "Incomplete CA state; restore the matching CA key and certificate" >&2
  exit 1
fi
chmod 600 "$CA_KEY"
chmod 644 "$CA_CERT"
key_matches "$CA_CERT" "$CA_KEY" || {
  echo "CA certificate and private key do not match" >&2
  exit 1
}
openssl verify -CAfile "$CA_CERT" "$CA_CERT"
openssl x509 -in "$CA_CERT" -checkend "$(( CERT_DAYS * 86400 ))" -noout || {
  echo "CA expires within the requested server lifetime; plan CA rotation" >&2
  exit 1
}

if ! "$FORCE" && [[ -s "$TLS_CERT" && -s "$TLS_KEY" ]] &&
  openssl x509 -in "$TLS_CERT" -checkend "$(( RENEW_DAYS * 86400 ))" -noout &&
  openssl x509 -in "$TLS_CERT" -checkip "$EDGE_IP" -noout &&
  openssl verify -purpose sslserver -verify_ip "$EDGE_IP" -CAfile "$CA_CERT" "$TLS_CERT" &&
  key_matches "$TLS_CERT" "$TLS_KEY"; then
  echo "Server certificate is valid; renewal not needed"
  exit 0
fi

cat > "$STAGE/server.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$EDGE_IP
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -subj "/CN=TSP Edge $EDGE_IP" \
  -keyout "$STAGE/privkey.pem" -out "$STAGE/server.csr" 2>"$STAGE/openssl.log"
openssl x509 -req -sha256 -days "$CERT_DAYS" \
  -in "$STAGE/server.csr" -CA "$CA_CERT" -CAkey "$CA_KEY" \
  -set_serial "0x$(openssl rand -hex 16)" -extfile "$STAGE/server.ext" \
  -out "$STAGE/server.pem"
openssl verify -purpose sslserver -verify_ip "$EDGE_IP" \
  -CAfile "$CA_CERT" "$STAGE/server.pem"
key_matches "$STAGE/server.pem" "$STAGE/privkey.pem"
cat "$STAGE/server.pem" "$CA_CERT" > "$STAGE/fullchain.pem"

# The whole tls/ directory is bind-mounted. Atomic file replacements remain
# visible to the container; Nginx reload happens only after both files are ready.
install -m 600 "$STAGE/privkey.pem" "$PKI_DIR/tls/.privkey.new"
install -m 644 "$STAGE/fullchain.pem" "$PKI_DIR/tls/.fullchain.new"
install -m 644 "$CA_CERT" "$PKI_DIR/tls/.root-ca.new"
mv -f "$PKI_DIR/tls/.privkey.new" "$TLS_KEY"
mv -f "$PKI_DIR/tls/.fullchain.new" "$TLS_CERT"
mv -f "$PKI_DIR/tls/.root-ca.new" "$PKI_DIR/tls/root-ca.crt"
openssl x509 -in "$TLS_CERT" -noout -dates -fingerprint -sha256
