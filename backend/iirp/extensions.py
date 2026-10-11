"""Optional feature packages.

A subpackage of ``iirp`` that has an ``extension`` module is an optional
feature: a checkout may leave the whole package out. The application, the
worker, the scheduler and migrations ask this module what is installed instead
of importing such a feature directly, so nothing else changes when it is absent.

An ``extension`` module may define:

- ``ROUTERS``: FastAPI routers to mount on the application;
- ``MIGRATIONS``: a directory of Alembic revisions on its own branch;
- ``MODELS``: the module that declares its tables (imported for migrations);
- ``JOBS``: job kind → ``(lane, handler, title)``; the worker claims each kind
  in its lane (one job at a time) and calls ``handler(job, stopping)``;
- ``schedule(s)``: called by the scheduler tick inside its transaction.
"""

import importlib
import importlib.util
import os
import pkgutil
from functools import cache
from pathlib import Path

import iirp
from iirp.config import ROOT


@cache
def installed() -> tuple:
    found = []
    for info in sorted(pkgutil.iter_modules(iirp.__path__), key=lambda item: item.name):
        name = f"iirp.{info.name}.extension"
        if info.ispkg and importlib.util.find_spec(name) is not None:
            found.append(importlib.import_module(name))
    return tuple(found)


def routers() -> list:
    return [router for module in installed() for router in getattr(module, "ROUTERS", ())]


def jobs() -> dict:
    return {kind: spec for module in installed() for kind, spec in getattr(module, "JOBS", {}).items()}


def schedules() -> list:
    return [module.schedule for module in installed() if hasattr(module, "schedule")]


def load_models() -> None:
    for module in installed():
        if hasattr(module, "MODELS"):
            importlib.import_module(module.MODELS)


def version_locations() -> list[str]:
    extra = [str(Path(module.MIGRATIONS)) for module in installed() if hasattr(module, "MIGRATIONS")]
    return [str(ROOT / "migrations/versions"), *extra]


def alembic_config(cmd_opts=None):
    """``alembic.ini`` with the revision directories of every installed feature."""
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"), cmd_opts=cmd_opts)
    # alembic.ini sets path_separator = os.
    config.set_main_option("version_locations", os.pathsep.join(version_locations()))
    return config


def migration_heads() -> set[str]:
    from alembic.script import ScriptDirectory

    return set(ScriptDirectory.from_config(alembic_config()).get_heads())
