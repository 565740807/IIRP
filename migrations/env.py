from alembic import context
from iirp.db import engine
from iirp.extensions import load_models
from iirp.models import Base

if context.is_offline_mode():
    raise RuntimeError("Migrations require a real PostgreSQL connection.")
# Installed optional features declare their tables on the same metadata.
load_models()
with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
