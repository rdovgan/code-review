# Code Review Bot — Developer Reference

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
│   ├── ai_calls.py           # bounded requests: timeout from the deadline, retry policy, soft-limit unwrap
│   ├── ai_reviewer.py        # Anthropic SDK, prompt loading, JSON parsing
│   ├── agent_reviewer.py     # review_mode agentic: tool loop (GLM/OpenAI-compatible + Claude)
│   └── merger.py             # dedup (file:line key) + severity sort
├── context/
│   ├── jira.py               # ticket key from branch/title + Jira REST fetch
│   ├── review_context.py     # PR_CONTEXT block: description, commits, ticket, repo guides
│   ├── snapshots.py          # shared /opt/projects snapshots (read-only) + bot's depth-1 branch clones
│   └── code_tools.py         # read_file / grep / find_files / list_dir: snapshot + the PR's files on top
├── workers/
│   └── celery_app.py         # process_review task, ThreadPoolExecutor
└── config/
    ├── settings.py           # pydantic-settings, @lru_cache singleton
    └── project_config.py     # load config/projects.yml + .codereview.yml

config/
└── projects.yml.example      # template; projects.yml is per-instance and git-ignored

prompts/
├── review_base.md            # shared rules + JSON schema; <language>_prompt.md is appended
├── java_prompt.md / dotnet / php / js / python   # language pitfalls only
├── verify_ai.md              # second pass that refutes AI findings
├── agent_review.md           # tool-use instructions, appended for agentic reviews
└── verify_semgrep.md

tests/
├── test_adapters.py
├── test_analyzers.py
├── test_merger.py
├── test_worker.py
└── fixtures/
```

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

## How to Add a New Git Platform Adapter

1. Create `app/adapters/<platform>.py`, implement all abstract methods from `GitPlatform` in `base.py`
2. Register it in `get_adapter()` in `app/adapters/factory.py`
3. Add `POST /webhook/<platform>` route in `app/main.py`
4. Add tests in `tests/test_adapters.py`
5. Document the webhook setup in `README.md`

## How to Add a New Language

1. Create `prompts/<language>_prompt.md` — language-specific pitfalls only; rules and schema are in `prompts/review_base.md`
2. Add extension mappings to `_EXT_TO_LANG` in `app/config/project_config.py`
3. Add a Semgrep rule mapping to `SEMGREP_RULE_MAP` in `app/analyzers/semgrep_runner.py`
4. Add a quick-reference config block in `README.md`

## Key Design Decisions

| Decision | Rationale |
|---|---|
| `Finding.dedup_key` = sha256 of `file:line` | One finding per line across sources (messages differ between Semgrep and AI); higher severity wins, Semgrep wins ties |
| `prepare_diff()` before analysis | Ignored/deleted/binary files are dropped first, then `max_diff_lines` cuts at file/hunk boundaries; skipped files are listed in the summary |
| Changed files fetched once per review | Same contents feed Semgrep and the AI's line-numbered file context (`AI_FILE_CONTEXT_MAX_CHARS`) |
| `stopped` build status when AI reviewed nothing | Budget/API failure must not look like a clean review; partial AI coverage keeps the normal status with a note |
| Review lock: 5 min TTL, released by the worker | Collapses webhook bursts without blocking re-review of a follow-up push |
| Prompt files as `.md`, not hardcoded | Prompt tuning without code deploys or restarts |
| Threads, not asyncio, in Celery | Semgrep is subprocess, the SDKs are sync — asyncio adds complexity with no benefit. Semgrep (+ its verify) runs in a background thread next to the AI review, agent parts run in a thread pool; diff chunks stay sequential (halving and the circuit breaker assume it) |
| `task_acks_late=True` | Message stays in queue until task completes — prevents data loss on worker crash |
| `worker_prefetch_multiplier=1` | Semgrep is CPU-heavy; one task per worker prevents resource contention |
| `HTTP 202` from webhook handler | Never wait for analysis — guarantees <1 sec webhook response |
| `BOT_MARKER` in every comment | Platform-agnostic way to find own comments for cleanup on re-review |
| Central `config/projects.yml`, git-ignored | Onboard projects without touching their codebase; `.codereview.yml` in repo can override. Each bot instance keeps its own registry, so it is not in git — `projects.yml.example` is the template |
| Graceful analyzer failures | If Semgrep or AI fails, task continues with the other's results |
| Semgrep findings AI-verified before posting (`semgrep_ai_verify`) | Cuts static-analysis false positives; fails open (keeps findings unfiltered) if verification errors out — a verification hiccup must never hide a real finding |
| PR_CONTEXT sent with every AI request | Without the PR description, Jira ticket and the repo's CLAUDE.md the model can only match generic patterns; every source is best-effort and capped by `AI_PR_CONTEXT_MAX_CHARS` |
| Whole files packed into AI requests (`AI_MAX_DIFF_TOKENS`) | One request per file made the model judge a service without its DTO/repository from the same PR; diff order keeps a package together |
| AI findings verified by a second AI pass (`ai_verify`) | The reviewer over-reports; a skeptical pass on the same material drops unproven findings. Fails open, and CRITICAL is dropped only with a concrete reason |
| `review_mode` diff / agentic / shadow per repo, central config only | Roll the agentic review out repo by repo; `.codereview.yml` can't switch a repo to the costlier mode |
| Agentic review falls back to diff review on any failure | A PR must never be left unreviewed because a clone, the model or the tool loop failed; the agent gets `_AGENTIC_EXTRA_SECONDS` on top so the fallback keeps the regular time budget |
| Agent parts run in parallel in one window; a failed part's files get the diff review | One slow part used the whole window and the next part's failure threw away the first part's findings (PR #52). Finished parts keep their findings; only the rest is diff-reviewed, not the whole diff again. A part starts only if the answer reserve + `AGENT_MIN_TOOL_WINDOW_SECONDS` fit |
| Every AI request has a read timeout derived from its deadline; the soft limit is only the backstop | Deadlines were checked only between requests and the SDK default was 600 s × 3 tries, so one request could outlive the task. `call_bounded` (`ai_calls.py`): no SDK retries, one own retry on 408/409/429/5xx when time allows, a request that cannot finish in time is not sent, a soft limit wrapped by the SDK is re-raised as `SoftTimeLimitExceeded` |
| GLM final answer sent with thinking off (`GLM_ANSWER_THINKING=auto`, auto-detected) | glm-5.3-flash reasoned ~4 min before writing the final JSON; the lookups are done by then. Only a 400 that a resend without the parameter fixes stops sending it. Z.AI honours the switch but still reasons ~1k tokens on long agent conversations, so reasoning tokens are only logged; the reserve follows observed answer times (slowest × 1.3 + 15 s, 120–270 s), and a timed-out answer sets 270 s with one alert |
| Soft time limit → `stopped`, never `failure` | A timeout says nothing about the code. The handler returns instead of re-raising, so `on_task_failure` cannot overwrite the status; hard = soft + 120 s leaves time to post |
| Diff review: verification after coverage, CRITICAL chunks first | Verifying chunk by chunk spent the time on verification while later chunks went unreviewed. Unverified findings are kept (fail open) and counted in the summary |
| Agent reads the server's `/opt/projects` snapshots, read-only, with the PR's changed files laid on top | No clones or history on disk; the folder is shared with docs generation and a cron, so the bot never fetches or checks out there. Repos not in it are not reviewed agentically |
| Bot's own depth-1 clone for other target branches (`demo-dev`, …), re-cloned and swapped | The shared snapshot is the main branch only; a fresh shallow clone keeps no history and readers never see a half-written folder |
| Own tool loop instead of an agent SDK | Must run on GLM (OpenAI-compatible) as well as Claude |
| Agent sent back once if it answers with < `AGENT_MIN_TOOL_CALLS` lookups | Agentic exists to check the change against the code; an answer from the diff alone is just a pricier diff review |
| Prompt caching in the agent loop; cache reads weighted in the budget (`AI_CACHED_TOKEN_WEIGHT`) | Each step re-sends the conversation; caching keeps depth affordable, and the budget should track cost, not raw tokens |
| Operator alerts in the Mattermost channel, deduplicated in Redis (`alert:<kind>:<repo>`, `ALERT_REPEAT_HOURS`) | Limits and fallbacks only show up in logs otherwise; one message per repo and kind keeps the channel readable when every PR hits the same limit. The agent's time limit is a metric + summary note only: with bounded windows it is expected on big PRs |
| Shadow review is a separate Celery task | It can fail or time out without touching the posted review's build status (the failure signals are bound to `process_review` only) |
| Deadline taken at task start | Time spent fetching and in the agent counts against the soft limit, so the fallback never runs past it. Semgrep runs alongside and is joined before posting (up to the deadline minus `_FINALIZE_RESERVE`) |
| Dynamic per-PR time limits + deadline-aware AI chunks | Bitbucket webhooks carry no file list, so limits scale by the real diffstat (fetched at enqueue); the AI chunk loop stops before the Celery soft limit and returns partial findings — the build status always gets finalized instead of the worker being SIGKILLed mid-review (stuck "in progress") |

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
- [ ] Rate limiting (AI budget alert: `grafana/provisioning/alerting/ai_budget.yml`)
- [ ] Multi-tenant support
