# V3 database migrations

Run migrations explicitly before starting a V3-enabled API or worker:

```bash
V3_DATABASE_URL=postgresql+asyncpg://... alembic upgrade head
V3_DATABASE_URL=postgresql+asyncpg://... alembic downgrade -1
```

The application never runs Alembic automatically. Back up PostgreSQL before a production migration and verify both upgrade and downgrade against an isolated database first.
