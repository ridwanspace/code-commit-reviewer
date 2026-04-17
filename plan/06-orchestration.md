# Plan 06 — `code-reviewer.py` orchestration

**Goal:** Wire the four modules into an end-to-end run. Map every exception to the PRD §9 exit-code matrix. One entrypoint, linear flow, no threads.

**PRD refs:** §3.3 (data flow), §9 (exit codes), §10 (examples).

---

## Flow

```
main()
 ├── parse_args()                                          # Plan 01
 ├── load_dotenv() + read keys                             # → exit 1 on miss
 ├── period = resolve_period(args.timeperiod, now_utc())   # → exit 2 on bad
 ├── gh = GithubClient(GITHUB_API_KEY)
 ├── gh.verify_branch(args.reponame, args.branch)          # → exit 3 on 404
 ├── commits = gh.list_commits(repo, branch, since, until, max_count)
 │       (params derived from period.kind)
 ├── if not commits:
 │       log.info("No commits found …")
 │       sys.exit(0)                                       # soft exit
 ├── for c in commits: files = gh.get_commit_diff(repo, sha)
 │       files = filter_commit_files(files)
 │       accumulate (c, files)
 ├── enforce_aggregate_budget(accum)
 ├── context = gh.fetch_context_bundle(repo, branch, args.max_context_bytes)
 ├── reviewer = GeminiReviewer(GEMINI_API_KEY)
 ├── llm_body = reviewer.review(ReviewRequest(...))        # → exit 5/6
 ├── path = write_report(args.output_dir, ReportMeta(...), llm_body)  # → exit 7
 └── print(path)                                           # machine-parseable
```

## Exception → exit map

Implement in a top-level try/except around the entire orchestration:

```python
try:
    run(args)
except SystemExit:
    raise
except KeyError as e:                         # env missing
    err(f"GITHUB_API_KEY or GEMINI_API_KEY missing from .env"); sys.exit(1)
except ValueError as e:                       # bad --timeperiod (raised inside resolve_period)
    err(str(e)); sys.exit(2)
except github_client.NotFound as e:
    err(str(e)); sys.exit(3)
except github_client.RateLimited as e:
    err(f"GitHub rate limit exhausted; resets at {e.reset_at:%Y-%m-%dT%H:%M:%SZ}"); sys.exit(4)
except gemini_client.TokenBudgetExceeded as e:
    err("Prompt exceeds safe token budget. Narrow the time period."); sys.exit(6)
except gemini_client.GeminiError as e:
    err(f"Gemini call failed: {e}"); sys.exit(5)
except report_writer.ReportWriteError as e:
    err(f"Could not write report: {e}"); sys.exit(7)
except Exception:
    if args.verbose:
        traceback.print_exc()
    else:
        err("Unexpected error. Re-run with --verbose for details.")
    sys.exit(99)
```

`err(msg)` = `print(msg, file=sys.stderr)`.

## Period → list_commits parameters

| `period.kind` | `since` | `until` | `max_count` |
|---|---|---|---|
| `days` | `period.since` | `period.until` | `None` (cap of 200 applies) |
| `range` | `period.since` | `period.until` | `None` |
| `count` | `None` | `None` | `period.count` |

## Progress logging (INFO)

Print a one-line update between phases so the user sees progress on slow networks:
```
INFO Verifying branch ridwanspace/foo@main…
INFO Listing commits since 2026-04-10T00:00:00Z (count=…)
INFO Fetching diffs for 23 commits…
INFO Filtered to 147 reviewable files across 23 commits (842 KB patch)
INFO Loaded codebase context (18 KB)
INFO Sending to Gemini (~84,000 tokens)…
INFO Gemini responded (4,210 chars)
INFO Wrote report: ./reports/review-foo-main-20260417-143022.md
```

On completion, print the file path to **stdout** on the last line so scripts can capture it: `echo $(python code-reviewer.py …)`.

## Final check: exit code 0 only if a report was written OR the period was empty.

---

## Acceptance

End-to-end against a real `ridwanspace/*` repo:
- [ ] `--timeperiod 7d` on an active branch → writes a file, exits 0.
- [ ] `--timeperiod 2026-04-01:2026-04-17` → writes a file with the correct resolved period string in the header.
- [ ] `--timeperiod 5` → writes a file with exactly 5 commits (or fewer if branch has fewer).
- [ ] Empty window → prints "No commits found", exits 0, no file written.
- [ ] Bogus branch → exits 3.
- [ ] Bogus repo → exits 3.
- [ ] Bogus `--timeperiod` → exits 2.
- [ ] With `GITHUB_API_KEY` unset → exits 1.

## Risks / gotchas

- Don't swallow `KeyboardInterrupt` in the broad `except Exception` — let it propagate.
- `sys.exit` codes must be the **exact** numbers from PRD §9 — downstream CI callers may grep them.
- Serial diff fetching is fine for v1 at the 200-commit cap (~30s typical). Don't pre-optimize with threads.
