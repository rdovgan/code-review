# Redis Backups & Persistence

## Configuration

Redis is configured with:
- **AOF (Append Only File)**: Enabled, syncs every second
- **Password protection**: Enabled (set `REDIS_PASSWORD` in `.env`)
- **Network access**: Localhost only (`127.0.0.1:6379`)
- **Volume persistence**: Docker volume `redis_data` mounted to `/data`

## Before Redeploy

Always backup Redis data before redeploying:

```bash
# Manual backup
./scripts/redis-backup.sh

# Or trigger SAVE from within Redis
docker compose exec redis redis-cli -a $REDIS_PASSWORD SAVE
```

## Automated Backups

Add to crontab for daily backups:

```bash
# Daily backup at 2 AM
0 2 * * * cd /path/to/code-review && ./scripts/redis-backup.sh >> /var/log/redis-backup.log 2>&1
```

## Restore from Backup

```bash
# List available backups
ls -la backups/redis/

# Restore specific backup
./scripts/redis-restore.sh backups/redis/redis_backup_20240328_120000.rdb
```

## Redis Data Survival

Your metrics will survive:
- ✅ Container restarts
- ✅ Server reboots (with Docker volume)
- ✅ App redeploys (if you backup first)

Your metrics will NOT survive:
- ❌ Volume deletion (`docker compose down -v`)
- ❌ Manual Redis FLUSHALL
- ❌ Container recreation without backup

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
