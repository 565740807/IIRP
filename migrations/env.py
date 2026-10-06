from alembic import context
from iirp.db import engine
from iirp.models import Base

if context.is_offline_mode():
    raise RuntimeError("Migrations require a real PostgreSQL connection.")
with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
