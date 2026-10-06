"""Explicit persistent legacy bootstrap; normal Runtime does not invoke it yet."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text

from knowgrain.database import ApplicationDatabase
from knowgrain.index_fence import IndexConflict
from knowgrain.index_identity import CoreIndexIdentity
from knowgrain.models import (
    CoreGeneration,
    CoreGenerationRevision,
    CoreSelector,
    SourceDocument,
    SourceRevision,
)


class CoreGenerationRepository:
    def __init__(self, database: ApplicationDatabase):
        self.database = database

    async def bootstrap_legacy(self, identity: CoreIndexIdentity) -> UUID:
        """Persist observed identity under startup quiescence, never infer a profile.

        Future startup must call before admitting any task. Existing revisions
        conservatively receive possibly-touched intents; none become verified.
        Ordinary task backfill is deferred to their coordinated active integration.
        """
        working_dir = str(identity.working_dir.resolve())
        if len(working_dir) > 4096:
            raise ValueError("Generation working path exceeds the supported length")
        async with self.database.session_factory() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(1263420247, 1196575050)"))
            selector = await session.scalar(
                select(CoreSelector).where(CoreSelector.id == 1).with_for_update()
            )
            if selector is not None:
                generation = await session.get(CoreGeneration, selector.active_generation_id)
                if generation is None or (
                    generation.workspace != identity.workspace
                    or Path(generation.working_dir) != Path(working_dir)
                    or generation.vector_model_name != identity.vector_model_name
                ):
                    raise IndexConflict("Persistent generation identity differs from bootstrap")
                return generation.id
            if await session.scalar(select(CoreGeneration.id).limit(1)) is not None:
                raise IndexConflict("Generation ledger exists without an initialized selector")
            generation_id = uuid4()
            session.add(
                CoreGeneration(
                    id=generation_id,
                    workspace=identity.workspace,
                    working_dir=working_dir,
                    vector_model_name=identity.vector_model_name,
                    config_status="legacy_unverified",
                )
            )
            await session.flush()
            session.add(CoreSelector(id=1, active_generation_id=generation_id))
            # Stable lock order: selector -> source -> revision -> member.
            await session.execute(
                select(SourceDocument.id).order_by(SourceDocument.id).with_for_update()
            )
            revisions = (
                await session.scalars(
                    select(SourceRevision)
                    .order_by(SourceRevision.source_id, SourceRevision.id)
                    .with_for_update()
                )
            ).all()
            for revision in revisions:
                session.add(
                    CoreGenerationRevision(
                        generation_id=generation_id,
                        revision_id=revision.id,
                        source_id=revision.source_id,
                        state="write_intent",
                    )
                )
            return generation_id
