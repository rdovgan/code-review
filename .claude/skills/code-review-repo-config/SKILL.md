---
name: code-review-repo-config
description: Map a set of source repositories and configure the Code Review Bot for them — scan the repos (e.g. /opt/projects), find build and runtime dependencies (Maven/npm, HTTP calls, proxies, UI→backend), draw the dependency map, propose per-repo settings (language, target_branches, ignore_paths, semgrep rules, max_diff_lines, review_mode, context_repos, agent_max_steps) and the daily token budget, then write config/projects.yml. Use when adding repositories to the bot, enabling agentic or shadow review, building "stacks"/context_repos, or re-mapping after repos changed.
---

# Configure repositories for the Code Review Bot

Turns "here are our repos" into a verified dependency map and a ready `config/projects.yml`.
Run it from the bot's folder (usually `/opt/code-review`). Read `README.md` ("Configuring projects",
"Review modes") once if you haven't. Per-field rules: `reference/config-rules.md` — read it before
step 5.

## Ground rules

- Read-only on the source repos: never fetch, checkout, clean or write in the snapshot folder
  (`/opt/projects`). It is shared with other tools.
- Never read or print the values in `.env` or `config/credentials.yml`. Check that a key is set with
  `grep -q '^KEY=.' .env`.
- The code wins over notes and over guesses. Every edge on the map is either exact (build files) or
  verified in code with a `file:line`; anything else is marked UNCERTAIN and goes to the user.
- Change only `config/projects.yml` (plus `config/repo-map.md`). Back it up first.

## 1. Gather inputs

Ask only for what you can't find:
- snapshot folder (default `/opt/projects`; must match the mount in `docker-compose.yml`);
- which repos to configure (default: every repo in the snapshot folder);
- for each repo, or as a default, `target_branches` (not visible in shallow clones);
- business coupling the code may not show: proxies between services, shared Redis/DB, queues.

Read the current `config/projects.yml` if it exists (keys already set there were set deliberately —
keep them unless the user agrees to change them).

## 2. Scan

```bash
python3 .claude/skills/code-review-repo-config/scripts/scan_repos.py /opt/projects \
  [--repos a,b,c] --out /tmp/repo-scan.json
```

It prints a summary: per repo (remote key, branch, kind ui/service/library, language, size, whether
it has a CLAUDE.md), exact build edges, runtime edge candidates and runtime hints (HTTP clients,
proxy classes, endpoints, API env vars, topics). Full detail is in the JSON. It takes seconds to a
couple of minutes. Repos asked for but not in the folder are listed — they can't go agentic.

## 3. Verify the runtime edges

Build edges are exact. Runtime edges are candidates: confirm each in code before using it, with a
bounded effort (a few targeted greps/reads per edge; use parallel subagents for many repos).

- **Candidate X → Y**: open the evidence (config key or file from the scan) and find the call site
  (RestTemplate/WebClient/Feign/HttpClient/axios). Note the direction: who calls whom.
- **Proxy classes** (`*Proxy*` in the hints): read them — a proxy means "X forwards to Y", so a
  removed endpoint in Y breaks X and X's clients.
- **UIs**: map the API env vars and path prefixes to the backend whose mounts/endpoints match. Env
  files are usually not in git; match by endpoint paths.
- **What the scan can't see**: shared session/token stores (grep for token/session classes writing
  to Redis), message topics (the hints list listener topics; find their producers), shared tables.
- Check the traps in `reference/config-rules.md` ("Known false leads").

Drop disproved edges. Keep UNCERTAIN ones out of `context_repos` unless the user confirms.

## 4. Draw the map

Write `config/repo-map.md` (instance-specific, like `projects.yml`):

1. A Mermaid `flowchart LR` with subgraphs Frontends / Services / Libraries. Solid arrows = build
   dependency (`A --> B`: A builds against B), dashed = runtime (`A -.HTTP.-> B`, label proxies,
   shared tokens, topics). Leave out libraries used by almost every repo (say so under the chart).
2. A table: repo | kind | depends on | used by | evidence (file:line or build file).
3. How to read it: if a box changes, the repos with arrows pointing at it are at risk.

## 5. Propose settings

Apply `reference/config-rules.md` per repo and present ONE table for the user to confirm:

| repo | key | language | target_branches | review_mode | agent_max_steps | context_repos | notes |

Plus: suggested `ignore_paths` / `semgrep_rules` / `max_diff_lines` only where they differ from the
current config, and the daily token budget with how it was estimated (measured from `/metrics` if
the bot runs). Call out what needs a decision: repos missing from the snapshot folder, UIs with
`target_branches: []`, repos without a CLAUDE.md (suggest one — it is sent to the AI as the
project's conventions), any UNCERTAIN edge.

Wait for the user's confirmation or corrections before writing.

## 6. Write config/projects.yml

1. `cp config/projects.yml config/projects.yml.$(date +%F-%H%M).bak` (or start from
   `config/projects.yml.example` if there is none).
2. Edit in place, preserving comments, anchors (`<<: *x`) and every key you were not asked to change.
   Add `review_mode`, `context_repos` (explicit lists of `workspace/slug`), `agent_max_steps` where
   it differs from 20. New repos: model the entry on a similar existing one.
3. New repos also need a token/secret in `config/credentials.yml` and a webhook — tell the user;
   don't edit credentials yourself.

## 7. Validate

```bash
docker compose exec worker python -c "
import os, yaml
from app.config.project_config import _load_central_config, REVIEW_MODES
names = list(yaml.safe_load(open('config/projects.yml'))['projects'])
for key in names:
    c = _load_central_config(key)
    ctx = c.get('context_repos', [])
    missing = [r for r in [key, *ctx] if not os.path.isdir('/projects/' + r.split('/')[1])]
    bad = c.get('review_mode', 'diff') not in REVIEW_MODES
    print(f'{key:40} {c.get(\"review_mode\",\"diff\"):8} ctx={len(ctx):2} missing={missing}{\" BAD MODE\" if bad else \"\"}')"
```

Any `missing` for an agentic/shadow repo, or `BAD MODE`, must be fixed. The file is read on every
review — no restart.

## 8. Pre-create target-branch snapshots

For each `agentic`/`shadow` repo, every target branch other than the one its snapshot is on
(`git -C /opt/projects/<slug> rev-parse --abbrev-ref HEAD`) needs the bot's own depth-1 clone.
Create them now so the first review doesn't spend its time budget on the clone:

```bash
docker compose exec worker python -m app.context.snapshots <platform> <workspace/slug> <branch>
```

Skip repos with `target_branches: []`. Report the disk used:
`docker system df -v | grep branch_snapshots`.

## 9. Report

- the map (`config/repo-map.md`) and the confirmed settings table;
- the `config/projects.yml` diff and the validation output;
- the budget estimate and the `.env` line to set (`AI_DAILY_TOKEN_BUDGET=...`) — the user sets it;
- open items: UNCERTAIN edges, repos missing from the snapshot folder, new repos needing
  credentials + webhooks, repos that would benefit from a CLAUDE.md;
- how to check it works: comment `review` on a PR, `docker compose logs -f worker`, look for
  `Agentic review complete` or `falling back (<reason>)`; for shadow repos
  `docker compose exec worker python scripts/shadow_report.py --details` after a few days.
