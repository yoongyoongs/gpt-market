from __future__ import annotations

import pytest

from app.config import Settings
from app.v3.container import V3Container


def test_v3_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)
    container = V3Container.from_settings(settings)
    assert settings.v3_enabled is False
    assert container.enabled is False
    assert container.database is None


def test_v3_enabled_requires_database_url() -> None:
    settings = Settings(_env_file=None, v3_enabled=True, v3_database_url=None)
    with pytest.raises(ValueError, match="V3_DATABASE_URL"):
        V3Container.from_settings(settings)


def test_v3_database_configuration_is_applied() -> None:
    settings = Settings(
        _env_file=None,
        v3_enabled=True,
        v3_database_url="postgresql+asyncpg://user:password@localhost/database",
        v3_database_pool_size=3,
        v3_database_max_overflow=2,
    )
    container = V3Container.from_settings(settings)
    assert container.enabled is True
    assert container.database is not None
    assert container.database.engine.url.render_as_string(hide_password=True) == (
        "postgresql+asyncpg://user:***@localhost/database"
    )
