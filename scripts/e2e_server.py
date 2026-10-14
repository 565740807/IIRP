"""Migrate, seed and serve a disposable browser stack with a frozen application clock."""

import argparse
from pathlib import Path

from freezegun import configure, freeze_time

from scripts.e2e_data import require_test_database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clock", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    require_test_database()
    from alembic import command
    from iirp.extensions import alembic_config

    command.upgrade(alembic_config(), "heads")
    # Framework schema classes must inherit real datetime types before Freezegun patches them.
    import uvicorn
    from iirp.api.app import app

    # Included routers construct effective field adapters lazily; OpenAPI prepares
    # them with real datetime types before application modules use the fixed clock.
    app.openapi()
    # Real monotonic time keeps the server's asyncio timers working. Date/time
    # reads in application modules and SQLAlchemy Python defaults stay frozen.
    # Sync FastAPI routes run in AnyIO worker threads. Freezegun's default
    # threading exclusion would let those routes see the real wall clock.
    configure(default_ignore_list=[])
    with freeze_time(args.clock, real_asyncio=True, ignore=["uvicorn"]):
        from scripts.e2e_data import seed

        seed(args.output / "fixtures.json")
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
