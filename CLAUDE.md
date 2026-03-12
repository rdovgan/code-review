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
5. Document the webhook setup in `PROJECTS.md`

## How to Add a New Language

1. Create `prompts/<language>_prompt.md` — include language-specific CRITICAL/BUG examples
2. Add extension mappings to `_EXT_TO_LANG` in `app/config/project_config.py`
3. Add a Semgrep rule mapping to `SEMGREP_RULE_MAP` in `app/analyzers/semgrep_runner.py`
4. Add a quick-reference config block in `PROJECTS.md`

## Key Design Decisions

| Decision | Rationale |
|---|---|
| `Finding.dedup_key` via sha256 | Single dedup mechanism; Semgrep findings win ties (inserted first) |
| Prompt files as `.md`, not hardcoded | Prompt tuning without code deploys or restarts |
| `ThreadPoolExecutor` in Celery | Semgrep is subprocess, Anthropic SDK is sync — asyncio adds complexity with no benefit |
| `task_acks_late=True` | Message stays in queue until task completes — prevents data loss on worker crash |
| `worker_prefetch_multiplier=1` | Semgrep is CPU-heavy; one task per worker prevents resource contention |
| `HTTP 202` from webhook handler | Never wait for analysis — guarantees <1 sec webhook response |
| `BOT_MARKER` in every comment | Platform-agnostic way to find own comments for cleanup on re-review |
| Central `config/projects.yml` | Onboard projects without touching their codebase; `.codereview.yml` in repo can override |
| Graceful analyzer failures | If Semgrep or AI fails, task continues with the other's results |

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
