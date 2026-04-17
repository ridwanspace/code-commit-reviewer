"""code-reviewer.py entrypoint — CLI parsing, orchestration, exit codes."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from dotenv import load_dotenv
from dateutil import parser as dateutil_parser

import github_client
import gemini_client
import report_writer
from github_client import GithubClient
from diff_filter import (
    filter_commit_files,
    enforce_aggregate_budget,
    total_patch_bytes,
)
from gemini_client import GeminiReviewer, ReviewRequest, estimate_tokens, SYSTEM_PROMPT, build_user_prompt
from report_writer import ReportMeta, write_report


log = logging.getLogger("code-reviewer")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def err(msg: str) -> None:
    """Print a message to stderr."""
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Period resolver                                                             #
# --------------------------------------------------------------------------- #

_PERIOD_BAD_MSG = (
    "ERROR: --timeperiod must be one of: '10' (last N commits), "
    "'7d' (last N days), or '2026-04-01:2026-04-17' (date range)."
)

_COUNT_RE = re.compile(r"^\d+$")
_DAYS_RE = re.compile(r"^(\d+)d$")
_RANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}:\d{4}-\d{2}-\d{2}$")


@dataclass
class Period:
    kind: Literal["count", "days", "range"]
    since: datetime | None   # UTC
    until: datetime | None   # UTC
    count: int | None
    display: str             # per PRD §8.3


def resolve_period(raw: str, now: datetime) -> Period:
    """Parse the --timeperiod string into a concrete Period.

    Parse order (first match wins):
      1. ^\\d+$            -> count (1..200)
      2. ^(\\d+)d$         -> days  (1..365)
      3. YYYY-MM-DD:YYYY-MM-DD -> range

    Raises ValueError on any parse / validation failure. Orchestration
    maps ValueError to exit code 2.
    """
    raw = raw.strip() if raw is not None else ""

    # 1) count
    if _COUNT_RE.match(raw):
        n = int(raw)
        if not (1 <= n <= 200):
            raise ValueError(
                f"ERROR: --timeperiod count must be between 1 and 200 (got {n})."
            )
        return Period(
            kind="count",
            since=None,
            until=None,
            count=n,
            display=f"Last {n} commits on branch",
        )

    # 2) days
    m = _DAYS_RE.match(raw)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 365):
            raise ValueError(
                f"ERROR: --timeperiod days must be between 1 and 365 (got {n})."
            )
        until = now
        since = now - timedelta(days=n)
        display = (
            f"Last {n} days ({since:%Y-%m-%dT%H:%M:%SZ} "
            f"→ {until:%Y-%m-%dT%H:%M:%SZ})"
        )
        return Period(
            kind="days",
            since=since,
            until=until,
            count=None,
            display=display,
        )

    # 3) explicit range
    if _RANGE_RE.match(raw):
        start_str, end_str = raw.split(":", 1)
        try:
            start_date = dateutil_parser.isoparse(start_str)
            end_date = dateutil_parser.isoparse(end_str)
        except (ValueError, OverflowError) as e:
            raise ValueError(_PERIOD_BAD_MSG) from e

        since = start_date.replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
        )
        until = end_date.replace(
            hour=23, minute=59, second=59, microsecond=0, tzinfo=timezone.utc
        )
        if since > until:
            raise ValueError(
                "ERROR: --timeperiod range start must be <= end "
                f"(got {start_str} > {end_str})."
            )
        return Period(
            kind="range",
            since=since,
            until=until,
            count=None,
            display=f"{start_str} → {end_str}",
        )

    # 4) no match
    raise ValueError(_PERIOD_BAD_MSG)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

_REPONAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments. Exits 2 on validation errors."""
    parser = argparse.ArgumentParser(
        prog="code-reviewer.py",
        description=(
            "Automated enterprise code review for ridwanspace/* repos. "
            "Fetches commits from GitHub, aggregates diffs, sends them to "
            "Gemini, and writes a Markdown report."
        ),
    )
    parser.add_argument(
        "--reponame",
        dest="reponame",
        type=str,
        required=True,
        help="Repository name under ridwanspace/* (owner is hardcoded).",
    )
    parser.add_argument(
        "--branch",
        dest="branch",
        type=str,
        required=True,
        help="Branch name to review.",
    )
    parser.add_argument(
        "--timeperiod",
        dest="timeperiod",
        type=str,
        required=True,
        help=(
            "One of: '10' (last N commits), '7d' (last N days), "
            "or '2026-04-01:2026-04-17' (date range)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        type=str,
        default="./reports",
        help="Directory for generated reports (default: ./reports).",
    )
    parser.add_argument(
        "--max-context-bytes",
        dest="max_context_bytes",
        type=int,
        default=40000,
        help="Cap for the codebase-context block sent to Gemini (default: 40000).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        dest="verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG-level logging.",
    )

    args = parser.parse_args(argv)

    # Post-parse validation.
    if not _REPONAME_RE.match(args.reponame or ""):
        err(
            "ERROR: --reponame must match [A-Za-z0-9._-]+ "
            f"(got {args.reponame!r})."
        )
        sys.exit(2)

    if not (args.branch or "").strip():
        err("ERROR: --branch must be a non-empty string.")
        sys.exit(2)

    return args


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #

def run(
    args: argparse.Namespace,
    period: Period,
    github_key: str,
    gemini_key: str,
) -> int:
    """End-to-end run. Returns a POSIX exit code."""
    repo = args.reponame
    branch = args.branch

    # 1. Verify branch.
    log.info("Verifying branch ridwanspace/%s@%s…", repo, branch)
    gh = GithubClient(github_key)
    gh.verify_branch(repo, branch)

    # 2. List commits per period kind.
    log.info("Listing commits: %s", period.display)
    if period.kind == "count":
        commits = gh.list_commits(repo, branch, max_count=period.count)
    else:
        commits = gh.list_commits(
            repo, branch, since=period.since, until=period.until
        )

    # 3. Empty window -> soft exit.
    if not commits:
        print("No commits found in the specified period. No report generated.")
        return 0

    # 4. Fetch + filter diffs per commit.
    log.info("Fetching diffs for %d commits…", len(commits))
    accum: list = []
    for commit in commits:
        files = gh.get_commit_diff(repo, commit.sha)
        files = filter_commit_files(files)
        accum.append((commit, files))

    # 5. Enforce aggregate patch-byte budget (drops low-signal files).
    accum = enforce_aggregate_budget(accum)

    total_files = sum(len(files) for _c, files in accum)
    total_bytes = total_patch_bytes(accum)
    log.info(
        "Filtered to %d reviewable files across %d commits (%d KB patch)",
        total_files,
        len(accum),
        total_bytes // 1024,
    )

    # 6. Load codebase context bundle.
    context = gh.fetch_context_bundle(repo, branch, args.max_context_bytes)
    if not context:
        log.warning("No codebase context files found (manifests/README missing).")
    else:
        log.info("Loaded codebase context (%d KB)", len(context) // 1024)

    # 7. Build request + call Gemini.
    reviewer = GeminiReviewer(gemini_key)
    req = ReviewRequest(
        reponame=repo,
        branch=branch,
        period_display=period.display,
        commit_count=len(accum),
        context_block=context,
        commits=accum,
    )

    # Best-effort token estimate for the progress log.
    try:
        est = estimate_tokens(SYSTEM_PROMPT + build_user_prompt(req))
        log.info("Sending to Gemini (~%d tokens)…", est)
    except Exception:
        log.info("Sending to Gemini…")

    llm_body = reviewer.review(req)
    log.info("Gemini responded (%d chars)", len(llm_body))

    # 8. Write report.
    meta = ReportMeta(
        reponame=repo,
        branch=branch,
        period_display=period.display,
        commit_count=len(accum),
        generated_at=datetime.now(timezone.utc),
        commits=accum,
    )
    path = write_report(args.output_dir, meta, llm_body)
    log.info("Wrote report: %s", path)

    # Final stdout line is the path (machine-parseable).
    print(path)
    return 0


# --------------------------------------------------------------------------- #
# Main entrypoint                                                             #
# --------------------------------------------------------------------------- #

def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # Load .env, read required keys. Handle BEFORE the big try/except so the
    # exact message + exit 1 are guaranteed.
    load_dotenv()
    github_key = os.environ.get("GITHUB_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not github_key or not gemini_key:
        err("ERROR: GITHUB_API_KEY or GEMINI_API_KEY missing from .env")
        sys.exit(1)

    # Resolve period outside the main run() call so ValueError maps cleanly
    # to exit 2 even if run() also raises ValueError for a different reason.
    try:
        period = resolve_period(args.timeperiod, datetime.now(timezone.utc))
    except ValueError as e:
        err(str(e))
        sys.exit(2)

    try:
        rc = run(args, period, github_key, gemini_key)
        sys.exit(rc)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except ValueError as e:
        err(str(e))
        sys.exit(2)
    except github_client.NotFound as e:
        err(f"ERROR: {e}")
        sys.exit(3)
    except github_client.RateLimited as e:
        err(
            "ERROR: GitHub rate limit exhausted; resets at "
            f"{e.reset_at:%Y-%m-%dT%H:%M:%SZ}"
        )
        sys.exit(4)
    except gemini_client.TokenBudgetExceeded:
        err("ERROR: Prompt exceeds safe token budget. Narrow the time period.")
        sys.exit(6)
    except gemini_client.GeminiError as e:
        err(f"ERROR: Gemini call failed: {e}")
        sys.exit(5)
    except report_writer.ReportWriteError as e:
        err(f"ERROR: Could not write report: {e}")
        sys.exit(7)
    except Exception:
        if args.verbose:
            traceback.print_exc()
        else:
            err("ERROR: Unexpected error. Re-run with --verbose for details.")
        sys.exit(99)


if __name__ == "__main__":
    main()
