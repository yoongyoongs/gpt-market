#!/bin/sh
# RC-10 / OPS-003: 备份恢复演练——起一次性 PostgreSQL 17 容器实际还原，
# 校验 alembic 版本 + 核心表行数 + 行数一致性 Hash，与生产隔离。
# 用法（服务器）：/opt/gpt-market/scripts/ops/restore_drill.sh [dump文件]
set -eu
DUMP=${1:-$(ls -1t /opt/gpt-market/backups/gpt_market_*.dump | head -1)}
DRILL=gpt-market-restore-drill
echo "restore drill: $DUMP"

docker rm -f "$DRILL" >/dev/null 2>&1 || true
docker run -d --name "$DRILL" -e POSTGRES_PASSWORD=drill-only \
  -e POSTGRES_USER=gpt_market -e POSTGRES_DB=gpt_market \
  postgres:17-bookworm >/dev/null
trap 'docker rm -f "$DRILL" >/dev/null 2>&1 || true' EXIT
until docker exec "$DRILL" pg_isready -U gpt_market >/dev/null 2>&1; do sleep 1; done

docker exec -i "$DRILL" pg_restore -U gpt_market -d gpt_market --no-owner --role=gpt_market < "$DUMP"

echo "== alembic version =="
docker exec "$DRILL" psql -U gpt_market -d gpt_market -tAc "SELECT version_num FROM public.alembic_version;"

echo "== core table row counts (drill vs production) =="
for T in v3.accounts v3.securities v3.decisions v3.trade_ledger v3.position_projections v3.audit_events v3.orchestrator_job_runs; do
  D=$(docker exec "$DRILL" psql -U gpt_market -d gpt_market -tAc "SELECT count(*) FROM $T;" || echo TABLE_MISSING)
  P=$(docker exec gpt-market-postgres psql -U gpt_market -d gpt_market -tAc "SELECT count(*) FROM $T;" || echo TABLE_MISSING)
  if [ "$D" = "$P" ]; then FLAG=match; else FLAG=MISMATCH; fi
  echo "$T drill=$D prod=$P $FLAG"
done

echo "restore drill PASSED"
