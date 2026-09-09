#!/usr/bin/env bash
set -euo pipefail

/opt/test/bootstrap.sh
exec /opt/hermes/docker/entrypoint-dispatch.sh gateway run --no-supervise
