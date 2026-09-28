"""Exercise fresh and previous-release upgrades in disposable PostgreSQL schemas.

TEST_DATABASE_URL must name a dedicated local/CI test database. Never production.
Usage: python scripts/check_migrations.py [--previous-ref v4.19.4]
"""

import argparse
import ast
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def previous_head(ref):
    revisions = set()
    parents = set()
    for path in git("ls-tree", "-r", "--name-only", ref, "alembic/versions").splitlines():
        if not path.endswith(".py"):
            continue
        for node in ast.parse(git("show", f"{ref}:{path}")).body:
            if isinstance(node, ast.AnnAssign):
                name = getattr(node.target, "id", "")
            elif isinstance(node, ast.Assign):
                name = getattr(node.targets[0], "id", "")
            else:
                continue
            if name not in ("revision", "down_revision"):
                continue
            value = ast.literal_eval(node.value)
            if name == "revision":
                revisions.add(value)
            elif value:
                parents.update(value if isinstance(value, tuple | list) else [value])
    heads = revisions - parents
    assert len(heads) == 1, f"Expected one migration head at {ref}: {heads}"
    return heads.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-ref")
    args = parser.parse_args()
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql" or not (url.database or "").startswith(
        "cartarch_test"
    ):
        raise SystemExit("Use a disposable PostgreSQL database named cartarch_test*.")
    ref = args.previous_ref
    if not ref:
        # A tagged release tests its predecessor; an untagged checkout tests latest tag.
        exact = subprocess.run(
            ["git", "describe", "--exact-match", "--tags"], cwd=ROOT, capture_output=True
        )
        ref = git("describe", "--tags", "--abbrev=0", "HEAD^" if exact.returncode == 0 else "HEAD")
    revision = previous_head(ref)
    engine = create_engine(url)
    for scenario in ("fresh", "upgrade"):
        schema = "migration_check_" + uuid.uuid4().hex
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        scoped = create_engine(scoped_url)
        env = dict(os.environ, DATABASE_URL=scoped_url.render_as_string(hide_password=False))

        def migrate(target, env=env):
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", target], cwd=ROOT, env=env, check=True
            )

        try:
            if scenario == "upgrade":
                migrate(revision)
                with scoped.begin() as conn:
                    conn.execute(
                        text("""INSERT INTO users
                        (username, password_hash, is_active, is_admin, deck_view_mode, deck_group_by, created_at)
                        VALUES ('migration@example.invalid', 'sentinel', true, false, 'list', 'type', now())""")
                    )
            migrate("head")
            # Compare table/column coverage; constraints are exercised by the PG suite.
            os.environ["DATABASE_URL"] = scoped_url.render_as_string(hide_password=False)
            from app import legacy_tables, models  # noqa: F401
            from app.db import Base

            inspector = inspect(scoped)
            for table in Base.metadata.sorted_tables:
                actual = {column["name"] for column in inspector.get_columns(table.name)}
                assert set(table.columns.keys()) <= actual, table.name
            if scenario == "upgrade":
                with scoped.connect() as conn:
                    row = conn.execute(
                        text(
                            "SELECT password_hash, session_version, deck_view_mode FROM users WHERE username='migration@example.invalid'"
                        )
                    ).one()
                    assert tuple(row) == ("sentinel", 0, "list"), row
            print(f"PASS {scenario} migrations (previous release {ref}, {revision})", flush=True)
        finally:
            scoped.dispose()
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


if __name__ == "__main__":
    main()
