# VPS Deployment Guide

Target: Ubuntu 22.04/24.04 VPS, exposed via HTTPS with Nginx reverse proxy.

---

## 1. Server Preparation

```bash
# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh
usermod -aG docker $USER
newgrp docker

# Install Docker Compose plugin
apt install -y docker-compose-plugin

# Install Nginx + Certbot
apt install -y nginx certbot python3-certbot-nginx

# Verify
docker --version
docker compose version
nginx -v
```

---

## 2. Create App User

```bash
useradd -m -s /bin/bash deploy
usermod -aG docker deploy
su - deploy
```

---

## 3. Deploy Application

```bash
# As deploy user
mkdir -p ~/code-review
cd ~/code-review

# Upload your project files (from local machine):
# rsync -av --exclude='.git' --exclude='__pycache__' ./ deploy@YOUR_VPS_IP:~/code-review/

# Or clone from git:
# git clone https://your-repo-url.git .
```

---

## 4. Configure Environment

```bash
cd ~/code-review
cp .env.example .env
nano .env
```

Fill in all values:

```env
ANTHROPIC_API_KEY=sk-ant-...
AI_MODEL=claude-sonnet-4-6
AI_MAX_TOKENS=4096
AI_MAX_DIFF_TOKENS=8000

REDIS_URL=redis://redis:6379/0

BITBUCKET_WEBHOOK_SECRET=generate-a-strong-random-secret
BITBUCKET_USERNAME=your-bitbucket-username
BITBUCKET_APP_PASSWORD=your-bitbucket-app-password

MAX_DIFF_LINES=500
```

Generate a secure webhook secret:
```bash
openssl rand -hex 32
```

Lock down the env file:
```bash
chmod 600 .env
```

---

## 5. Build and Start

```bash
cd ~/code-review

# Build images and start all services in background
docker compose up -d --build

# Verify all containers are running
docker compose ps

# Check logs
docker compose logs -f app
docker compose logs -f worker
```

Expected output from `docker compose ps`:
```
NAME                    STATUS          PORTS
code-review-app-1       Up (healthy)    0.0.0.0:8000->8000/tcp
code-review-worker-1    Up
code-review-redis-1     Up (healthy)    0.0.0.0:6379->6379/tcp
```

Test the health endpoint:
```bash
curl http://localhost:8000/health
# Expected: {"status":"ok","redis":"ok"}
```

---

## 6. Nginx Reverse Proxy

Replace `yourdomain.com` with your actual domain.

```bash
# As root
nano /etc/nginx/sites-available/code-review
```

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Request-ID $request_id;

        # Webhook payloads can be large
        client_max_body_size 10M;
        proxy_read_timeout 30s;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/code-review /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

---

## 7. SSL Certificate

```bash
certbot --nginx -d yourdomain.com
# Follow prompts — choose "Redirect HTTP to HTTPS"

# Verify auto-renewal
certbot renew --dry-run
```

---

## 8. Firewall

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw deny 8000      # Block direct access to app port
ufw deny 6379      # Block Redis from outside
ufw enable
ufw status
```

---

## 9. Configure Bitbucket Webhook

1. In Bitbucket: **Repository Settings → Webhooks → Add webhook**
2. URL: `https://yourdomain.com/webhook/bitbucket`
3. Secret: the value of `BITBUCKET_WEBHOOK_SECRET` from your `.env`
4. Triggers: check **Pull request: Created** and **Pull request: Updated**
5. Save and use **Test connection** to verify

---

## 10. Verify End-to-End

```bash
# Watch logs while opening a test PR
docker compose logs -f worker

# Manual webhook test (get a valid signature first):
SECRET="your-webhook-secret"
PAYLOAD=$(cat tests/fixtures/bitbucket_payload.json)
SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print "sha256="$2}')
curl -X POST https://yourdomain.com/webhook/bitbucket \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature: $SIG" \
  -d "$PAYLOAD"
# Expected: {"status":"queued","task_id":"..."}
```

---

## Maintenance

### Update application

```bash
cd ~/code-review
git pull                          # or re-upload files
docker compose up -d --build      # rebuilds only changed layers
```

### View logs

```bash
docker compose logs -f            # all services
docker compose logs -f worker     # worker only
docker compose logs --tail=100 app
```

### Restart services

```bash
docker compose restart app worker
docker compose restart            # all
```

### Stop / tear down

```bash
docker compose down               # stop, keep volumes
docker compose down -v            # stop + delete redis data
```

### Celery worker scaling

```bash
# Edit docker-compose.yml → worker → command: --concurrency=8
docker compose up -d --scale worker=2   # run 2 worker containers
```

### Redis persistence

Redis data is stored in a named Docker volume (`redis_data`). To back it up:

```bash
docker run --rm \
  -v code-review_redis_data:/data \
  -v $(pwd)/backups:/backup \
  alpine tar czf /backup/redis-$(date +%Y%m%d).tar.gz /data
```

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `{"redis":"error"}` from `/health` | `docker compose ps redis` — is Redis healthy? |
| Webhook returns 401 | Verify `BITBUCKET_WEBHOOK_SECRET` matches Bitbucket config |
| Webhook returns 202 but no comments posted | `docker compose logs worker` for task errors |
| Worker retrying endlessly | Check `ANTHROPIC_API_KEY` is valid; check Bitbucket credentials |
| Container exits immediately | `docker compose logs app` — likely missing required env var |
| Semgrep timeout | Reduce `max_diff_lines` in `.codereview.yml` or increase subprocess timeout in `semgrep_runner.py` |
