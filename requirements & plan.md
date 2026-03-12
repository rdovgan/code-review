# AI-Powered Code Review System
## Requirements and Implementation Plan
*Version 1.0*

---

## 1. Project Overview

An automated code review system for an IT company with multiple departments (Java, .NET, PHP, JS) and various git platforms (GitHub, GitLab, Bitbucket). The solution combines static code analysis with AI review and is deployed on a self-hosted VPS.

### 1.1 Goals

- Automatic detection of security vulnerabilities, logic errors, and performance issues in PRs before merge
- Single configuration point for all languages and git platforms
- Reduced workload on senior developers for reviewing routine issues
- Fast feedback loop: review results posted as inline PR comments within 2–3 minutes
- Accumulation of code quality metrics per department and project

### 1.2 Scope

| Component | In Scope | Out of Scope |
|---|---|---|
| Git Platforms | GitHub, GitLab, Bitbucket | Azure DevOps, Gerrit |
| Languages | Java, .NET (C#), PHP, JavaScript/TypeScript | Python, Go, Rust |
| AI Provider | AI Model API | Other AI providers (fallback) |
| Static Analysis | Semgrep (all languages), ESLint (JS), PHPStan (PHP) | SonarQube Cloud |
| Notifications | Slack, inline PR comments | Jira tickets, Email |
| Infrastructure | Self-hosted VPS (Ubuntu) | AWS/GCP managed services |

---

## 2. Solution Architecture

### 2.1 System Components

| Component | Technology | Purpose |
|---|---|---|
| Orchestrator | Python FastAPI | Receives webhooks, coordinates analysis, posts results |
| Queue / Worker | Redis + Celery | Async processing (webhook responds in < 3 sec) |
| Static Analyzer | Semgrep CLI | Analyzes git diff against OWASP and custom rules |
| AI Reviewer | AI Model API | Parses diff, classifies issues by severity |
| Platform Adapters | Python (PyGithub, python-gitlab, stashy) | Abstraction layer over git platform APIs |
| Notifications | Slack Webhooks | Summary to channel after review completes |
| Dashboard | Grafana + MySQL | Metrics: issues by language, accuracy, processing time |
| Reverse Proxy | Nginx + Let's Encrypt | TLS termination, webhook endpoint routing |

### 2.2 Severity Levels

| Level | Definition | PR Action |
|---|---|---|
| `CRITICAL` | Security vulnerabilities, data leaks, SQL injection, RCE | Blocks merge (required status check) |
| `BUG` | Logic errors, NPE, race conditions, incorrect logic | Comment + Slack warning |
| `PERFORMANCE` | N+1 queries, unnecessary allocations, blocking IO in async context | Inline PR comment |
| `SUGGEST` | Readability, naming, refactoring, best practices | Comment (does not block merge) |

---

## 3. Requirements

### 3.1 Functional Requirements

#### FR-1: Webhook Integration
- Support webhooks from GitHub, GitLab, Bitbucket with HMAC SHA-256 validation
- Handle events: `PR opened`, `PR updated (synchronize)`, `PR reopened`
- Respond to webhook in < 3 seconds (async processing via Redis queue)
- Retry logic: 3 attempts with exponential backoff on processing failure

#### FR-2: Static Analysis
- Run Semgrep only on files changed in the PR — not the entire repository
- Language-specific rulesets: `owasp`, `security-audit`, `correctness` per language
- Support custom rules in the `.semgrep/` directory of the repository
- Hard limit: analyze diffs up to 500 changed lines (configurable)

#### FR-3: AI Review
- Send git diff to AI Model with project language context and change type
- Structured JSON output: `[{severity, file, line, message, suggestion}]`
- Limit diff to 8000 tokens; if larger — analyze per file separately
- Incremental review: on PR update, analyze only new commits

#### FR-4: PR Comments
- Inline comments on specific lines in files where issues are found
- Summary comment with overall review result and statistics by severity
- Remove outdated comments on re-review of the same PR
- Support 👍/👎 reactions to collect feedback on AI comment quality

#### FR-5: Per-Project Configuration
- `.codereview.yml` file in the repository root to override settings
- Configuration: language, severity threshold, disabled rules, Slack channel
- Ability to disable AI review or static analysis per project

### 3.2 Non-Functional Requirements

| Requirement | Metric | Target |
|---|---|---|
| Performance | Time from webhook to first comment | < 3 minutes for PRs up to 200 lines |
| Availability | Orchestrator service uptime | > 99% (systemd auto-restart) |
| Security | Webhook validation | HMAC SHA-256 for each platform |
| Security | API keys and tokens | Environment variables, never in code |
| Scalability | Concurrent PRs | Up to 20 simultaneously (Redis queue) |
| Observability | Logging | Structured JSON logs, daily rotation |
| Cost | AI Model API spend | Budget alert at > $50/month |

### 3.3 VPS Specifications

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Disk | 40 GB SSD | 80 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| Python | 3.11+ | 3.12 |
| Redis | 7.x | 7.2+ |
| MySQL | 8.0+ | 8.4 |

---

## 4. Step-by-Step Implementation Plan

### Phase 1 — Foundation & Basic Pipeline

**Step 1: VPS Setup**
- Provision Ubuntu server, configure Nginx with SSL (Let's Encrypt)
- Install Python environment, Redis, MySQL
- *Outcome: Server ready for deployment*

**Step 2: Orchestrator Skeleton**
- Create FastAPI app with webhook endpoint and HMAC validation
- *Outcome: `POST /webhook/bitbucket` accepts and validates requests*

**Step 3: Bitbucket Adapter**
- Implement `get_diff()`, `post_comment()`, `post_inline_comment()` via Bitbucket API
- *Outcome: Diff reading and comment posting via API*

**Step 4: Async Queue**
- Set up Redis + Celery worker, retry logic, structured JSON logging
- *Outcome: Webhook responds in < 1 sec, tasks queued with retry*

**Step 5: Semgrep Integration (Java)**
- Run Semgrep on diff, parse JSON output
- *Outcome: Semgrep finds issues in Java diffs*

**Step 6: End-to-End Test (Bitbucket)**
- Full cycle test: Bitbucket PR → webhook → Semgrep → inline comment
- *Outcome: Full pipeline working on test repo*

### Phase 2 — AI Review

**Step 7: AI Model API Integration**
- Send diff to AI Model, parse structured JSON response
- *Outcome: AI returns structured list of issues*

**Step 8: Prompt Engineering**
- Define severity levels, language context, JSON schema validation
- *Outcome: Quality prompt with low noise level*

**Step 9: Token Management**
- Implement diff truncation, per-file analysis for large PRs
- *Outcome: Handles PRs up to 2000 lines*

**Step 10: Results Merge**
- Combine Semgrep + AI results: deduplication, severity sorting
- *Outcome: Single issue list without duplicates*

**Step 11: Incremental Review**
- Analyze only new commits on PR update
- *Outcome: No repeated comments on each push*

**Step 12: Calibration**
- Test on real Java PRs, tune prompt, collect feedback
- *Outcome: Accuracy > 70%, noise < 30%*

### Phase 3 — Multi-Platform & Multi-Language

**Step 13: GitHub Adapter**
- Implement webhooks, diff API, inline comments via PyGithub
- *Outcome: GitHub PRs receive reviews*

**Step 14: GitLab Adapter**
- Implement MR webhooks, discussions API via python-gitlab
- *Outcome: GitLab MRs receive reviews*

**Step 15: .NET (C#) Support**
- Add Semgrep rules + AI prompts for C#
- *Outcome: C# PRs are analyzed*

**Step 16: PHP Support**
- Add Semgrep + PHPStan + AI prompts for PHP
- *Outcome: PHP PRs are analyzed*

**Step 17: JavaScript/TypeScript Support**
- Add ESLint + Semgrep + AI prompts for JS/TS
- *Outcome: JS/TS PRs are analyzed*

**Step 18: Per-Project Config**
- Implement `.codereview.yml` parser and override logic
- *Outcome: Each project has its own configuration*

### Phase 4 — Notifications, Monitoring, Go-Live

**Step 19: Slack Integration**
- Post review summary, alert on CRITICAL issues
- *Outcome: Teams receive Slack notifications*

**Step 20: MySQL Metrics Schema**
- Store issues, PRs, accuracy, timing, cost data
- *Outcome: Data stored for analysis and reporting*

**Step 21: Grafana Dashboard**
- Visualize issues per team, severity trends, API cost
- *Outcome: Metrics dashboard available*

**Step 22: Rate Limiting & Cost Controls**
- Add rate limiting, AI Model API budget alerts
- *Outcome: Protection against abuse and overspend*

**Step 23: Documentation**
- Write README, `.codereview.yml` reference, runbook
- *Outcome: Teams can self-configure*

**Step 24: Go-Live**
- Gradual rollout per department, collect first-week feedback
- *Outcome: System in production for all departments*

---

## 5. Risks and Mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| High false positive rate from AI | High | Medium | Feedback loop (👍/👎), regular prompt updates |
| AI Model API unavailable or high latency | Low | Medium | Fallback to Semgrep-only; retry with exponential backoff |
| API key leakage | Low | Critical | Env variables on VPS, monthly key rotation |
| Large PR > 2000 lines | Medium | Low | Hard limit, skip AI with notification to PR author |
| Webhook spam or DDoS | Low | Medium | Nginx rate limiting, HMAC validation |
| AI Model API budget exceeded | Medium | Medium | Budget alert at $50/month, max_tokens limit |

---

## 6. Success Metrics

| Metric | Target | Measurement |
|---|---|---|
| Review time (webhook → comment) | < 3 minutes | Grafana / structured logs |
| AI comment accuracy (useful 👍) | > 70% | Feedback reactions in PR |
| False positive rate | < 25% | Negative reactions / total comments |
| PR coverage | 100% of PRs receive review | PRs without review / total PRs |
| Defects found before merge | +40% from baseline | Comparison with previous quarter |
| AI Model API cost | < $100 / month | Provider dashboard + Grafana |

---