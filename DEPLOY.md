# VPS Deployment Guide

Target: Ubuntu 22.04/24.04 LTS, Docker Compose stack, HTTPS via Nginx + Let's Encrypt.

**Recommended VPS specs:** 4 vCPU, 8 GB RAM, 80 GB SSD
**Minimum:** 2 vCPU, 4 GB RAM, 40 GB SSD

---

## 1. Server Preparation

```bash
# Log in as root, then:

# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Docker Compose plugin and Nginx + Certbot
apt install -y docker-compose-plugin nginx certbot python3-certbot-nginx

# Verify
docker --version
docker compose version
nginx -v
```

---

## 2. Create App User

Run the bot as a non-root user with Docker access.

```bash
useradd -m -s /bin/bash deploy
usermod -aG docker deploy
```

All subsequent steps are run as the `deploy` user unless noted otherwise.

```bash
su - deploy
```

---

## 3. Upload Application Files

```bash
mkdir -p ~/code-review
cd ~/code-review
```

From your local machine:
```bash
rsync -av --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  ./ deploy@YOUR_VPS_IP:~/code-review/
```

Or from git:
```bash
git clone https://your-repo-url.git .
```

---

## 4. Configure Environment

```bash
cd ~/code-review
cp .env.example .env
nano .env
```

```env
ANTHROPIC_API_KEY=sk-ant-...
AI_MODEL=claude-sonnet-4-6
AI_MAX_TOKENS=4096
AI_MAX_DIFF_TOKENS=8000

REDIS_URL=redis://redis:6379/0

MAX_DIFF_LINES=500
```

```bash
chmod 600 .env
```

---

## 5. Configure Credentials

Bitbucket credentials are stored per workspace (organization) in `config/credentials.yml`.
This file is **never committed to git**.

```bash
cp config/credentials.yml.example config/credentials.yml
nano config/credentials.yml
chmod 600 config/credentials.yml
```

Add one block per Bitbucket workspace:

```yaml
bitbucket:
  workspaces:

    first-workspace:
      username: alice
      app_password: ATBBxxxxxxxxxxxxxxxxxxxxxxxxxxxx
      webhook_secret: <output of: openssl rand -hex 32>

    second-workspace:
      username: bob
      app_password: ATBByyyyyyyyyyyyyyyyyyyyyyyyyyyy
      webhook_secret: <output of: openssl rand -hex 32>
```

**How to create a Bitbucket App Password:**
1. Bitbucket → Personal Settings → App passwords → Create app password
2. Label: `code-review-bot`
3. Permissions: **Repositories: Read** | **Pull requests: Read, Write**

Generate a webhook secret for each workspace:
```bash
openssl rand -hex 32
```

No bot restart is needed when editing this file — credentials are read on every request.

---

## 6. Build and Start

```bash
cd ~/code-review
docker compose up -d --build
```

Verify all containers are running:
```bash
docker compose ps
```

Expected:
```
NAME                    STATUS          PORTS
code-review-app-1       Up (healthy)    0.0.0.0:8000->8000/tcp
code-review-worker-1    Up
code-review-redis-1     Up (healthy)    0.0.0.0:6379->6379/tcp
```

Check the health endpoint:
```bash
curl http://localhost:8000/health
# Expected: {"status":"ok","redis":"ok"}
```

Check startup logs:
```bash
docker compose logs app
docker compose logs worker
```

---

## 7. Nginx Reverse Proxy

Run as root. Replace `yourdomain.com` with your actual domain.

```bash
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

        client_max_body_size 10M;
        proxy_read_timeout 30s;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/code-review /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

---

## 8. SSL Certificate

```bash
certbot --nginx -d yourdomain.com
# Choose: Redirect HTTP to HTTPS

certbot renew --dry-run   # verify auto-renewal works
```

---

## 9. Firewall

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw deny 8000        # block direct app port access
ufw deny 6379        # block Redis from outside
ufw enable
ufw status
```

---

## 10. Register Projects

For each repository you want reviewed:

1. Add the project to `config/projects.yml` — see `PROJECTS.md`
2. Add the Bitbucket webhook:
   - **Repository Settings → Webhooks → Add webhook**
   - URL: `https://yourdomain.com/webhook/bitbucket`
   - Secret: the `webhook_secret` for that workspace from `config/credentials.yml`
   - Triggers: **Pull request: Created** + **Pull request: Updated**

---

## 11. Verify End-to-End

```bash
# Tail worker logs while opening a real test PR
docker compose logs -f worker
```

Manual webhook test with a signed request:
```bash
SECRET="webhook-secret-for-myworkspace"
PAYLOAD=$(cat tests/fixtures/bitbucket_payload.json)
SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print "sha256="$2}')

curl -s -X POST https://yourdomain.com/webhook/bitbucket \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature: $SIG" \
  -d "$PAYLOAD"
# Expected: {"status":"queued","task_id":"..."}
```

Check a task was queued:
```bash
docker compose exec redis redis-cli LLEN celery
# Expected: 1
```

---

## Maintenance

### Update application

```bash
cd ~/code-review
git pull                          # or rsync new files
docker compose up -d --build      # rebuilds only changed layers
```

### Add / edit workspace credentials

```bash
nano ~/code-review/config/credentials.yml
# No restart needed
```

### Add / edit project settings

```bash
nano ~/code-review/config/projects.yml
# No restart needed
```

### View logs

```bash
docker compose logs -f              # all services, live
docker compose logs -f worker       # worker only
docker compose logs --tail=100 app  # last 100 lines
```

Log format is structured JSON (structlog). To filter by field:
```bash
docker compose logs worker 2>&1 | grep '"status": "failure"'
```

### Restart services

```bash
docker compose restart app worker   # apply .env changes
docker compose restart              # all services
```

### Stop / tear down

```bash
docker compose down                 # stop, keep Redis volume
docker compose down -v              # stop + delete all data
```

### Scale workers

Edit `docker-compose.yml` → worker service → `--concurrency=8`, then:
```bash
docker compose up -d --scale worker=2   # run 2 worker containers
```

### Back up Redis

```bash
mkdir -p ~/code-review/backups
docker run --rm \
  -v code-review_redis_data:/data \
  -v ~/code-review/backups:/backup \
  alpine tar czf /backup/redis-$(date +%Y%m%d).tar.gz /data
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `/health` returns `{"redis":"error"}` | Redis container not running | `docker compose ps redis`; `docker compose restart redis` |
| Webhook returns `400` with "No credentials" | Workspace not in `credentials.yml` | Add the workspace block to `config/credentials.yml` |
| Webhook returns `401` | Wrong `webhook_secret` for this workspace | Compare `credentials.yml` value with the Bitbucket webhook secret field |
| Webhook returns `202` but nothing happens | Worker not running or crashed | `docker compose ps worker`; `docker compose logs worker` |
| Build status stuck at `INPROGRESS` | Worker crashed mid-task | `docker compose logs --tail=100 worker` — look for Python exceptions |
| "PR too large" comment posted | Diff exceeds `max_diff_lines` | Increase limit in `config/projects.yml` for this repo, or split the PR |
| Worker retrying endlessly | Bad API key or Bitbucket `app_password` | Verify `ANTHROPIC_API_KEY` in `.env`; verify `app_password` in `credentials.yml` |
| Container exits on startup | Missing required env var | `docker compose logs app` — check for pydantic `ValidationError` |
| Semgrep times out | Large or complex files | Reduce `max_diff_lines`; increase timeout in `semgrep_runner.py` |
