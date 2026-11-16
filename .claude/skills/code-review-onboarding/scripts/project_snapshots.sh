#!/usr/bin/env bash
# Shallow, single-branch copies of the reviewed repos — the folder the Code Review Bot's agent
# reads (PROJECTS_HOST_DIR, default /opt/projects). Only for servers that don't have such a
# folder yet; if one exists and is kept current by something else, leave it alone.
#
#   project_snapshots.sh clone <git-url> [branch] [dir]   # add a repo (folder = repo slug)
#   project_snapshots.sh refresh [dir]                    # update every repo to its branch tip
#
# Refresh keeps depth 1 and drops old objects, so the folder never grows with history.
# Cron, nightly at 03:00:
#   0 3 * * * /opt/code-review/.claude/skills/code-review-onboarding/scripts/project_snapshots.sh refresh >> /var/log/project-snapshots.log 2>&1
# Git must be able to authenticate to the remotes non-interactively (SSH key or credential helper).
set -euo pipefail

cmd="${1:-}"
case "$cmd" in
  clone)
    url="${2:?git url required}"
    branch="${3:-}"
    dir="${4:-${PROJECTS_HOST_DIR:-/opt/projects}}"
    slug="$(basename "${url%.git}")"
    mkdir -p "$dir"
    if [ -e "$dir/$slug" ]; then echo "exists: $dir/$slug"; exit 0; fi
    git clone --quiet --depth 1 --single-branch ${branch:+--branch "$branch"} "$url" "$dir/$slug"
    chmod -R o+rX "$dir/$slug"   # the bot's container user (uid 1000) must be able to read it
    echo "cloned $slug ($(git -C "$dir/$slug" rev-parse --abbrev-ref HEAD))"
    ;;
  refresh)
    dir="${2:-${PROJECTS_HOST_DIR:-/opt/projects}}"
    for repo in "$dir"/*/; do
      [ -d "$repo/.git" ] || continue
      branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD)"
      if git -C "$repo" fetch --quiet --depth 1 origin "$branch" \
         && git -C "$repo" reset --quiet --hard FETCH_HEAD \
         && git -C "$repo" clean --quiet -fdx; then
        git -C "$repo" reflog expire --expire=now --all
        git -C "$repo" gc --quiet --prune=now
        chmod -R o+rX "$repo"
        echo "$(date '+%F %T') ok   $(basename "$repo") ($branch)"
      else
        echo "$(date '+%F %T') FAIL $(basename "$repo") ($branch)" >&2
      fi
    done
    ;;
  *)
    sed -n '2,13p' "$0"
    exit 2
    ;;
esac
