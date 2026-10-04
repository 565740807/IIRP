"""Synthetic installation entrypoint. The normal Compose command is unchanged."""

from alembic import command
from alembic.config import Config

from scripts.validation.identity import (
    add_test_route,
    configured_identity,
    disable_sources,
    verified_identity,
)


def main():
    configured_identity()
    command.upgrade(Config("alembic.ini"), "head")
    disable_sources()
    import uvicorn
    from iirp.api import app

    add_test_route(app, "/__iirp_test_identity__", verified_identity)
    uvicorn.run(app, host="0.0.0.0", port=18081)


if __name__ == "__main__":
    main()
