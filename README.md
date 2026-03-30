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
- **AI-SPM** — on-demand security posture scanning of entire repositories via API (secrets, misconfigurations, vulnerable dependencies)
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

## AI Security Posture Management (AI-SPM)

In addition to PR-triggered reviews, the bot supports **proactive repository scanning** via the AI-SPM API. Given a Bitbucket API token, it enumerates all accessible repositories and scans each one for security issues — without waiting for a PR.

### When to use

- Audit an entire workspace for hardcoded secrets before onboarding
- On-demand security assessment of a new or acquired repository
- Periodic security posture checks outside of the PR workflow

### Scan categories

| Category | What it detects |
|---|---|
| `SECRET` | Hardcoded credentials, API keys, tokens, passwords |
| `MISCONFIGURATION` | Auth bypass, insecure defaults, missing validation |
| `DEPENDENCY` | Vulnerable library usage, unsafe APIs (eval, pickle, etc.) |

### Usage

**1. Start a scan**

```bash
curl -X POST http://your-bot/spm/scan \
  -H "Content-Type: application/json" \
  -d '{
    "platform": "bitbucket",
    "access_key": "ATBBxxxxxxx",
    "workspace": "my-workspace",
    "categories": ["SECRET", "MISCONFIGURATION", "DEPENDENCY"]
  }'
# Returns: {"scan_group_id": "abc123...", "status": "queued", "task_id": "..."}
```

Omit `workspace` to scan all workspaces accessible by the token. Omit `categories` to run all checks.

**2. Check progress**

```bash
curl http://your-bot/spm/scan/{scan_group_id}
# Returns progress, per-repo status, and finding summaries
```

**3. Get full findings for a repository**

```bash
curl http://your-bot/spm/scan/{scan_group_id}/report/{scan_id}
# Returns all findings with file, line, message, suggestion, and category
```

Results are stored for 7 days.

### Difference from PR review

| | PR Review | AI-SPM |
|---|---|---|
| Trigger | PR opened / updated | API request |
| Scope | Diff only | Entire repository |
| Output | Inline PR comments | JSON via API |
| Use case | Continuous review | On-demand audit |

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

See [PROJECTS.md](PROJECTS.md) for full setup instructions, including how to add projects and configure Bitbucket webhooks.
