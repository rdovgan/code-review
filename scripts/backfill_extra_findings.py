"""
One-time backfill for metrics:extra_findings.

Approximation formula (best we can do from cumulative data):
    extra_findings ≈ max(0, total_findings - 5 × total_reviews)

Underestimates slightly because PRs with 0-4 findings "use up" some
of the free 5-per-PR allowance, but there's no per-PR history to replay.

Usage:
    # Local (uses REDIS_URL from .env or default localhost)
    python scripts/backfill_extra_findings.py

    # Docker (runs inside the app container, picks up env vars)
    docker compose exec app python scripts/backfill_extra_findings.py
"""
import sys
from pathlib import Path

# Allow running from project root or inside docker container
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings

import redis

settings = get_settings()
r = redis.from_url(settings.REDIS_URL, decode_responses=True)

# --- read existing counters ---

findings_sev = r.hgetall("metrics:findings:severity")
reviews = r.hgetall("metrics:reviews")
existing_extra = int(r.get("metrics:extra_findings") or 0)

total_findings = sum(int(v) for v in findings_sev.values())
total_reviews = sum(int(v) for k, v in reviews.items() if k != "skipped")

extra = max(0, total_findings - 5 * total_reviews)

print(f"  Total findings (all time):  {total_findings}")
print(f"  Total reviews  (non-skip):  {total_reviews}")
print(f"  Free allowance (5 × rev):   {5 * total_reviews}")
print(f"  Approx extra findings:      {extra}")
print(f"  Currently in Redis:         {existing_extra}")

if existing_extra >= extra:
    print("\n  Already up to date — nothing to do.")
    sys.exit(0)

confirm = input(f"\n  Set metrics:extra_findings = {extra}? [y/N] ").strip().lower()
if confirm != "y":
    print("  Aborted.")
    sys.exit(1)

r.set("metrics:extra_findings", extra)
print(f"  Done. metrics:extra_findings = {extra}")
