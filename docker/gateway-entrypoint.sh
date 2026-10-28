#!/bin/sh
set -eu
umask 077
# Named volumes only; no host paths or Docker socket are mounted.
mkdir -p /state /workspace /models
chown 10001:10001 /state /workspace /models
gosu pocket:pocket chmod 700 /state /workspace /models
exec gosu pocket:pocket python /app/docker/runtime.py "$@"
