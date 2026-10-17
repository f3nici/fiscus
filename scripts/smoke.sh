#!/usr/bin/env bash
# Smoke test a built Fiscus image: it starts, migrates, serves, refuses
# anonymous access to data, never caches the service worker or pages, and the
# operator CLI runs inside it.
#
#   scripts/smoke.sh fiscus:ci
set -euo pipefail

IMAGE="${1:?usage: scripts/smoke.sh <image>}"
NAME="fiscus-smoke-$$"
PORT="${SMOKE_PORT:-18000}"

cleanup() {
  docker logs "$NAME" > smoke.log 2>&1 || true
  docker rm -f "$NAME" > /dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" \
  -e FISCUS_BASE_URL="http://localhost:${PORT}" \
  "$IMAGE" > /dev/null

for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${PORT}/readyz" 2> /dev/null | grep -q '"status":"ready"'; then
    echo "ready"
    break
  fi
  sleep 2
done
curl -fsS "http://localhost:${PORT}/readyz" | grep -q '"status":"ready"' || { docker logs "$NAME"; exit 1; }

echo "serves the sign-in page"
page=$(curl -fsS "http://localhost:${PORT}/login")
grep -q 'Sign in with a passkey' <<< "$page"

echo "refuses anonymous access to data"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/accounts")
test "$code" = "303" || { echo "expected 303 to /login, got $code"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Accept: application/json' "http://localhost:${PORT}/accounts")
test "$code" = "401" || { echo "expected 401, got $code"; exit 1; }

echo "pages and the service worker are never cached"
grep -qi 'cache-control: no-store' <<< "$(curl -sI "http://localhost:${PORT}/login")"
grep -qi 'cache-control: no-cache, no-store' <<< "$(curl -sI "http://localhost:${PORT}/sw.js")"

# Output is captured before grepping: with pipefail, `cmd | grep -q` fails
# whenever grep exits early and cmd is killed by SIGPIPE.
echo "prints a bootstrap link on first run"
logs=$(docker logs "$NAME" 2>&1)
grep -q '/setup/' <<< "$logs"

echo "the operator CLI runs, as a non-root user"
users=$(docker exec "$NAME" python -m fiscus.cli users)
grep -q 'No users yet' <<< "$users"
test "$(docker exec "$NAME" id -u)" != "0"

echo "smoke test passed"
