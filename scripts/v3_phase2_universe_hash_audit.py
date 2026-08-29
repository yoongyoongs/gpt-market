from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.market_data import (
    Market,
    SecurityMember,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from app.v3.infrastructure.db.models import (
    SecurityModel,
    UniverseMemberModel,
    UniverseSnapshotModel,
    UniverseSourceModel,
)
from app.v3.infrastructure.providers.universe import ExchangeUniverseProvider


async def audit(database_url: str) -> dict:
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            snapshot, source_code = (
                await session.execute(
                    select(UniverseSnapshotModel, UniverseSourceModel.code)
                    .join(
                        UniverseSourceModel,
                        UniverseSourceModel.source_id == UniverseSnapshotModel.source_id,
                    )
                    .order_by(
                        UniverseSnapshotModel.known_at.desc(),
                        UniverseSnapshotModel.created_at.desc(),
                    )
                    .limit(1)
                )
            ).one()
            rows = (
                await session.execute(
                    select(UniverseMemberModel, SecurityModel)
                    .join(
                        SecurityModel,
                        SecurityModel.security_id == UniverseMemberModel.security_id,
                    )
                    .where(UniverseMemberModel.snapshot_id == snapshot.snapshot_id)
                    .order_by(SecurityModel.market, SecurityModel.code)
                )
            ).all()
        database_members = tuple(
            SecurityMember(
                code=security.code,
                market=Market(security.market),
                name=member.name,
                trading_status=member.trading_status,
                is_st=member.is_st,
                suspended=member.suspended,
                is_new_listing=member.is_new_listing,
                delisting_risk=member.delisting_risk,
                raw_reference=member.raw_reference,
            )
            for member, security in rows
        )
        database_content = UniverseSnapshotContent(
            snapshot_id=snapshot.snapshot_id,
            source_code=source_code,
            status=UniverseSnapshotStatus(snapshot.status),
            as_of=snapshot.as_of,
            fetch_time=snapshot.fetch_time,
            known_at=snapshot.known_at,
            coverage=float(snapshot.coverage),
            stale=snapshot.stale,
            previous_snapshot_id=snapshot.previous_snapshot_id,
            members=database_members,
        )
        report = {
            "snapshot_id": str(snapshot.snapshot_id),
            "stored_hash": snapshot.content_hash,
            "database_hash": database_content.computed_content_hash(),
            "database_members": len(database_members),
        }
        if source_code == "official_exchanges":
            provider = ExchangeUniverseProvider(timeout=30)
            try:
                fetched = await provider.fetch_snapshot()
            finally:
                await provider.close()
            live_content = UniverseSnapshotContent(
                **database_content.model_dump(exclude={"members"}),
                members=fetched.members,
            )
            database_by_key = {
                (member.market.value, member.code): member.model_dump(mode="json")
                for member in database_content.members
            }
            live_by_key = {
                (member.market.value, member.code): member.model_dump(mode="json")
                for member in live_content.members
            }
            changed = [
                key
                for key in sorted(database_by_key.keys() & live_by_key.keys())
                if database_by_key[key] != live_by_key[key]
            ]
            report.update(
                {
                    "live_hash_with_database_metadata": live_content.computed_content_hash(),
                    "live_members": len(live_content.members),
                    "database_only": len(database_by_key.keys() - live_by_key.keys()),
                    "live_only": len(live_by_key.keys() - database_by_key.keys()),
                    "changed_count": len(changed),
                    "changed_sample": [
                        {
                            "key": key,
                            "database": database_by_key[key],
                            "live": live_by_key[key],
                        }
                        for key in changed[:10]
                    ],
                }
            )
        return report
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit latest V3 Universe hash round-trip")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(audit(args.database_url)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
