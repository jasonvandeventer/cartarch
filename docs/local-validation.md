# Local release validation

Run from the repository root. Use Python 3.12 (the container/CI version), Node 22,
and Docker. All database/container fixtures below are disposable; never supply a
production database URL. No check pushes an image or changes the cluster.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
npm ci --ignore-scripts
npx playwright install --with-deps chromium firefox
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest tests/
```

The npm lifecycle scripts are disabled because the existing djlint wrapper tries
to install into system Python. Playwright's browser installation is explicit.

Run the PostgreSQL suite and migration rehearsal:

```sh
docker run --rm -d --name cartarch-test-pg \
  -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=cartarch_test \
  -p 127.0.0.1:55439:5432 postgres:18-alpine
export TEST_DATABASE_URL=postgresql+psycopg://postgres:local-test-only@127.0.0.1:55439/cartarch_test
# Wait for pg_isready before the checks.
docker exec cartarch-test-pg pg_isready -U postgres -d cartarch_test
.venv/bin/python scripts/check_migrations.py
.venv/bin/pytest tests/
docker stop cartarch-test-pg
unset TEST_DATABASE_URL
```

The suite recreates tables in its test database. Migration checks instead create
and remove uniquely named schemas: fresh install to head, and the previous tagged
release's revision to head with an existing user preserved. Use
`--previous-ref vX.Y.Z` to select an explicit release. A tagged checkout defaults
to its predecessor; an untagged checkout defaults to the latest reachable tag.

Run the browser and built-image gates:

```sh
PYTHON=.venv/bin/python npm run test:browser
docker build --build-arg APP_VERSION=dev -t cartarch-verified:build .
PYTHON=.venv/bin/python EXPECTED_VERSION=dev scripts/check_image.sh cartarch-verified:build
```

The image gate creates an isolated Docker network, PostgreSQL, and the actual
application container. It runs Alembic, seeds a synthetic 100-card deck, starts the
real app lifespan, tests login/CSRF/authenticated routes, and exercises menus and
mutations in Chromium and Firefox at 390/1400 pixels. Temporary containers and the
network are removed on exit; logs remain at `/tmp/cartarch-image.log`.
`SMOKE_PORT` changes the default port 5581. `SMOKE_HOLD_SECONDS` keeps the local
fixture alive briefly for manual inspection after the assertions pass.

CI invokes these same checks through `verify.yml`. Release and dev publishers
load its saved image artifact and retag/push it; they never rebuild after testing.
