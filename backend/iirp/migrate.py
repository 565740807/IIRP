"""Alembic with the revisions of every installed feature.

Use it in place of ``python -m alembic``, for example
``python -m iirp.migrate upgrade heads``: a feature keeps its revisions on its
own branch, so the database has one head per installed branch.
"""

import sys

from alembic.config import CommandLine

from iirp.extensions import alembic_config


def main(argv=None):
    cli = CommandLine(prog="python -m iirp.migrate")
    options = cli.parser.parse_args(argv)
    if not hasattr(options, "cmd"):
        cli.parser.error("too few arguments")
    cli.run_cmd(alembic_config(cmd_opts=options), options)


if __name__ == "__main__":
    main(sys.argv[1:])
