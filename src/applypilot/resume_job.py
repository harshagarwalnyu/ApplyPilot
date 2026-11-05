"""Continue one application in the worker Chrome exactly where the page is now (no navigation, no new tab).

  python -m applypilot.resume_job --url-like akamai [--note "..."] [--worker 0] [--model haiku] [--effort high]

For when a human (or the operator) got past a sign-in / emailed code in the worker browser and the agent
must pick up from that page. Records the result the same way the orchestrator does.
"""
from __future__ import annotations

import argparse
import sys

from applypilot import config
from applypilot.apply import launcher
from applypilot.database import get_connection


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url-like", required=True, help="substring of the job url or company to resume")
    ap.add_argument("--note", default="", help="extra operator note for the agent")
    ap.add_argument("--worker", type=int, default=0)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--effort", default="high")
    args = ap.parse_args(argv)
    config.load_env()

    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    like = f"%{args.url_like}%"
    rows = conn.execute("SELECT * FROM jobs WHERE (url LIKE ? OR company LIKE ?) AND COALESCE(apply_status,'') != 'applied'",
                        (like, like)).fetchall()
    if len(rows) != 1:
        print(f"need exactly one unapplied job matching {args.url_like!r}, found {len(rows)}")
        return 2
    job = dict(rows[0])
    conn.execute("UPDATE jobs SET apply_status='in_progress' WHERE url=?", (job["url"],))
    conn.commit()

    launcher.set_effort(args.effort)
    port = launcher.BASE_CDP_PORT + args.worker if hasattr(launcher, "BASE_CDP_PORT") else 9222 + args.worker
    context = ("The human operator already completed sign-in and the emailed verification in this browser. "
               "The application form is open in the current tab. Continue filling it from the current page, "
               "section by section, review, and submit.")
    if args.note:
        context += "\nOPERATOR NOTE: " + args.note
    result, duration_ms, _ = launcher.run_job(job, port=port, worker_id=args.worker, model=args.model,
                                              skip_tab_reset=True, extra_context=context)
    print("result:", result)
    if result == "applied":
        launcher.mark_result(job["url"], "applied", duration_ms=duration_ms)
    elif result.startswith("failed:"):
        reason = result.split(":", 1)[1]
        launcher.mark_result(job["url"], "failed", reason, permanent=launcher._is_permanent_failure(result),
                             duration_ms=duration_ms)
    else:
        launcher.mark_result(job["url"], result, duration_ms=duration_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
