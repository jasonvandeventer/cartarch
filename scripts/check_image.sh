#!/usr/bin/env bash
# Test an already-built image; the publisher must push this very image afterwards.
set -euo pipefail
image=${1:?usage: scripts/check_image.sh IMAGE}
name="cartarch-check-$$"
port=${SMOKE_PORT:-5581}
cleanup() {
  docker logs "$name-app" > "${SMOKE_LOG:-/tmp/cartarch-image.log}" 2>&1 || true
  docker rm -f "$name-app" "$name-pg" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$name" >/dev/null
docker run -d --name "$name-pg" --network "$name" \
  -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=cartarch_test postgres:18-alpine >/dev/null
ready=false
for _ in {1..60}; do
  if docker exec "$name-pg" pg_isready -U postgres -d cartarch_test >/dev/null; then ready=true; break; fi
  sleep 1
done
$ready || { echo 'Postgres failed to start'; exit 1; }
database="postgresql+psycopg://postgres:local-test-only@$name-pg:5432/cartarch_test"
docker run --rm --network "$name" -e DATABASE_URL="$database" "$image" python -m alembic upgrade head
docker run --rm --network "$name" -e DATABASE_URL="$database" "$image" python scripts/seed_smoke.py
docker run -d --name "$name-app" --network "$name" -p "127.0.0.1:$port:8000" \
  -e HTTPS_PROXY=http://127.0.0.1:9 -e HTTP_PROXY=http://127.0.0.1:9 \
  -e DATABASE_URL="$database" -e DEV_MODE=true -e SESSION_SECRET_KEY=local-smoke-secret "$image" >/dev/null
export SMOKE_URL="http://127.0.0.1:$port"
ready=false
for _ in {1..60}; do
  if curl -fsS "$SMOKE_URL/health" >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
$ready || { docker logs "$name-app"; exit 1; }
if [[ -n ${EXPECTED_VERSION:-} ]]; then
  docker exec -e EXPECTED_VERSION="$EXPECTED_VERSION" "$name-app" python -c 'import os; assert os.environ["APP_VERSION"] == os.environ["EXPECTED_VERSION"]'
fi
"${PYTHON:-python}" scripts/smoke_http.py
node tests/browser/deck-menus.cjs
# Optional local inspection before teardown; CI leaves this unset.
if [[ ${SMOKE_HOLD_SECONDS:-0} -gt 0 ]]; then sleep "$SMOKE_HOLD_SECONDS"; fi
