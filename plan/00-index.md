# Dev Plan Index — `code-reviewer.py`

Source: [`PRD.md`](../PRD.md)

Plans are sequenced. Each one is independently implementable and testable once its predecessors are done.

| # | File | Deliverable | Depends on |
|---|---|---|---|
| 01 | [01-scaffolding-and-cli.md](./01-scaffolding-and-cli.md) | Project skeleton, `.env` loader, CLI parser, period resolver | — |
| 02 | [02-github-client.md](./02-github-client.md) | `github_client.py` — commits, diffs, context files, pagination, rate limits | 01 |
| 03 | [03-diff-filter.md](./03-diff-filter.md) | `diff_filter.py` — extension whitelist, size caps, aggregate trimming | 02 |
| 04 | [04-gemini-client.md](./04-gemini-client.md) | `gemini_client.py` — system prompt, user prompt builder, SDK call | 03 |
| 05 | [05-report-writer.md](./05-report-writer.md) | `report_writer.py` — markdown assembly, commit breakdown table, file naming | 04 |
| 06 | [06-orchestration.md](./06-orchestration.md) | `code-reviewer.py` entrypoint — wiring, error mapping, exit codes | 01–05 |
| 07 | [07-validation.md](./07-validation.md) | Acceptance-criteria walkthrough against a real repo | 06 |

## Suggested working order

1. Ship 01 end-to-end (args parse, no-op run) — verifies environment.
2. Build 02 in isolation with a quick REPL script — confirm GitHub auth & pagination.
3. Build 03 with unit-style sanity checks on canned diffs.
4. Build 04 using a fixture from step 02 — first real Gemini call.
5. Build 05 — produces a valid Markdown file from fixture data.
6. Wire 06 to glue the modules; exit-code matrix from PRD §9.
7. Run 07 against 2–3 real repos to validate all three `--timeperiod` formats.

## Non-goals for v1 (defer per PRD §12)

- PR mode, Slack/email delivery, multi-repo, CI gate, scheduling, streaming, alt providers.
- Parallel commit fetching — serial is fine at the 200-commit cap.
- Unit test framework — manual/fixture validation is acceptable for v1. Add pytest in a follow-up.
