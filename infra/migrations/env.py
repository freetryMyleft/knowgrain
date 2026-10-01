from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_engine_from_config

from knowgrain.config import Settings
from knowgrain.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
password = settings.postgres_password
if hasattr(password, "get_secret_value"):
    password = password.get_secret_value()
database_url = URL.create(
    "postgresql+asyncpg",
    username=settings.postgres_user,
    password=password,
    host=settings.postgres_host,
    port=settings.postgres_port,
    database=getattr(settings, "knowgrain_postgres_db", "knowgrain"),
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": database_url.render_as_string(hide_password=False)},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
