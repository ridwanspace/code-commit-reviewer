# code-reviewer.py

Automated enterprise code review for `ridwanspace/*` GitHub repositories. Fetches commits over a time window, aggregates code-only diffs with lightweight codebase context, sends the payload to Google Gemini (`gemini-3.1-flash-lite-preview`), and writes a structured Markdown report.

- Runs on **direct pushes** and arbitrary time windows — not tied to PRs.
- Runs **locally** — no GitHub App install.
- Produces a **portable Markdown artifact**.
- **One** Gemini call per run (Flash-Lite tier) — cheap and fast.

See [`PRD.md`](./PRD.md) for the full spec; [`plan/`](./plan/) for the implementation plans.

## Install

Requires Python 3.10+.

```bash
pip install -r requirements.txt
```

## Configure

Create `.env` in the project root:

```
GITHUB_API_KEY=<fine-grained PAT with Contents:Read + Pull requests:Read for ridwanspace/*>
GEMINI_API_KEY=<your Gemini API key>
```

## Usage

```bash
python3 code-reviewer.py --reponame <REPO> --branch <BRANCH> --timeperiod <PERIOD>
```

Owner is hardcoded to `ridwanspace` — pass only the repo name.

### `--timeperiod` formats

| Format | Example | Meaning |
|---|---|---|
| Integer (1–200) | `10` | Last 10 commits on the branch |
| Days (1–365) | `7d` | Commits in the last 7 days (UTC) |
| Range | `2026-04-01:2026-04-17` | Commits between these dates (UTC, inclusive) |

### Optional flags

| Flag | Default | Purpose |
|---|---|---|
| `--output-dir <path>` | `./reports` | Where the Markdown report is written |
| `--max-context-bytes <int>` | `40000` | Cap on the codebase-context block sent to Gemini |
| `-v` / `--verbose` | off | DEBUG logs: HTTP timings, rate headers, token estimates |

## Examples

**Last 7 days on staging:**
```bash
python3 code-reviewer.py \
  --reponame restaurant-ops-management \
  --branch staging \
  --timeperiod 7d
```

**Explicit date range on main:**
```bash
python3 code-reviewer.py \
  --reponame restaurant-ops-management \
  --branch main \
  --timeperiod 2026-04-01:2026-04-17
```

**Last 15 commits on a feature branch, custom output directory:**
```bash
python3 code-reviewer.py \
  --reponame billing-service \
  --branch feature/stripe-migration \
  --timeperiod 15 \
  --output-dir ~/code-reviews
```

## Output

Report path format:
```
<output-dir>/review-<repo>-<branch>-<YYYYMMDD-HHMMSS>.md
```
Slashes in branch names are replaced with underscores (`feature/x` → `feature_x`). Timestamp is UTC.

On success, the report path is printed to stdout on the last line — machine-parseable:
```bash
REPORT=$(python3 code-reviewer.py --reponame foo --branch main --timeperiod 7d | tail -1)
cat "$REPORT"
```

Each report contains a header block, the LLM's six-section review (Summary with quality score, Critical / Major / Minor Issues, Suggestions, What Was Done Well), and a deterministic Commit Breakdown table rendered from GitHub data.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Report written, or no commits in window (soft exit) |
| `1` | Missing `GITHUB_API_KEY` / `GEMINI_API_KEY` |
| `2` | Invalid CLI argument (`--reponame`, `--branch`, or `--timeperiod`) |
| `3` | Repository or branch not found / no access |
| `4` | GitHub rate limit exhausted |
| `5` | Gemini API call failed |
| `6` | Prompt exceeds safe token budget — narrow the time period |
| `7` | Could not write report (filesystem error) |
| `99` | Unexpected error (use `--verbose` for traceback) |

## Architecture

```
code-reviewer.py     CLI, period resolver, orchestration, exit-code mapping
github_client.py     REST client — commits, diffs, context files, pagination, rate limits
diff_filter.py       Extension whitelist, per-file size caps, aggregate-budget trimming
gemini_client.py     System prompt, user prompt builder, single generate_content call
report_writer.py     Markdown header + LLM body + deterministic Commit Breakdown table
```

## Non-goals (v1)

PR mode, Slack/email delivery, multi-repo batch mode, CI gate, scheduling, streaming, alternative LLM providers. See PRD §12 for the deferred roadmap.
