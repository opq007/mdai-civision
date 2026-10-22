"""The live model catalog, cached and refreshable.

The list of video models civision offers changes over time, so it is pulled
from upstream rather than hard-coded, then cached in memory for a few
minutes. Aliases (the `sora-2` style names) live in the database so an
operator can repoint them without a redeploy.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.civision.client import CivisionClient
from app.civision.mapping import (
    DEFAULT_ALIASES,
    ResolvedModel,
    build_catalog,
    resolve_model as _resolve_model,
    resolve_seconds as _resolve_seconds,
    resolve_size as _resolve_size,
)
from app.models import ModelAlias

CACHE_TTL_S = 300.0


@dataclass
class Catalog:
    models: list[ResolvedModel] = field(default_factory=list)
    fetched_at: float = 0.0
    source: str = "empty"

    def resolve(self, requested: str) -> ResolvedModel | None:
        return _resolve_model(requested, self.models)

    def resolve_size(self, size: str | None) -> tuple[int, int]:
        return _resolve_size(size)

    def resolve_seconds(self, seconds) -> int:
        return _resolve_seconds(seconds)


class CatalogService:
    def __init__(self) -> None:
        self._cache: Catalog | None = None
        self._lock = asyncio.Lock()

    async def _aliases(self, session: AsyncSession) -> dict[str, str]:
        rows = (
            await session.execute(select(ModelAlias))
        ).scalars().all()
        aliases = dict(DEFAULT_ALIASES)
        for row in rows:
            aliases[row.alias] = row.civision_value
        return aliases

    async def refresh(self, session: AsyncSession) -> Catalog:
        client = CivisionClient()
        raw = await client.model_catalog()
        aliases = await self._aliases(session)
        catalog = Catalog(
            models=build_catalog(raw, aliases),
            fetched_at=time.time(),
            source="upstream",
        )
        self._cache = catalog
        return catalog

    async def get(self, session: AsyncSession | None = None) -> Catalog:
        """Return the catalog, refreshing it from upstream when stale.

        The caller may pass a session; if not, one is opened here. Aliases
        live in the database, so a session is needed to resolve them.
        """
        if self._cache and time.time() - self._cache.fetched_at < CACHE_TTL_S:
            return self._cache

        if session is not None:
            return await self._refresh_or_keep(session)

        from app.db import session_scope

        async with session_scope() as own:
            return await self._refresh_or_keep(own)

    async def _refresh_or_keep(self, session: AsyncSession) -> Catalog:
        async with self._lock:
            if self._cache and time.time() - self._cache.fetched_at < CACHE_TTL_S:
                return self._cache
            try:
                return await self.refresh(session)
            except Exception:
                # Keep serving the last good catalog rather than failing
                # every request because upstream is briefly unavailable.
                if self._cache:
                    return self._cache
                return Catalog(models=build_catalog([], DEFAULT_ALIASES))

    def invalidate(self) -> None:
        self._cache = None


catalog_service = CatalogService()
