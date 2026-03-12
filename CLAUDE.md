# Code Review Bot — Developer Guide

## Architecture Overview

```
Webhook → FastAPI (main.py) → Celery Task (workers/celery_app.py)
                                      ↓
                          ThreadPoolExecutor (2 workers)
                          ├── SemgrepRunner (semgrep_runner.py)
                          └── AIReviewer    (ai_reviewer.py)
                                      ↓
                              merger.py (dedup + sort)
                                      ↓
                          GitPlatform adapter (post comments)
```

## Dev Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the API server
uvicorn app.main:app --reload

# Run a Celery worker
celery -A app.workers.celery_app worker --loglevel=info

# Run unit tests
pytest tests/ -m "not integration"

# Run all tests including integration (requires semgrep)
pytest tests/

# Docker (full stack)
docker compose up --build
```

## Adding a New Git Platform Adapter

1. Create `app/adapters/<platform>.py`
2. Implement all abstract methods from `GitPlatform` in `app/adapters/base.py`
3. Add to `get_adapter()` in `app/adapters/factory.py`
4. Add a new webhook route in `app/main.py`
5. Add tests in `tests/test_adapters.py`

## Adding a New Language

1. Create `prompts/<language>_prompt.md` with severity examples specific to that language
2. Add extension mappings to `_EXT_TO_LANG` in `app/config/project_config.py`
3. Add Semgrep rule mapping to `SEMGREP_RULE_MAP` in `app/analyzers/semgrep_runner.py` if applicable

## Project Config (.codereview.yml)

Place in the root of any repository to customize review behavior:

```yaml
language: java          # auto-detect if omitted
ai_review: true
static_analysis: true
block_merge_on:
  - CRITICAL
max_diff_lines: 500
slack_channel: "#code-review"
ignore_paths:
  - "migrations/*"
  - "*.generated.*"
semgrep_rules:
  - owasp
  - security-audit
ai_focus:
  - security
  - bugs
```

## Key Design Decisions

| Decision | Rationale |
|---|---|
| `Finding.dedup_key` via sha256 | Single dedup mechanism; Semgrep findings win ties |
| Prompt files as `.md` | Prompt tuning without code deploys |
| `ThreadPoolExecutor` in Celery | Semgrep is subprocess, Anthropic SDK is sync |
| `task_acks_late=True` | Prevents data loss on worker crash |
| `HTTP 202` from webhook handler | Guarantees <1 sec webhook response |
| `BOT_MARKER` in every comment | Platform-agnostic comment cleanup on re-review |
