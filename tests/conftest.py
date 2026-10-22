"""Test-wide setup.

The most important thing here is the **isolated database**. The service's
tests share a database with the running dev server by default, which means a
test that creates a job row can be picked up by the server's workers on the
next restart and start a real generation — spending real magicubes.

Pointing `DATABASE_URL` at a throwaway file before anything imports
`app.db` keeps the test database completely separate.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Must happen before `app.db` is imported anywhere, because the engine is
# created at import time and reads the setting then.
_TMP_DIR = tempfile.mkdtemp(prefix="civision-tests-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(_TMP_DIR) / 'test.db'}"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

# The storage directory must not point at the real videos folder.
os.environ["STORAGE_DIR"] = str(Path(_TMP_DIR) / "videos")

import pytest  # noqa: E402

from app.models import Base  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Create the schema in the throwaway test database.

    The test client does not run the application's lifespan, so `init_db`
    never fires. Without this the tables do not exist.
    """
    import asyncio

    from app.config import get_settings
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(get_settings().database_url)

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create())
    yield
    asyncio.run(engine.dispose())
