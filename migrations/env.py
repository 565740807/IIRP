from alembic import context
from iirp.db import engine
from iirp.models import Base

# Versioned price tables replaced by the 24-hour cache (migration 0025). The
# application no longer reads or writes them; their data is dropped in a
# separate, confirmed cleanup step, after which this list goes away.
LEGACY_TABLES = {"price_dataset_version", "market_bar_revision", "dataset_bar", "corporate_action"}


def include_object(item, name, type_, reflected, compare_to):
    table = item if type_ == "table" else getattr(item, "table", None)
    return not (reflected and table is not None and table.name in LEGACY_TABLES)


if context.is_offline_mode():
    raise RuntimeError("Migrations require a real PostgreSQL connection.")
with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()
