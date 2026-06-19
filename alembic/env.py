from logging.config import fileConfig

from sqlalchemy import create_engine, pool

# Register the app's full schema on Base.metadata for autogenerate:
#   - app.models      → the 21 ORM-mapped tables
#   - app.legacy_tables → the 7 raw-SQL tables (DDL-only Core Tables) that are NOT
#                          ORM models and would otherwise be invisible to autogenerate.
# DATABASE_URL is the same env-driven seam as app/db.py (Gate-#4 Phase A0): unset →
# the app's SQLite file; set → Postgres. So Alembic targets exactly the app's DB.
import app.legacy_tables  # noqa: F401,E402 — registers the 7 raw tables on Base.metadata
import app.models  # noqa: F401,E402 — registers the ORM tables on Base.metadata
from alembic import context
from app.db import DATABASE_URL, Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Point Alembic at the same database the app uses. ``%`` is escaped because
# set_main_option runs the value through configparser interpolation.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata

# Tables that exist in the database but are NOT managed by this metadata — never
# emit create/drop for them. ``sqlite_sequence`` is a SQLite-internal table created
# implicitly by AUTOINCREMENT columns; ``schema_migrations`` is the legacy
# run_migrations() ledger (retired in Phase D); ``alembic_version`` is Alembic's own
# bookkeeping (auto-ignored, listed here for clarity).
_EXCLUDED_TABLES = {"schema_migrations", "sqlite_sequence", "alembic_version"}


def include_name(name, type_, parent_names):
    if type_ == "table":
        return name not in _EXCLUDED_TABLES
    return True


def _is_sqlite() -> bool:
    return DATABASE_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI needed)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        compare_type=True,
        render_as_batch=_is_sqlite(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (real connection)."""
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
            compare_type=True,
            render_as_batch=_is_sqlite(),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
