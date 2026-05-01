#!/bin/sh
# Redis entrypoint with auto-restore from latest backup

set -e

REDIS_PASSWORD="${REDIS_PASSWORD:-redis_secure_password_change_me}"
BACKUP_DIR="/backups"
DATA_DIR="/data"

echo "Redis entrypoint: checking for existing data..."

# Check if Redis has any data
if [ -f "$DATA_DIR/dump.rdb" ] || [ -f "$DATA_DIR/appendonly.aof" ]; then
    echo "Existing Redis data found, starting normally..."
else
    echo "No existing data found, checking for backups..."
    LATEST_BACKUP=$(ls -t "$BACKUP_DIR"/redis_backup_*.rdb 2>/dev/null | head -1)

    if [ -n "$LATEST_BACKUP" ]; then
        echo "Found backup: $LATEST_BACKUP"
        echo "Restoring from backup..."
        cp "$LATEST_BACKUP" "$DATA_DIR/dump.rdb"
        echo "Restore complete. Starting Redis..."
    else
        echo "No backups found, starting with empty database..."
    fi
fi

# Execute the command passed to the container
exec "$@"
