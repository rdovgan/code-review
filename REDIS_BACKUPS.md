# Redis Backups & Persistence

## Quick Setup

Run the setup script to configure everything:

```bash
./scripts/setup-redis-persistence.sh
```

## Configuration

Redis is configured with:
- **AOF (Append Only File)**: Enabled, syncs every second
- **RDB Snapshots**: Automatic every 15min (1 change), 5min (10 changes), 1min (10000 changes)
- **Password protection**: Enabled (set `REDIS_PASSWORD` in `.env`)
- **Network access**: Localhost only (`127.0.0.1:6379`)
- **Volume persistence**: Docker volume `redis_data` mounted to `/data`
- **Auto-restore**: Automatically restores from latest backup on container recreation

## Auto-Restore on Reboot

Redis now automatically restores data on container recreation:
1. On container start, checks for existing data in `/data`
2. If no data found, looks for latest backup in `/backups/redis/`
3. Automatically restores the most recent `redis_backup_*.rdb` file
4. Starts Redis with restored data

## Manual Backup

```bash
# Create backup manually
./scripts/redis-backup.sh

# Or trigger SAVE from within Redis
docker compose exec redis redis-cli -a $REDIS_PASSWORD SAVE
```

## Automated Backups

### Option 1: Cron (macOS/Linux)

Add to crontab for hourly backups:

```bash
# Edit crontab
crontab -e

# Add this line (hourly backups)
0 * * * * cd /path/to/code-review && ./scripts/redis-backup.sh >> logs/redis-backup.log 2>&1
```

### Option 2: Systemd Timer (Linux)

```bash
# Install service and timer
sudo cp scripts/redis-auto-backup.* /etc/systemd/system/
sudo systemctl enable redis-auto-backup.timer
sudo systemctl start redis-auto-backup.timer

# Check status
systemctl status redis-auto-backup.timer
```

## Restore from Backup

```bash
# List available backups
ls -la backups/redis/

# Manual restore (if auto-restore doesn't work)
./scripts/redis-restore.sh backups/redis/redis_backup_20240328_120000.rdb
```

## Redis Data Survival

Your metrics will survive:
- ✅ Container restarts
- ✅ Server reboots (with Docker volume + auto-restore)
- ✅ App redeploys (automatic backup before redeploy recommended)
- ✅ Container recreation (auto-restore from latest backup)

Your metrics will NOT survive:
- ❌ Volume deletion (`docker compose down -v`)
- ❌ Manual Redis FLUSHALL
- ❌ Deleting backups/redis/ directory

## Monitoring Metrics Retention

Check current metrics in Redis:

```bash
# Connect to Redis CLI
docker compose exec redis redis-cli -a $REDIS_PASSWORD

# Check all metrics keys
KEYS metrics:*

# Check specific metric
HGETALL metrics:reviews
HGETALL metrics:webhooks

# Check memory usage
INFO memory
```

## Reset Metrics (if needed)

```bash
# Clear all metrics
docker compose exec redis redis-cli -a $REDIS_PASSWORD
> DEL metrics:webhooks metrics:reviews metrics:findings:severity
> DEL metrics:by_lang metrics:by_project metrics:by_author
> DEL metrics:findings_by_lang metrics:findings_by_project metrics:findings_by_author
> DEL metrics:duration:sum_ms metrics:duration:count
> EXIT
```
