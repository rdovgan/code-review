#!/bin/bash
# Setup Redis persistence with auto-backup and auto-restore

set -e

echo "Setting up Redis persistence for code-review..."
echo ""

# Create necessary directories
mkdir -p backups/redis
chmod 755 backups/redis

# Make scripts executable
chmod +x scripts/redis-backup.sh scripts/redis-restore.sh scripts/redis-entrypoint.sh

echo "✓ Created backups/redis directory"
echo "✓ Made scripts executable"
echo ""

# Check if Docker volume exists
echo "Checking Docker volume..."
if docker volume inspect code-review_redis_data &>/dev/null; then
    echo "✓ Docker volume 'code-review_redis_data' exists"
else
    echo "→ Creating Docker volume 'code-review_redis_data'..."
    docker volume create code-review_redis_data
    echo "✓ Volume created"
fi
echo ""

# Backup current Redis data if running
echo "Checking for running Redis..."
if docker compose ps redis &>/dev/null | grep -q "Up"; then
    echo "→ Creating backup of current Redis data..."
    ./scripts/redis-backup.sh
else
    echo "→ Redis not running (will be backed up on first run)"
fi
echo ""

echo "========================================="
echo "Setup complete!"
echo "========================================="
echo ""
echo "What's configured:"
echo "  • Redis AOF enabled (appendfsync everysec)"
echo "  • RDB snapshots: every 15min (1 change), 5min (10 changes), 1min (10000 changes)"
echo "  • Docker volume for /data persistence"
echo "  • Auto-restore from latest backup on container recreation"
echo "  • Backup script at ./scripts/redis-backup.sh"
echo ""
echo "To start with persistence:"
echo "  docker compose down"
echo "  docker compose up -d redis"
echo ""
echo "To enable automatic hourly backups (macOS):"
echo "  crontab -e"
echo "  Add: 0 * * * * cd $(pwd) && ./scripts/redis-backup.sh >> logs/redis-backup.log 2>&1"
echo ""
echo "To enable automatic hourly backups (Linux with systemd):"
echo "  sudo cp scripts/redis-auto-backup.* /etc/systemd/system/"
echo "  sudo systemctl enable redis-auto-backup.timer"
echo "  sudo systemctl start redis-auto-backup.timer"
