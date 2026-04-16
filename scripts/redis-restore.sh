#!/bin/bash
# Restore Redis data from backup
# Usage: ./scripts/redis-restore.sh backups/redis/redis_backup_20240328_120000.rdb

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <backup_file>"
    echo "Example: $0 backups/redis/redis_backup_20240328_120000.rdb"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "Error: Backup file not found: $BACKUP_FILE"
    exit 1
fi

echo "Restoring Redis from: $BACKUP_FILE"
echo "WARNING: This will replace current Redis data!"
read -p "Continue? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

# Stop redis to avoid conflicts
echo "Stopping Redis..."
docker compose stop redis

# Copy backup file to container
echo "Copying backup file to container..."
docker cp "$BACKUP_FILE" code-review-redis-1:/data/dump.rdb

# Start redis
echo "Starting Redis..."
docker compose start redis

# Wait for redis to be healthy
echo "Waiting for Redis to be ready..."
sleep 5

# Verify restore
echo "Verifying restore..."
docker compose exec redis redis-cli -a "${REDIS_PASSWORD:-redis_secure_password_change_me}" DBSIZE

echo "Restore complete!"
