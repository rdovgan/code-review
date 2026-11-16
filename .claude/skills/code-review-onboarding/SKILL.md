---
name: code-review-onboarding
description: Set up a new Code Review Bot instance on a server from scratch, or audit an existing one — prerequisites, .env, platform credentials, the project snapshot folder for agentic review, Docker stack, HTTPS, webhooks, end-to-end check and backups. Hands repository mapping and config/projects.yml to the code-review-repo-config skill. Use when installing the bot for a new team or project, moving it to another server, or asked to "onboard", "deploy", "set up" or "check the setup of" the code review bot.
---

# Onboard a Code Review Bot instance

Walks a person through a working install, checking each step before moving on. Run from the bot's
folder (`/opt/code-review` by convention). Reference: `README.md` ("Setup", "Review modes",
"Operations"). One instance serves one team/organization; its config files are its own.

## Ground rules

- The person types secrets, not you: never ask them to paste API keys or tokens into the chat, and
  never print `.env` or `config/credentials.yml`. Check a key is set with `grep -q '^KEY=.' .env`.
- Check, don't assume: every step ends with a command that proves it worked.
- If a step is already done (existing instance), verify it and move on — this skill also audits.
- Ask before anything outward-facing or hard to undo: opening firewall ports, creating webhooks,
  issuing certificates, deleting volumes.

## 0. Plan

Ask once, up front:
- git platform(s) and workspace(s); which repos to review (rough list is fine);
- AI provider: `claude` (Anthropic key) or `glm` (Z.AI key);
- domain for webhooks; whether there is a Mattermost channel and a Jira site to connect;
- whether agentic review is wanted (needs a folder with shallow clones of the repos — step 4).

Show the steps below as a checklist and keep it updated.

## 1. Server

```bash
docker --version && docker compose version && git --version && nginx -v
df -h / && free -g && nproc        # 2+ vCPU, 4+ GB RAM; agentic review: disk for snapshots
```

Missing pieces: Docker (`curl -fsSL https://get.docker.com | sh`), compose plugin, nginx, certbot.

## 2. Code

```bash
git clone <bot-repo-url> /opt/code-review && cd /opt/code-review
git checkout $(git describe --tags --abbrev=0)   # latest release, unless they want master
```

## 3. Configuration files

```bash
cp .env.example .env
cp config/credentials.yml.example config/credentials.yml
cp config/projects.yml.example config/projects.yml
chmod 600 .env config/credentials.yml
```

**.env** — the person fills it in their editor. Then verify, without printing values:

```bash
for k in AI_PROVIDER AI_DAILY_TOKEN_BUDGET REDIS_PASSWORD; do grep -q "^$k=." .env && echo "ok $k" || echo "MISSING $k"; done
grep -q '^AI_PROVIDER=glm' .env && k=ZAI_API_KEY || k=ANTHROPIC_API_KEY; grep -q "^$k=." .env && echo "ok $k" || echo "MISSING $k"
```

Optional, check only if wanted: `MATTERMOST_WEBHOOK_URL`, `JIRA_BASE_URL` + `JIRA_EMAIL` +
`JIRA_API_TOKEN` (a read-only account or a token with the `read:jira-work` scope),
`PROJECTS_HOST_DIR` if the snapshot folder is not `/opt/projects`. Budget: a first guess is fine
here; step 5 refines it.

**config/credentials.yml** — per workspace or per repo, a token and a `webhook_secret`. Explain the
structure using the example file. Token scopes (Bitbucket): **Repositories: Read**,
**Pull requests: Read, Write** — Read on repositories is also what the bot uses to clone target
branches for agentic review. Secrets: `openssl rand -hex 32`, one per repo or workspace.

## 4. Snapshot folder (only for agentic/shadow review)

The agent reads shallow clones in `/opt/projects/<repo-slug>` (read-only for the bot).
- **Folder already exists** (kept current by something else): check it's readable by the
  container user and on the main branch — `ls -ld /opt/projects/*/ | head; for d in /opt/projects/*/; do echo "$(basename $d) $(git -C $d rev-parse --abbrev-ref HEAD)"; done`.
  Don't change it.
- **No folder**: create it with `scripts/project_snapshots.sh clone <git-url> [branch]` per repo,
  and install the nightly refresh cron from that script's header. The folder name must equal the
  repo slug in the platform.

## 5. Repositories → config/projects.yml

Use the **code-review-repo-config** skill: it scans the repos, maps dependencies, proposes the
per-repo settings and budget, and writes `config/projects.yml`. Without agentic review a short
manual config is enough (`config/projects.yml.example`).

## 6. Start

```bash
docker compose up -d --build
docker compose ps                          # app, worker, redis, prometheus, grafana: Up
curl -s localhost:8000/health              # {"status":"ok","redis":"ok"}
docker compose logs --tail=30 worker       # "celery@... ready."
docker compose exec worker ls /projects | head   # agentic only: the snapshot folder is visible
```

Worker restarting → read its log before anything else (dependency or `.env` errors show there).

## 7. HTTPS and firewall

Nginx site proxying to `127.0.0.1:8000` (README "Expose over HTTPS"), then
`nginx -t && systemctl reload nginx` and `certbot --nginx -d <domain>`. Ask before changing the
firewall: allow SSH and Nginx, deny 8000. Redis and Prometheus are bound to localhost already.
Grafana (`:3000`) starts with admin/admin — have the password changed or the port closed.
Check: `curl -s https://<domain>/health`.

## 8. Webhooks

For each repo in `config/projects.yml`, give the person the exact settings (they create them):
- Bitbucket: URL `https://<domain>/webhook/bitbucket`, secret = its `webhook_secret`, triggers
  Pull request Created, Updated, Comment created.
- GitHub: `/webhook/github`, JSON, secret, events Pull requests + Issue comments.
- GitLab: `/webhook/gitlab`, secret token, Merge request events.

A list of repo → webhook URL is easier to work through than prose. The platform's "test" delivery
should get HTTP 200/202; `401` = secret mismatch, `400` = workspace/repo missing in credentials.

## 9. End-to-end check

Open a small test PR (or comment `review` on one) in one repo of each review mode, and watch
`docker compose logs -f worker`:
- `Done — total: N ... status: success|failure` and a summary comment on the PR;
- agentic repos: `Agentic review complete` (or `falling back (<reason>)` — fix the reason);
- Mattermost message for new PRs of `notify_authors`, if configured;
- `curl -s localhost:8000/metrics | grep code_review_reviews_total`.

## 10. Backups and handover

- `./scripts/setup-redis-persistence.sh`, then schedule `./scripts/redis-backup.sh` (cron or the
  systemd timer in `scripts/`).
- Back up `.env`, `config/credentials.yml`, `config/projects.yml` somewhere safe — they are not in
  git.
- Hand over: domain, which repos are on which review mode, the budget and where to watch it
  (Grafana, `code_review_ai_tokens_today`), how to update (`git pull && docker compose up -d --build
  app worker`), and the open items left from any step.
