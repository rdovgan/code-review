# Rules for choosing per-repo settings

Every value below is a proposal for the user to confirm, not a decision. Field meanings and
defaults: README.md, "Configuring projects" and "Review modes".

## Identity

- **Key** — `workspace/slug` from the repo's git remote (`repo_full_name` in the scan). Never derive it
  from the folder name: folders can be renamed (`revpal-ui` is `revpal-plus-ui`).
- **Snapshot folder** — the agent finds a repo as `<PROJECTS_SNAPSHOT_DIR>/<slug>`, slug = the part
  after `/` in the key. If the folder name differs from the slug, agentic review cannot find it: tell
  the user to rename the folder (or clone it again under the slug).

## language

From the scan's `language` (most source files): `java`, `js` (JS/TS/Vue), `php`, `dotnet`, `python`.
Kotlin counts as `java`. Mixed repos: the language of the code that changes most — ask if unclear.

## target_branches

Not visible in a shallow snapshot. Ask, or keep the existing value. Common shape:
`[master, main, <long-lived env branches>]`. Warn when a repo has `[]` (= every target branch) and
uses `agentic`/`shadow`: the bot keeps a depth-1 clone per distinct target branch.

Two Java lines (`master` = Java 8, `main` = Java 21) are two branches of the same repo; both go into
`target_branches` if PRs go to both.

## ignore_paths

Start from the scan's `ignore_path_candidates`, keep only what is generated, vendored or data:
- build output: `**/target/**`, `dist/**`, `build/**`
- dependencies: `vendor/**`, lock files (`package-lock.json`, `yarn.lock`, `composer.lock`)
- generated code: `**/generated/**`, `**/*.Designer.cs`, `**/*.min.js`
- data / docs / IDE: `**/*.csv`, `**/*.md`, `.idea/**`
- `**/*.properties`, `**/*.sql`, `**/migrations/**` — only if the team doesn't want them reviewed
  (they often hold real config and schema changes; ask).

## semgrep_rules and static_analysis

| language | semgrep_rules |
|---|---|
| java | `owasp`, `security-audit`, `p/java`, `p/findsecbugs`, `p/secrets` |
| js | `owasp`, `p/javascript`, `p/typescript`, `p/react` (if React), `p/secrets` |
| php | `owasp`, `p/php`, `p/secrets` |
| dotnet | `owasp`, `p/csharp`, `p/secrets` |
| python | `owasp`, `security-audit`, `p/python`, `p/django`/`p/flask` (if used), `p/secrets` |

`static_analysis: false` for very large legacy repos where Semgrep was noisy or slow (ask; check the
existing config — a value set there was usually set for a reason).

## max_diff_lines

By size (`source_lines` in the scan): < 20k → 3000; 20k–200k → 5000–10000; > 200k → 15000.
Keep a larger existing value.

## review_mode

| Situation | Mode |
|---|---|
| Repo is not in the snapshot folder | `diff` (agentic would always fall back) |
| Library with consumers among the scanned repos | `agentic` — finding what a change breaks in consumers is where the agent pays off |
| The largest / most central repo, or the first rollout | `shadow` first, `agentic` after comparing (`scripts/shadow_report.py`) |
| UI whose backends are in the snapshot folder | `agentic` |
| UI with `target_branches: []` | `diff`, or set target branches first |
| Small standalone service, no context repos | `diff` — the agent has little to look at |

## context_repos

For each repo: **what it depends on** (the agent reads definitions there) **+ what depends on it**
(the agent finds what a change breaks). Both build and runtime edges count:

- build: Maven/npm dependencies (exact, from the scan);
- runtime: HTTP calls, proxies, shared session/token stores, message topics (verified);
- UI → the backends whose endpoints it calls; a backend → the UIs that call it.

Rules:
- Only repos present in the snapshot folder. Never the repo itself (the bot skips it anyway).
- Direct neighbours only, not the whole stack. Transitive ones only when code calls them directly.
- A library almost everything uses (e.g. a utils jar): list it in the repos that use it heavily, but
  for the library itself list only its main consumers (≤ 10).
- Direction matters for runtime calls: if A calls B's API, then B's context includes A (a removed
  endpoint in B breaks A), and A's context includes B (A reads B's contract). A change in A cannot
  break B.
- Write each list explicitly. YAML anchors cannot merge lists, so shared "stacks" only work when a
  repo needs exactly one stack.

## agent_max_steps

20 by default. 30 when `context_repos` has more than 7 entries or the repo has > 200k source lines
— the agent needs steps for both dependencies and consumers. More than 40 rarely helps and costs
tokens on every step.

## Token budget (`AI_DAILY_TOKEN_BUDGET` in .env)

If the bot already runs, measure instead of guessing (`curl -s localhost:8000/metrics`):
- PRs per repo: `code_review_reviews_by_project_total` (divide by the days since the counters were reset);
- tokens per review: `code_review_ai_tokens_total` (sum of kinds) / `code_review_reviews_total`.

Otherwise, rough tokens per review: `diff` 30k–100k (more for big PRs: the diff, changed files and
PR context are sent, plus a verification pass when there are findings); `agentic` 150k–600k (the
conversation is re-sent on every step; capped by `AGENT_MAX_TOKENS_PER_REVIEW`, 800k); `shadow` =
diff + agentic.

Daily budget ≈ Σ over repos (PRs per day × tokens per review), × 1.5 for bursts. When the budget runs
out every review gets `STOPPED` until midnight, so err on the high side and watch the first days.

## Known false leads

- Config keys named after a repo do not always point at it: `bp.supplier_api.*` keys point at
  mbp's own `/xml/.../supplierapi` endpoints, not at the `supplier-api` repo. Trust the URL's host and
  path (whose mount point is it?) over the key name.
- A shaded "uber jar" (e.g. `ChannelIntegration-…-without-dependencies`) brings its whole dependency
  tree into the consumer: a consumer of the uber jar depends on everything inside it.
- A team's module notes can be outdated (one said an uber jar bundled `revpal`; the POMs said no).
  The code wins; mention the disagreement to the user.
- Coupling the scan cannot see: shared session/token stores in Redis (one app writing tokens another
  reads), shared database tables, message queues. Ask the user, and grep for the session/token class
  names and topic names the scan lists.
