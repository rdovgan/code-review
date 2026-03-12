# Adding a Project to the Code Review Bot

This guide walks through connecting an existing Bitbucket repository to the bot. Assumes the bot is already deployed and `/health` returns `{"status":"ok","redis":"ok"}`.

---

## Prerequisites

- Bot is running at `https://yourdomain.com` (see `DEPLOY.md`)
- You have admin access to the Bitbucket repository
- You have the `BITBUCKET_WEBHOOK_SECRET` value from the bot's `.env`

---

## Step 1 — Create a Bitbucket App Password

The bot needs credentials to read diffs and post comments on your repository.

1. Go to **Bitbucket → Personal Settings → App passwords**
2. Click **Create app password**
3. Label: `code-review-bot`
4. Grant these permissions:
   - **Repositories:** Read
   - **Pull requests:** Read, Write
5. Copy the generated password

Update the bot's `.env` on the VPS:
```bash
# On VPS, as deploy user
cd ~/code-review
nano .env
```
Set:
```env
BITBUCKET_USERNAME=your-bitbucket-account-username
BITBUCKET_APP_PASSWORD=the-password-you-just-copied
```

Restart the app and worker to apply:
```bash
docker compose restart app worker
```

---

## Step 2 — Add the Webhook to Your Repository

1. In Bitbucket, open your repository
2. Go to **Repository Settings → Webhooks → Add webhook**
3. Fill in:
   - **Title:** `Code Review Bot`
   - **URL:** `https://yourdomain.com/webhook/bitbucket`
   - **Secret:** the value of `BITBUCKET_WEBHOOK_SECRET` from the bot's `.env`
   - **Status:** Active
4. Under **Triggers**, select **Choose from a full list of triggers**, then check:
   - Pull Request: **Created**
   - Pull Request: **Updated**
5. Click **Save**

Test the connection using Bitbucket's **Test connection** button — it should return HTTP 200.

---

## Step 3 — Add `.codereview.yml` to Your Repository (Optional)

Without this file the bot runs with defaults: AI review on, Semgrep on, blocks merge on CRITICAL findings, 500-line diff limit.

Create `.codereview.yml` in the **root of your repository** and commit it to your default branch:

```yaml
# Language: auto-detected from file extensions if omitted.
# Supported: java, dotnet, php, js
language: auto

# Enable/disable analyzers
ai_review: true
static_analysis: true

# Which severities block the PR (prevents merge until resolved)
block_merge_on:
  - CRITICAL

# Skip review if diff exceeds this many lines
max_diff_lines: 500

# Paths to never report findings for (glob patterns)
ignore_paths:
  - "migrations/*"
  - "**/*.generated.*"
  - "vendor/*"
  - "node_modules/*"

# Semgrep rule packs to run
# Available: owasp, security-audit, p/java, p/csharp, p/php, p/javascript
semgrep_rules:
  - owasp
  - security-audit

# What the AI should focus on (informational — shapes the prompt)
ai_focus:
  - security
  - bugs
```

The bot fetches this file from the **base branch** of the PR on every review run, so changes take effect immediately without redeploying the bot.

---

## Step 4 — Open a Test Pull Request

1. Create a branch with at least one changed file
2. Open a pull request against your default branch
3. The bot should respond within a few seconds

**What happens:**
- Bitbucket sends the webhook → bot returns HTTP 202 immediately
- A Celery task starts in the background
- The bot posts a build status `INPROGRESS` on the PR
- Semgrep and the AI analyzer run in parallel
- Inline comments appear on changed lines
- A summary comment is posted at the top of the PR
- Build status updates to `SUCCESSFUL` or `FAILED` (if CRITICAL findings exist)

**If nothing happens after 30 seconds**, check the worker logs:
```bash
# On VPS
docker compose logs --tail=50 worker
```

---

## Severity Reference

| Label | Meaning | Blocks merge? |
|-------|---------|---------------|
| `CRITICAL` | Security vulnerability (injection, auth bypass, RCE, data leak) | Yes (default) |
| `BUG` | Logic error, NPE risk, resource leak, race condition | No (default) |
| `PERFORMANCE` | N+1 queries, blocking I/O, allocations in hot path | No |
| `SUGGEST` | Naming, readability, best practices | No |

To make `BUG` also block merges, add it to `block_merge_on` in `.codereview.yml`:
```yaml
block_merge_on:
  - CRITICAL
  - BUG
```

---

## Re-running the Review

Push a new commit to the PR branch — this triggers the `pullrequest:updated` event. The bot will:
1. Delete all its previous comments
2. Re-run the full analysis
3. Post fresh results

---

## Multiple Repositories

No extra bot configuration is needed. Each repository:
- Uses its own `.codereview.yml` (or defaults if absent)
- Has its own webhook pointing to the same bot URL
- The same `BITBUCKET_APP_PASSWORD` works as long as the user has access to all repos

---

## Disabling the Bot for a Specific PR

Add a line to the PR description:
```
<!-- skip-code-review -->
```

> **Note:** This is not yet implemented. To add support, see `app/workers/celery_app.py` — check `pr_context.title` or description for the skip marker before running analysis.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No comments, no status update | Webhook not firing or 401 response | Check webhook secret matches `.env`; check Bitbucket webhook delivery log |
| `401` in Bitbucket webhook delivery log | Wrong `BITBUCKET_WEBHOOK_SECRET` | Re-check `.env` value vs. Bitbucket webhook secret field |
| Status stays `INPROGRESS` forever | Worker crashed mid-task | `docker compose logs worker` — look for Python exceptions |
| "PR too large" comment | Diff exceeds `max_diff_lines` | Increase limit in `.codereview.yml` or split the PR |
| Comments posted but no inline positions | File path mismatch | Check that `ignore_paths` isn't accidentally filtering all files |
| AI returns no findings | Language not detected / prompt missing | Check `pr_context.language` in worker logs; add `language: java` to `.codereview.yml` |
| Semgrep returns no findings | Semgrep not installed in container | Check `docker compose logs worker` for `semgrep: not found`; rebuild image |
