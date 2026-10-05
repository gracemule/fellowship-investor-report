from __future__ import annotations

import pytest
from dotenv import find_dotenv, load_dotenv

from chui_reporter import config
from chui_reporter.extract.workbook import Workbook

load_dotenv(find_dotenv(usecwd=True), override=False)


def _book(path):
    if not path.exists():
        pytest.skip(f"source document not available: {path.name}")
    return Workbook(path)


@pytest.fixture(scope="session")
def lp() -> Workbook:
    return _book(config.LP_WORKPAPER)


@pytest.fixture(scope="session")
def gp() -> Workbook:
    return _book(config.GP_WORKPAPER)


@pytest.fixture(scope="session")
def fund_model() -> Workbook:
    return _book(config.FUND_MODEL)


@pytest.fixture()
def store():
    """A Store in a throwaway schema on the real Neon database, dropped after.
    Real Postgres on purpose: JSONB, upserts and the pooler are exactly what a
    SQLite stand-in would hide."""
    import os
    import uuid

    from chui_reporter.agent.store import Store

    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    s = Store(schema=f"t_{uuid.uuid4().hex[:10]}")
    try:
        yield s
    finally:
        s.drop_schema()
