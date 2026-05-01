#!/bin/bash
# Backup Redis data to host filesystem
# Works both inside container (via /backups mount) and from host

set -e

BACKUP_DIR="./backups/redis"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="redis_backup_${TIMESTAMP}.rdb"

echo "Creating Redis backup..."

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Check if running inside container or from host
if [ -f "/data/dump.rdb" ]; then
    # Inside container - copy directly
    cp /data/dump.rdb "$BACKUP_DIR/$BACKUP_FILE"
else
    # From host - use docker compose
    # Trigger Redis SAVE command to create .rdb file
    docker compose exec -T redis redis-cli -a "${REDIS_PASSWORD:-redis_secure_password_change_me}" SAVE

    # Copy the .rdb file from container to host
    docker cp code-review-redis-1:/data/dump.rdb "$BACKUP_DIR/$BACKUP_FILE"
fi

echo "Backup saved to: $BACKUP_DIR/$BACKUP_FILE"
echo "Latest 5 backups:"
ls -lt "$BACKUP_DIR" | head -6

# Keep only last 10 backups
echo "Cleaning old backups (keeping last 10)..."
cd "$BACKUP_DIR" && ls -t redis_backup_*.rdb | tail -n +11 | xargs -r rm --

echo "Backup complete!"
