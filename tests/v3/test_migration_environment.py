from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.v3.infrastructure.db.base import Base


ROOT = Path(__file__).resolve().parents[2]


def test_alembic_environment_loads_without_database_credentials() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert Path(scripts.dir).resolve() == ROOT / "migrations"
    assert scripts.get_heads() == []


def test_v3_metadata_starts_empty_before_first_migration() -> None:
    assert list(Base.metadata.sorted_tables) == []
