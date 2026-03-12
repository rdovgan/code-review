# AI-Powered Code Review System — Technical Reference
*Developer reference for configurations, project structure, and integration details*

---

## 1. Processing Flow

```
Git Platform (GitHub / GitLab / Bitbucket)
        │  webhook POST (PR opened / updated)
        ▼
   nginx (TLS termination)
        │
        ▼
   Orchestrator (FastAPI)
        │  HMAC validate → Redis queue
        ▼
   Celery Worker
        │
   ┌────┴──────────────────┐
   ▼                       ▼
Semgrep CLI           AI Model API
(static analysis)     (AI review)
   │                       │
   └────────┬──────────────┘
            │  merge + dedup + sort by severity
            ▼
   PR inline comments + Slack summary
            │
            ▼
   MySQL (metrics + audit log)
```

**Flow description:**
1. Developer opens or updates a PR on GitHub / GitLab / Bitbucket
2. Platform sends webhook POST to VPS (nginx → orchestrator)
3. Orchestrator validates HMAC signature, puts task into Redis queue
4. Celery worker fetches git diff via the platform API
5. In parallel: Semgrep static analysis + AI Model review on the diff
6. Results are merged, deduplicated, sorted by severity
7. Orchestrator posts inline comments to the PR and summary to Slack
8. Metrics are stored in MySQL for the Grafana dashboard

---

## 2. Project Structure

```
code-review-system/
├── CLAUDE.md                    # project context and conventions
├── .codereview.yml              # config for this repo itself
├── docker-compose.yml
├── app/
│   ├── main.py                  # FastAPI entrypoint
│   ├── adapters/
│   │   ├── base.py              # GitPlatform ABC
│   │   ├── bitbucket.py
│   │   ├── github.py
│   │   └── gitlab.py
│   ├── analyzers/
│   │   ├── semgrep_runner.py
│   │   └── ai_reviewer.py
│   ├── workers/
│   │   └── celery_app.py
│   └── config/
│       └── settings.py
├── prompts/
│   ├── java_prompt.md
│   ├── dotnet_prompt.md
│   ├── php_prompt.md
│   └── js_prompt.md
├── tests/
│   └── fixtures/
│       ├── sample_java.diff
│       ├── sample_js.diff
│       └── expected_findings.json
└── docs/
    └── webhooks/
        ├── bitbucket_payload.json
        ├── github_payload.json
        └── gitlab_payload.json
```

---

## 3. Per-Project Configuration (`.codereview.yml`)

Each repository may include a `.codereview.yml` file in its root to customize system behavior:

```yaml
# .codereview.yml
language: java                  # auto-detect | java | dotnet | php | js
ai_review: true                 # enable/disable AI review
static_analysis: true           # enable/disable Semgrep
block_merge_on:                 # severity levels that block merge
  - CRITICAL
max_diff_lines: 500             # diff line limit for analysis
slack_channel: "#code-review"   # Slack channel for notifications
ignore_paths:                   # glob paths excluded from review
  - "**/*.generated.java"
  - "**/migrations/**"
semgrep_rules:                  # Semgrep rule sets
  - owasp
  - security-audit
  - p/java
ai_focus:                       # AI review focus areas
  - security
  - bugs
  - performance
```

### Configuration Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `language` | string | auto-detect | Project language: java, dotnet, php, js |
| `ai_review` | boolean | true | Enable/disable AI review |
| `static_analysis` | boolean | true | Enable/disable Semgrep analysis |
| `block_merge_on` | list | [CRITICAL] | Severity levels that block merge |
| `max_diff_lines` | integer | 500 | Diff line limit for analysis |
| `slack_channel` | string | #code-review | Slack channel for notifications |
| `ignore_paths` | list | [] | Glob paths excluded from review |
| `semgrep_rules` | list | [owasp, security] | Semgrep rule sets |
| `ai_focus` | list | [security, bugs] | AI review focus areas |

---

## 4. AI Review Prompt Templates

### Java (`prompts/java_prompt.md`)

```
You are a senior Java code reviewer. Analyze the following git diff and identify issues.

Classify each issue as one of:
- CRITICAL: security vulnerabilities, data leaks, SQL/command injection, auth bypass
- BUG: logic errors, null pointer risks, resource leaks, incorrect exception handling
- PERFORMANCE: N+1 queries, unnecessary object creation, blocking calls in async context
- SUGGEST: readability, naming conventions, refactoring opportunities, Java best practices

Return ONLY valid JSON, no preamble:
[
  {
    "severity": "CRITICAL|BUG|PERFORMANCE|SUGGEST",
    "file": "path/to/File.java",
    "line": 42,
    "message": "Clear description of the issue",
    "suggestion": "How to fix it"
  }
]

If no issues found, return: []
```

> Apply the same structure for `dotnet_prompt.md`, `php_prompt.md`, and `js_prompt.md` — adjust language-specific best practices and common issue patterns per language.

---

## 5. Project Initialization Checklist

- [ ] Initialize structure: FastAPI app skeleton with `adapters/`, `analyzers/`, `workers/`, `config/` directories
- [ ] Write abstract `GitPlatform` class (ABC) with methods: `get_diff`, `post_comment`, `post_inline_comment`
- [ ] Implement `BitbucketAdapter` as the first concrete implementation
- [ ] Write Semgrep runner: accepts list of files, runs `semgrep --json`, returns findings
- [ ] Write AI reviewer: accepts diff + language, sends to AI Model API, parses JSON response
- [ ] Configure Docker Compose or systemd services: orchestrator, celery worker, redis, nginx
- [ ] Create `CLAUDE.md` in the project root with architecture description, dev commands, and conventions
- [ ] Add fixture files: `tests/fixtures/` with sample diffs and expected review output
- [ ] Add webhook payload examples: `docs/webhooks/` for each git platform

---

## 6. Key Technical Decisions

| Decision | Choice | Reason |
|---|---|---|
| Async processing | Celery + Redis | Webhook must respond in < 3 sec; analysis takes 30–90 sec |
| Diff limit | 8000 tokens / 500 lines | Balance between coverage and API cost |
| Deduplication | File + line + message hash | Prevent duplicate comments from Semgrep and AI overlap |
| Incremental review | Compare head SHA with last reviewed SHA | Avoid re-commenting on unchanged code |
| HMAC validation | SHA-256 per platform | Each platform uses a different header and secret format |

---

*See [CodeReview_Requirements_Plan.md](CodeReview_Requirements_Plan.md) for project requirements, implementation plan, risks, and success metrics.*
