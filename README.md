# Code Review Bot

An automated code review bot that integrates with Bitbucket and runs on every pull request. It combines static analysis (Semgrep) and AI review (Claude) to catch security vulnerabilities, bugs, and performance issues before code is merged.

---

## What it does

When a PR is opened or updated in Bitbucket, the bot:

1. Runs **Semgrep** static analysis to detect security vulnerabilities and common bugs
2. If Semgrep finds no issues, runs **Claude AI** review for deeper analysis
3. Posts **inline comments** on the changed lines with findings
4. Posts a **summary comment** at the top of the PR
5. Sets the **build status** to pass or fail — blocking merge if critical issues are found

---

## Key Features

- **Static analysis** — Semgrep with OWASP Top 10, security audit, and language-specific rule packs
- **AI review** — Claude analyzes the diff for security, bugs, and performance issues
- **Inline comments** — findings are posted directly on the relevant lines in the PR
- **Merge blocking** — PRs with critical findings get a `FAILED` build status
- **Multi-language** — Java, JavaScript/TypeScript, C#, PHP (auto-detected from file extensions)
- **Per-project config** — each repo can have its own rules, ignored paths, and severity thresholds
- **Repo-level overrides** — teams can place a `.codereview.yml` in their repo to override central settings
- **Deduplication** — re-running a review cleans up old comments before posting fresh ones
- **Monitoring** — Prometheus metrics + Grafana dashboard with per-language, per-project, and per-author stats

---

## Severity Levels

| Level | Meaning | Blocks merge |
|-------|---------|-------------|
| `CRITICAL` | Security vulnerability (injection, auth bypass, RCE, data leak) | Yes |
| `BUG` | Logic error, null dereference, resource leak, race condition | No |
| `PERFORMANCE` | N+1 queries, blocking I/O, allocations in hot paths | No |
| `SUGGEST` | Naming, readability, best practices | No |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| API server | FastAPI |
| Task queue | Celery + Redis |
| Static analysis | Semgrep OSS |
| AI review | Anthropic Claude API |
| Git platform | Bitbucket Cloud |
| Monitoring | Prometheus + Grafana |
| Deployment | Docker Compose |

---

## Quick Start

```bash
# 1. Copy and fill in your credentials
cp .env.example .env

# 2. Register your first project
nano config/projects.yml

# 3. Start everything
docker compose up --build -d
```

---

## Managing Projects

The bot uses a two-layer config system. For each PR, settings are resolved in this order:

```
System defaults
      ↓
config/projects.yml    ← central registry, managed on the VPS
      ↓
.codereview.yml        ← optional file in the repo root (overrides central)
```

You can onboard a project by editing `config/projects.yml` alone — no changes to the repo are required.

### Adding a Project

#### Step 1 — Register in config/projects.yml

SSH into the VPS and add an entry. The key is the Bitbucket repo full name: `workspace/repository-slug`.

```bash
nano ~/code-review/config/projects.yml
```

Minimal entry — language auto-detected, everything else at defaults:

```yaml
projects:
  myworkspace/my-service:
    language: java
```

Full entry:

```yaml
projects:
  myworkspace/my-service:
    language: java
    ai_review: true
    static_analysis: true
    block_merge_on:
      - CRITICAL
    max_diff_lines: 500
    ignore_paths:
      - "migrations/*"
      - "**/*.generated.java"
    semgrep_rules:
      - owasp
      - security-audit
      - p/java
    ai_focus:
      - security
      - bugs
```

No restart needed — the file is read on every review task.

#### Step 2 — Add credentials in config/credentials.yml

```bash
nano ~/code-review/config/credentials.yml
```

Generate a webhook secret for the repo:
```bash
openssl rand -hex 32
```

**Use whichever token type your Bitbucket plan allows:**

**Repository Access Token** (per-repo — most common):
- Repository → **Settings → Security → Access tokens → Create access token**
- Scopes: **Repositories: Read** | **Pull requests: Read, Write**

```yaml
bitbucket:
  workspaces:
    myworkspace:
      repositories:
        my-service:
          api_token: ATBBxxxxxxxxxxxx      # Repository Access Token
          webhook_secret: aabbccdd...
```

**Workspace Access Token** (one token covers all repos in the workspace):
- Workspace → **Settings → Security → Access tokens → Create access token**

```yaml
bitbucket:
  workspaces:
    myworkspace:
      api_token: ATBBxxxxxxxxxxxx          # Workspace Access Token
      repositories:
        my-service:
          webhook_secret: aabbccdd...
        other-service:
          webhook_secret: 11223344...
```

**App Password** (legacy, still works):
- Profile avatar → **Personal settings → App Passwords**

```yaml
bitbucket:
  workspaces:
    myworkspace:
      username: alice
      app_password: ATBBxxxxxxxxxxxx
      repositories:
        my-service:
          webhook_secret: aabbccdd...
```

No restart needed — credentials are read on every request.

#### Step 3 — Add the Webhook in Bitbucket

1. Open the repository → **Repository Settings → Webhooks → Add webhook**
2. Fill in:
   - **Title:** `Code Review Bot`
   - **URL:** `https://yourdomain.com/webhook/bitbucket`
   - **Secret:** the `webhook_secret` value for this workspace from `config/credentials.yml`
   - **Status:** Active
3. Triggers: **Pull request: Created** and **Pull request: Updated**
4. Save, then click **Test connection** — should return HTTP 200

#### Step 4 — Open a Test PR

Create a branch, open a PR. Within a few seconds:
- Build status → `INPROGRESS`
- Semgrep + AI run in parallel
- Inline comments appear on changed lines
- Summary comment posted at the PR top
- Build status → `SUCCESSFUL` or `FAILED` (if CRITICAL findings exist)

If nothing appears after 30 seconds: `docker compose logs --tail=50 worker`

### Removing a Project

1. Delete the entry from `config/projects.yml`
2. Delete the webhook in Bitbucket: **Repository Settings → Webhooks → Delete**

### Config Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `language` | string | `auto` | `java`, `dotnet`, `php`, `js`, `python`, or `auto` (detects from file extensions) |
| `ai_review` | bool | `true` | Run the Claude AI analyzer |
| `static_analysis` | bool | `true` | Run Semgrep |
| `block_merge_on` | list | `[CRITICAL]` | Severity levels that set build status to FAILED |
| `max_diff_lines` | int | `500` | Skip review if diff exceeds this line count |
| `ignore_paths` | list | `[]` | Glob patterns — matching files produce no findings |
| `semgrep_rules` | list | `[owasp, security-audit]` | Rule packs: `owasp`, `security-audit`, `p/java`, `p/csharp`, `p/php`, `p/javascript`, `p/python`, `p/django`, `p/flask` |
| `ai_focus` | list | `[security, bugs]` | Hints passed to the AI prompt |
| `target_branches` | list | `[master, main]` | Only review PRs targeting these branches |
| `slack_channel` | string | `""` | Slack channel for notifications (Phase 3, not yet active) |

### Quick Reference Configs by Language

**Java / Spring Boot**
```yaml
myworkspace/spring-api:
  language: java
  ignore_paths: ["migrations/*", "**/*.generated.java", "src/test/*"]
  semgrep_rules: [owasp, security-audit, p/java]
```

**React / TypeScript**
```yaml
myworkspace/react-app:
  language: js
  ignore_paths: ["dist/*", "build/*", "**/*.min.js", "node_modules/*"]
  semgrep_rules: [owasp, p/javascript]
```

**C# / ASP.NET**
```yaml
myworkspace/dotnet-service:
  language: dotnet
  block_merge_on: [CRITICAL, BUG]
  ignore_paths: ["**/Migrations/*", "**/*.Designer.cs", "**/*.g.cs"]
  semgrep_rules: [owasp, p/csharp]
```

**PHP / Laravel**
```yaml
myworkspace/laravel-app:
  language: php
  ignore_paths: ["vendor/*", "storage/*", "bootstrap/cache/*"]
  semgrep_rules: [owasp, p/php]
```

**Python / Django**
```yaml
myworkspace/django-api:
  language: python
  ignore_paths: ["migrations/*", "**/*_test.py", "tests/*"]
  semgrep_rules: [owasp, security-audit, p/python, p/django]
```

**Python / Flask**
```yaml
myworkspace/flask-service:
  language: python
  ignore_paths: ["tests/*", "**/*_test.py"]
  semgrep_rules: [owasp, security-audit, p/python, p/flask]
```

**Legacy / large repo (lenient mode)**
```yaml
myworkspace/legacy-monolith:
  language: java
  static_analysis: false
  block_merge_on: []
  max_diff_lines: 1000
  ignore_paths: ["legacy/*", "generated/*"]
  ai_focus: [security]
```

### Per-Repo Override (.codereview.yml)

Teams can place a `.codereview.yml` in their repo root to override any central setting. It is read from the PR's **base branch** on every review, so changes take effect on the next PR without any bot restart.

### Re-running the Review

Push a new commit to the PR branch. The bot will delete its previous comments, re-run the full analysis, and post fresh results.

---

## Deployment Guide

Target: Ubuntu 22.04/24.04 LTS, Docker Compose stack, HTTPS via Nginx + Let's Encrypt.

**Recommended VPS specs:** 4 vCPU, 8 GB RAM, 80 GB SSD
**Minimum:** 2 vCPU, 4 GB RAM, 40 GB SSD

### 1. Server Preparation

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

### 2. Create App User

Run the bot as a non-root user with Docker access.

```bash
useradd -m -s /bin/bash deploy
usermod -aG docker deploy
```

All subsequent steps are run as the `deploy` user unless noted otherwise.

```bash
su - deploy
```

### 3. Upload Application Files

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

### 4. Configure Environment

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

### 5. Configure Credentials

Bitbucket credentials are stored per workspace (organization) in `config/credentials.yml`.
This file is **never committed to git**.

```bash
cp config/credentials.yml.example config/credentials.yml
nano config/credentials.yml
chmod 600 config/credentials.yml
```

Generate a webhook secret for each repository:
```bash
openssl rand -hex 32
```

**Auth options** — use whichever your Bitbucket plan supports (first match wins per repo):

**Option A — Repository Access Token** (per-repo, most common)
- Repository → **Settings → Security → Access tokens → Create access token**
- Scopes: **Repositories: Read** | **Pull requests: Read, Write**

**Option B — Workspace Access Token** (one token for all repos in the workspace)
- Workspace → **Settings → Security → Access tokens → Create access token**
- Scopes: **Repositories: Read** | **Pull requests: Read, Write**

**Option C — App Password** (legacy, still works)
- Profile avatar → **Personal settings → App passwords → Create app password**
- Permissions: **Repositories: Read** | **Pull requests: Read, Write**

```yaml
bitbucket:
  workspaces:

    # Option A: per-repo token
    first-workspace:
      repositories:
        backend-api:
          api_token: ATBBxxxxxxxxxxxx         # Repository Access Token
          webhook_secret: aabbccdd...
        frontend-app:
          api_token: ATBByyyyyyyyyyyy
          webhook_secret: 11223344...

    # Option B: workspace token shared by all repos
    second-workspace:
      api_token: ATBBzzzzzzzzzzzzzz           # Workspace Access Token
      repositories:
        mobile-app:
          webhook_secret: deadbeef...
        data-service:
          webhook_secret: cafebabe...

    # Option C: App Password (legacy)
    third-workspace:
      username: alice
      app_password: ATBBaaaaaaaaaaaa
      repositories:
        legacy-app:
          webhook_secret: 00001111...
```

No bot restart is needed when editing this file — credentials are read on every request.

### 6. Build and Start

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

### 7. Nginx Reverse Proxy

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

### 8. SSL Certificate

```bash
certbot --nginx -d yourdomain.com
# Choose: Redirect HTTP to HTTPS

certbot renew --dry-run   # verify auto-renewal works
```

### 9. Firewall

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw deny 8000        # block direct app port access
ufw deny 6379        # block Redis from outside
ufw enable
ufw status
```

### 10. Register Projects

For each repository you want reviewed:

1. Add the project to `config/projects.yml` — see [Managing Projects](#managing-projects) section above
2. Add the Bitbucket webhook:
   - **Repository Settings → Webhooks → Add webhook**
   - URL: `https://yourdomain.com/webhook/bitbucket`
   - Secret: the `webhook_secret` for that workspace from `config/credentials.yml`
   - Triggers: **Pull request: Created** + **Pull request: Updated**

### 11. Verify End-to-End

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

### Maintenance

#### Update application

```bash
cd ~/code-review
git pull                          # or rsync new files
docker compose up -d --build      # rebuilds only changed layers
```

#### Add / edit workspace credentials

```bash
nano ~/code-review/config/credentials.yml
# No restart needed
```

#### Add / edit project settings

```bash
nano ~/code-review/config/projects.yml
# No restart needed
```

#### View logs

```bash
docker compose logs -f              # all services, live
docker compose logs -f worker       # worker only
docker compose logs --tail=100 app  # last 100 lines
```

Log format is structured JSON (structlog). To filter by field:
```bash
docker compose logs worker 2>&1 | grep '"status": "failure"'
```

#### Restart services

```bash
docker compose restart app worker   # apply .env changes
docker compose restart              # all services
```

#### Stop / tear down

```bash
docker compose down                 # stop, keep Redis volume
docker compose down -v              # stop + delete all data
```

#### Scale workers

Edit `docker-compose.yml` → worker service → `--concurrency=8`, then:
```bash
docker compose up -d --scale worker=2   # run 2 worker containers
```

---

## Redis Backups & Persistence

All review metrics and statistics are stored in Redis. The system is configured to survive reboots and container recreation.

### Quick Setup

Run the setup script to configure everything:

```bash
./scripts/setup-redis-persistence.sh
```

### Configuration

Redis is configured with:
- **AOF (Append Only File)**: Enabled, syncs every second
- **RDB Snapshots**: Automatic every 15min (1 change), 5min (10 changes), 1min (10000 changes)
- **Password protection**: Enabled (set `REDIS_PASSWORD` in `.env`)
- **Network access**: Localhost only (`127.0.0.1:6379`)
- **Volume persistence**: Docker volume `redis_data` mounted to `/data`
- **Auto-restore**: Automatically restores from latest backup on container recreation

### Auto-Restore on Reboot

Redis now automatically restores data on container recreation:
1. On container start, checks for existing data in `/data`
2. If no data found, looks for latest backup in `/backups/redis/`
3. Automatically restores the most recent `redis_backup_*.rdb` file
4. Starts Redis with restored data

### Manual Backup

```bash
# Create backup manually
./scripts/redis-backup.sh

# Or trigger SAVE from within Redis
docker compose exec redis redis-cli -a $REDIS_PASSWORD SAVE
```

### Automated Backups

#### Option 1: Cron (macOS/Linux)

Add to crontab for hourly backups:

```bash
# Edit crontab
crontab -e

# Add this line (hourly backups)
0 * * * * cd /path/to/code-review && ./scripts/redis-backup.sh >> logs/redis-backup.log 2>&1
```

#### Option 2: Systemd Timer (Linux)

```bash
# Install service and timer
sudo cp scripts/redis-auto-backup.* /etc/systemd/system/
sudo systemctl enable redis-auto-backup.timer
sudo systemctl start redis-auto-backup.timer

# Check status
systemctl status redis-auto-backup.timer
```

### Restore from Backup

```bash
# List available backups
ls -la backups/redis/

# Manual restore (if auto-restore doesn't work)
./scripts/redis-restore.sh backups/redis/redis_backup_20240328_120000.rdb
```

### Redis Data Survival

Your metrics will survive:
- ✅ Container restarts
- ✅ Server reboots (with Docker volume + auto-restore)
- ✅ App redeploys (automatic backup before redeploy recommended)
- ✅ Container recreation (auto-restore from latest backup)

Your metrics will NOT survive:
- ❌ Volume deletion (`docker compose down -v`)
- ❌ Manual Redis FLUSHALL
- ❌ Deleting backups/redis/ directory

### Monitoring Metrics Retention

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

### Reset Metrics (if needed)

```bash
# Clear all metrics
docker compose exec redis redis-cli -a $REDIS_PASSWORD
> DEL metrics:webhooks metrics:reviews metrics:findings:severity
> DEL metrics:by_lang metrics:by_project metrics:by_author
> DEL metrics:findings_by_lang metrics:findings_by_project metrics:findings_by_author
> DEL metrics:duration:sum_ms metrics:duration:count
> EXIT
```

### Back up Redis (Alternative method)

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
| No comments, no status update | Check Bitbucket webhook delivery log for HTTP status; verify `BITBUCKET_WEBHOOK_SECRET` matches |
| Webhook returns 401 (projects) | Secret in Bitbucket doesn't match `BITBUCKET_WEBHOOK_SECRET` in `.env` |
| No inline comments | Check `ignore_paths` isn't filtering all files; check worker logs for path errors |
| AI returns no findings | Set explicit `language:` in the project config; verify `ANTHROPIC_API_KEY` is valid |
| Semgrep returns no findings | `docker compose logs worker` — check for `semgrep: not found`; rebuild image |

---

## Architecture

```
Git Platform (Bitbucket / GitHub / GitLab)
        │  webhook POST (PR opened / updated)
        ▼
   Nginx (TLS termination)
        │
        ▼
   FastAPI  app/main.py
        │  HMAC validate → Celery task
        ▼
   Celery Worker  app/workers/celery_app.py
        │
   ┌────┴───────────────────┐
   ▼                        ▼
SemgrepRunner          AIReviewer
app/analyzers/         app/analyzers/
semgrep_runner.py      ai_reviewer.py
   │                        │
   └──────────┬─────────────┘
              │  merge + dedup + sort by severity
              ▼           app/analyzers/merger.py
   GitPlatform adapter
   (post inline comments + summary + build status)
```

---

## Project Structure

```
app/
├── main.py                   # FastAPI: webhook routes + /health
├── models.py                 # Finding, PRContext, ReviewConfig, Severity
├── adapters/
│   ├── base.py               # GitPlatform ABC + hmac_verify() + BOT_MARKER
│   ├── bitbucket.py          # Bitbucket Cloud adapter (httpx)
│   └── factory.py            # get_adapter(platform, settings)
├── analyzers/
│   ├── semgrep_runner.py     # Semgrep subprocess wrapper
│   ├── ai_reviewer.py        # Anthropic SDK, prompt loading, JSON parsing
│   └── merger.py             # dedup (sha256 key) + severity sort
├── workers/
│   └── celery_app.py         # process_review task, ThreadPoolExecutor
└── config/
    ├── settings.py           # pydantic-settings, @lru_cache singleton
    └── project_config.py     # load config/projects.yml + .codereview.yml

config/
└── projects.yml              # central per-project settings registry

prompts/
├── java_prompt.md
├── dotnet_prompt.md
├── php_prompt.md
└── js_prompt.md

tests/
├── test_adapters.py
├── test_analyzers.py
├── test_merger.py
├── test_worker.py
└── fixtures/
```

---

## Dev Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the API server locally
uvicorn app.main:app --reload

# Run a Celery worker locally
celery -A app.workers.celery_app worker --loglevel=info

# Unit tests (no semgrep required)
pytest tests/ -m "not integration"

# All tests including integration (requires semgrep installed)
pytest tests/

# Full stack via Docker
docker compose up --build
```

---

## How to Add a New Git Platform Adapter

1. Create `app/adapters/<platform>.py`, implement all abstract methods from `GitPlatform` in `base.py`
2. Register it in `get_adapter()` in `app/adapters/factory.py`
3. Add `POST /webhook/<platform>` route in `app/main.py`
4. Add tests in `tests/test_adapters.py`
5. Document the webhook setup in this README

## How to Add a New Language

1. Create `prompts/<language>_prompt.md` — include language-specific CRITICAL/BUG examples
2. Add extension mappings to `_EXT_TO_LANG` in `app/config/project_config.py`
3. Add a Semgrep rule mapping to `SEMGREP_RULE_MAP` in `app/analyzers/semgrep_runner.py`
4. Add a quick-reference config block in the Project Configuration section above

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `Finding.dedup_key` via sha256 | Single dedup mechanism; Semgrep findings win ties (inserted first) |
| Prompt files as `.md`, not hardcoded | Prompt tuning without code deploys or restarts |
| `ThreadPoolExecutor` in Celery | Semgrep is subprocess, Anthropic SDK is sync — asyncio adds complexity with no benefit |
| `task_acks_late=True` | Message stays in queue until task completes — prevents data loss on worker crash |
| `worker_prefetch_multiplier=1` | Semgrep is CPU-heavy; one task per worker prevents resource contention |
| `HTTP 202` from webhook handler | Never wait for analysis — guarantees <1 sec webhook response |
| `BOT_MARKER` in every comment | Platform-agnostic way to find own comments for cleanup on re-review |
| Central `config/projects.yml` | Onboard projects without touching their codebase; `.codereview.yml` in repo can override |
| Graceful analyzer failures | If Semgrep or AI fails, task continues with the other's results |

---

## Roadmap

### Phase 3
- [ ] GitHub adapter (`app/adapters/github.py`)
- [ ] GitLab adapter (`app/adapters/gitlab.py`)
- [ ] MySQL persistence (findings history, per-repo config storage)
- [ ] Slack notifications (`slack_channel` in `ReviewConfig` already wired)
- [ ] Grafana dashboard + metrics endpoint

### Phase 4
- [ ] Web UI for review history
- [ ] Per-author statistics
- [ ] Rate limiting + AI API budget alerts
- [ ] Multi-tenant support
