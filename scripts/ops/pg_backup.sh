#!/bin/sh
# RC-10 / OPS-002: 每日生产备份（自定义格式压缩），保留 7 份日备 + 4 份周备。
# 用法（服务器）：crontab -> 30 2 * * * /opt/gpt-market/scripts/ops/pg_backup.sh
set -eu
BACKUP_DIR=/opt/gpt-market/backups
STAMP=$(date +%Y%m%d-%H%M%S)
DAILY="$BACKUP_DIR/gpt_market_daily_$STAMP.dump"

mkdir -p "$BACKUP_DIR"
docker exec gpt-market-postgres pg_dump -U gpt_market -d gpt_market -Fc > "$DAILY"
chmod 600 "$DAILY"

# 周备：周一归档一份（UTC+8 周一），保留 4 份
if [ "$(date +%u)" = "1" ]; then
  cp "$DAILY" "$BACKUP_DIR/gpt_market_weekly_$STAMP.dump"
  chmod 600 "$BACKUP_DIR/gpt_market_weekly_$STAMP.dump"
  ls -1t "$BACKUP_DIR"/gpt_market_weekly_*.dump | tail -n +5 | xargs -r rm --
fi
ls -1t "$BACKUP_DIR"/gpt_market_daily_*.dump | tail -n +8 | xargs -r rm --

SIZE=$(du -h "$DAILY" | cut -f1)
echo "$(date '+%F %T') OK $DAILY size=$SIZE" >> "$BACKUP_DIR/backup.log"
