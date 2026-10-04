from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from iirp.config import settings


@lru_cache
def engine():
    return create_engine(
        settings().database_url,
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 3,
            "options": "-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=500",
        },
    )


def session() -> Session:
    return Session(engine(), expire_on_commit=False)
