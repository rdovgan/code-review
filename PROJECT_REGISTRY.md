# Managing Projects in the Bot Registry

The bot maintains a central config file at `config/projects.yml` that stores
settings for every repository it reviews. No database or restart needed —
the file is read on every review task.

---

## How Config Layers Work

For each PR, settings are resolved in this order (later layers win):

```
ReviewConfig defaults
       ↓
config/projects.yml   ← bot-managed, covers all projects
       ↓
.codereview.yml       ← optional, in the repo root (overrides central)
```

This means:
- You can onboard a project by adding it to `projects.yml` alone — no changes to the repo required
- A team can still fine-tune their own settings via `.codereview.yml` in their repo
- Fields not set anywhere fall back to the system defaults

---

## All Available Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `language` | string | `auto` | `java`, `dotnet`, `php`, `js`, or `auto` (detect from file extensions) |
| `ai_review` | bool | `true` | Run the AI (Claude) analyzer |
| `static_analysis` | bool | `true` | Run Semgrep |
| `block_merge_on` | list | `[CRITICAL]` | Severity levels that set PR status to FAILED |
| `max_diff_lines` | int | `500` | Skip review if diff exceeds this line count |
| `ignore_paths` | list | `[]` | Glob patterns — matching files are never reported |
| `semgrep_rules` | list | `[owasp, security-audit]` | Rule packs to run |
| `ai_focus` | list | `[security, bugs]` | Hints passed to the AI prompt |
| `slack_channel` | string | `""` | Slack channel for notifications (Phase 3) |

**Semgrep rule options:** `owasp`, `security-audit`, `p/java`, `p/csharp`, `p/php`, `p/javascript`

---

## Adding a New Project

Open `config/projects.yml` on the VPS and add an entry under `projects:`.
The key must be the Bitbucket repo's full name — `workspace/repository-slug`.

```bash
# On VPS
nano ~/code-review/config/projects.yml
```

Minimal entry (relies on auto-detection and defaults):

```yaml
projects:
  myworkspace/new-service:
    language: java
```

Full entry:

```yaml
projects:
  myworkspace/new-service:
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

Then add the Bitbucket webhook for that repo (see `ONBOARDING.md` Step 2).
No bot restart needed.

---

## Removing a Project

1. Delete the entry from `config/projects.yml`
2. Remove the webhook in Bitbucket: **Repository Settings → Webhooks → Delete**

---

## Viewing All Registered Projects

```bash
# List all repo names
grep -E "^\s{2}\S" ~/code-review/config/projects.yml

# Pretty-print the full file
cat ~/code-review/config/projects.yml
```

---

## Editing a Project's Settings

Edit the entry directly in `config/projects.yml`. Changes take effect on the
next PR event — no restart required.

```bash
nano ~/code-review/config/projects.yml
```

---

## Quick Reference: Configs for Common Project Types

### Java / Spring Boot

```yaml
myworkspace/spring-api:
  language: java
  block_merge_on: [CRITICAL]
  ignore_paths:
    - "migrations/*"
    - "**/*.generated.java"
    - "src/test/*"
  semgrep_rules: [owasp, security-audit, p/java]
```

### React / TypeScript

```yaml
myworkspace/react-app:
  language: js
  block_merge_on: [CRITICAL]
  ignore_paths:
    - "dist/*"
    - "build/*"
    - "**/*.min.js"
    - "node_modules/*"
  semgrep_rules: [owasp, p/javascript]
```

### C# / ASP.NET

```yaml
myworkspace/dotnet-service:
  language: dotnet
  block_merge_on: [CRITICAL, BUG]
  ignore_paths:
    - "**/Migrations/*"
    - "**/*.Designer.cs"
    - "**/*.g.cs"
  semgrep_rules: [owasp, p/csharp]
```

### PHP / Laravel

```yaml
myworkspace/laravel-app:
  language: php
  block_merge_on: [CRITICAL]
  ignore_paths:
    - "vendor/*"
    - "storage/*"
    - "bootstrap/cache/*"
  semgrep_rules: [owasp, p/php]
```

### Large repo / legacy code (lenient mode)

```yaml
myworkspace/legacy-monolith:
  language: java
  static_analysis: false    # too many false positives
  block_merge_on: []        # never block, just inform
  max_diff_lines: 1000
  ignore_paths:
    - "legacy/*"
    - "generated/*"
  ai_focus: [security]      # narrow focus to reduce noise
```

---

## Current Projects

Edit this table when you add or remove a project.

| Repository | Language | Blocks on | Added |
|------------|----------|-----------|-------|
| *(add your first project)* | | | |
