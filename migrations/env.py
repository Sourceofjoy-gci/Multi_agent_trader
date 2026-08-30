from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config
from sqlalchemy.pool import NullPool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ``alembic -x url=...`` is the documented way to migrate as the migrator role
# without writing a password into alembic.ini. Alembic does not wire -x to
# sqlalchemy.url on its own, so do it here -- otherwise the documented command
# silently falls back to whatever the ini holds, which is the wrong role.
_url_override = context.get_x_argument(as_dictionary=True).get("url")
if _url_override:
    config.set_main_option("sqlalchemy.url", _url_override)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )

    with connectable.begin() as connection:
        connection.exec_driver_sql("SET ROLE trading_house_owner")
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
