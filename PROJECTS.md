# Managing Projects

The bot uses a two-layer config system. For each PR, settings are resolved in this order:

```
System defaults
      ↓
config/projects.yml    ← central registry, managed on the VPS
      ↓
.codereview.yml        ← optional file in the repo root (overrides central)
```

You can onboard a project by editing `config/projects.yml` alone — no changes to the repo are required.

---

## Adding a Project

### Step 1 — Register in config/projects.yml

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

### Step 2 — Add credentials in config/credentials.yml

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
- Profile avatar → **Personal settings → App passwords**

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

### Step 3 — Add the Webhook in Bitbucket

1. Open the repository → **Repository Settings → Webhooks → Add webhook**
2. Fill in:
   - **Title:** `Code Review Bot`
   - **URL:** `https://yourdomain.com/webhook/bitbucket`
   - **Secret:** the `webhook_secret` value for this workspace from `config/credentials.yml`
   - **Status:** Active
3. Triggers: **Pull request: Created** and **Pull request: Updated**
4. Save, then click **Test connection** — should return HTTP 200

### Step 4 — Open a Test PR

Create a branch, open a PR. Within a few seconds:
- Build status → `INPROGRESS`
- Semgrep + AI run in parallel
- Inline comments appear on changed lines
- Summary comment posted at the PR top
- Build status → `SUCCESSFUL` or `FAILED` (if CRITICAL findings exist)

If nothing appears after 30 seconds: `docker compose logs --tail=50 worker`

---

## Removing a Project

1. Delete the entry from `config/projects.yml`
2. Delete the webhook in Bitbucket: **Repository Settings → Webhooks → Delete**

---

## Config Field Reference

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

### Severity levels

| Label | Meaning | Blocks merge by default? |
|-------|---------|--------------------------|
| `CRITICAL` | Security vulnerability (injection, auth bypass, RCE, data leak) | Yes |
| `BUG` | Logic error, null dereference, resource leak, race condition | No |
| `PERFORMANCE` | N+1 queries, blocking I/O, allocations in hot paths | No |
| `SUGGEST` | Naming, readability, best practices | No |

---

## Quick Reference Configs by Language

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

---

## Per-Repo Override (.codereview.yml)

Teams can place a `.codereview.yml` in their repo root to override any central setting. It is read from the PR's **base branch** on every review, so changes take effect on the next PR without any bot restart.

---

## Re-running the Review

Push a new commit to the PR branch. The bot will delete its previous comments, re-run the full analysis, and post fresh results.

---

## Current Projects

Update this table as you add or remove projects.

| Repository | Language | Blocks on | Added |
|------------|----------|-----------|-------|
| *(add your first project)* | | | |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No comments, no status update | Check Bitbucket webhook delivery log for HTTP status; verify `BITBUCKET_WEBHOOK_SECRET` matches |
| Webhook returns 401 | Secret in Bitbucket doesn't match `BITBUCKET_WEBHOOK_SECRET` in `.env` |
| Build status stuck at INPROGRESS | Worker crashed mid-task — `docker compose logs worker` |
| "PR too large" comment | Increase `max_diff_lines` in the project config or split the PR |
| No inline comments | Check `ignore_paths` isn't filtering all files; check worker logs for path errors |
| AI returns no findings | Set explicit `language:` in the project config; verify `ANTHROPIC_API_KEY` is valid |
| Semgrep returns no findings | `docker compose logs worker` — check for `semgrep: not found`; rebuild image |
