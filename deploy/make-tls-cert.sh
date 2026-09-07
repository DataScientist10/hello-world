#!/bin/sh
# Create an offline CA and a hub certificate, entirely on this machine.
#
# No public CA is involved (and none is reachable), so the depot is its own
# trust root: the CA certificate is copied to each vehicle and pinned there.
#
#   ./deploy/make-tls-cert.sh iot-hub.depot.local 10.20.0.10
set -eu

HOSTNAME="${1:-iot-hub.depot.local}"
IP="${2:-127.0.0.1}"
OUT="${OUT:-./tls}"
DAYS="${DAYS:-3650}"        # long-lived on purpose: nothing here can renew online

mkdir -p "$OUT"
cd "$OUT"

if [ ! -f ca.key ]; then
    echo "creating the depot CA"
    openssl req -x509 -newkey rsa:4096 -sha256 -days "$DAYS" -nodes \
        -keyout ca.key -out ca.crt -subj "/CN=Depot Offline CA/O=Fleet Operations"
fi

cat > hub.ext <<EXT
subjectAltName = DNS:${HOSTNAME}, IP:${IP}
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
basicConstraints = critical, CA:FALSE
EXT

openssl req -newkey rsa:2048 -sha256 -nodes -keyout hub.key -out hub.csr -subj "/CN=${HOSTNAME}"
openssl x509 -req -in hub.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out hub.crt -days "$DAYS" -sha256 -extfile hub.ext
rm -f hub.csr hub.ext

chmod 600 ca.key hub.key
echo
echo "wrote $(pwd)/hub.crt and hub.key  -> set HUB_TLS_CERT / HUB_TLS_KEY"
echo "copy $(pwd)/ca.crt to every vehicle and pin it in the agent's TLS context"
echo "keep ca.key offline; it is the trust root for the whole fleet"
