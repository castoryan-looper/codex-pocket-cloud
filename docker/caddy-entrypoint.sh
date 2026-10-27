#!/bin/sh
set -eu
umask 077
# Provision the same persistent CA that the serving process uses. Copy only its public certificate.
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
test -s /data/caddy/pki/authorities/local/root.crt
cp /data/caddy/pki/authorities/local/root.crt /trust/root.crt.next
chmod 644 /trust/root.crt.next
mv /trust/root.crt.next /trust/root.crt
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
