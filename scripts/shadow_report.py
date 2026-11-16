"""Compare shadow (agentic) reviews with the reviews that were posted.

    docker compose exec worker python scripts/shadow_report.py              # one line per review
    docker compose exec worker python scripts/shadow_report.py --details    # plus the findings
    docker compose exec worker python scripts/shadow_report.py --repo ws/repo

Records are kept for 30 days (process_shadow_review in app/workers/celery_app.py).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import redis  # noqa: E402

from app.config.settings import get_settings  # noqa: E402


def _finding_line(f: dict) -> str:
    return f"    [{f['severity']}] {f['file']}:{f['line']} {f['message']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="only this repo (workspace/slug)")
    parser.add_argument("--details", action="store_true", help="print the findings of both reviews")
    args = parser.parse_args()

    r = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    pattern = f"shadow_review:{args.repo}:*" if args.repo else "shadow_review:*"
    records = []
    for key in r.scan_iter(pattern):
        raw = r.get(key)
        if raw:
            records.append((key.removeprefix("shadow_review:"), json.loads(raw)))
    if not records:
        print("No shadow reviews recorded.")
        return 0

    records.sort(key=lambda kr: kr[1].get("recorded_at", 0))
    totals = {"posted": 0, "agent": 0, "both": 0, "failed": 0}
    print(f"{'review':<60} {'posted':>6} {'agent':>6} {'both':>5} {'lookups':>7}  note")
    for name, rec in records:
        totals["posted"] += len(rec["posted"])
        totals["agent"] += len(rec["agent"])
        totals["both"] += rec.get("both", 0)
        totals["failed"] += 1 if rec.get("error") else 0
        note = f"FAILED: {rec['error']}" if rec.get("error") else f"{rec.get('duration_ms', 0) / 1000:.0f}s"
        print(f"{name:<60} {len(rec['posted']):>6} {len(rec['agent']):>6} {rec.get('both', 0):>5} "
              f"{rec.get('tool_calls', 0):>7}  {note}")
        if args.details:
            agent_keys = {(f["file"], f["line"]) for f in rec["agent"]}
            posted_keys = {(f["file"], f["line"]) for f in rec["posted"]}
            only_posted = [f for f in rec["posted"] if (f["file"], f["line"]) not in agent_keys]
            only_agent = [f for f in rec["agent"] if (f["file"], f["line"]) not in posted_keys]
            if only_posted:
                print("  posted only (agent did not confirm):")
                print("\n".join(_finding_line(f) for f in only_posted))
            if only_agent:
                print("  agent only (diff review missed):")
                print("\n".join(_finding_line(f) for f in only_agent))
    print(f"\n{len(records)} review(s): posted {totals['posted']} finding(s), agent {totals['agent']}, "
          f"same line {totals['both']}, agent failed {totals['failed']} time(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
