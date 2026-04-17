# PRD: `code-reviewer.py` — Automated Enterprise Code Review Agent

**Version:** 1.0
**Status:** Draft — implementation-ready
**Owner:** `ridwanspace`

---

## 1. Overview

### 1.1 Purpose
`code-reviewer.py` is a single-command CLI tool that performs **enterprise-grade automated code review** on commits pushed to any repository owned by `ridwanspace`. It fetches commits from GitHub over a user-specified time window, aggregates their code-only diffs, enriches them with lightweight codebase context (stack manifests + README), and sends the payload to Google Gemini (`gemini-3.1-flash-lite-preview`) with a hardened reviewer system prompt. The model returns a structured, severity-grouped review which is persisted as a timestamped Markdown report.

### 1.2 Problem being solved
Manual code review is inconsistent, slow, and easily skipped on staging branches or solo-dev repositories. Commercial products (CodeRabbit, Reviewpad, Graphite) are tied to PR workflows and require org-level installation. This tool:

- Works on **direct pushes** and arbitrary time windows — not just PRs.
- Runs **locally** — no third-party GitHub App install, no data leaving outside GitHub + Gemini.
- Produces a **portable Markdown artifact** that can be committed, emailed, pasted into Slack, or fed to CI.
- Is **cheap and fast** — single Gemini call per run, Flash-Lite tier.

### 1.3 Target user
Solo developers, tech leads, and small engineering teams who:
- Own repositories under a single GitHub user/org.
- Want a regular "sanity pass" over staging before promoting to production.
- Need audit trails of review feedback without setting up a bot.

### 1.4 Non-goals
- Not a PR-gating bot (see §12 Future Enhancements).
- Not a multi-tenant SaaS.
- Not a replacement for human review on high-risk changes.
- Not a static analyzer — it uses an LLM, not AST rules.

---

## 2. Tech Stack

| Component | Version | Purpose |
|---|---|---|
| Python | `>=3.10,<3.13` | f-strings, `match`, structural typing |
| `requests` | `>=2.31` | GitHub REST calls |
| `python-dotenv` | `>=1.0` | Load `.env` |
| `google-generativeai` | `>=0.8` | Gemini SDK |
| `argparse` | stdlib | CLI parsing |
| `python-dateutil` | `>=2.8` | Robust date parsing for `YYYY-MM-DD:YYYY-MM-DD` ranges |

**Install:**
```bash
pip install requests python-dotenv google-generativeai python-dateutil
```

A `requirements.txt` must be generated alongside `code-reviewer.py`.

---

## 3. Architecture

### 3.1 Module breakdown

```
code-commit-reviewer/
├── code-reviewer.py        # Entrypoint: CLI parsing, orchestration
├── github_client.py        # All GitHub REST API interaction
├── gemini_client.py        # Gemini SDK wrapper + prompt assembly
├── diff_filter.py          # File extension whitelist + size limits
├── report_writer.py        # Markdown rendering & file output
├── requirements.txt
├── .env                    # GITHUB_API_KEY, GEMINI_API_KEY (already present)
└── PRD.md
```

### 3.2 Module responsibilities

**`code-reviewer.py`** (entrypoint, ~80 LOC)
- Parse CLI args.
- Load `.env`.
- Resolve time period → (since_date, until_date) or (n_commits).
- Orchestrate: `github_client` → `diff_filter` → `gemini_client` → `report_writer`.
- Handle top-level exceptions, print user-friendly errors, set exit codes.

**`github_client.py`**
- `GithubClient(token: str)` class.
- `list_commits(repo, branch, since=None, until=None, max_count=None) -> list[Commit]`
- `get_commit_diff(repo, sha) -> list[FileDiff]`
- `get_file_content(repo, path, ref) -> str | None` (for context files)
- Handles pagination via `Link` header (`rel="next"`).
- Surfaces rate-limit state (`X-RateLimit-Remaining`, `X-RateLimit-Reset`).
- Uses `Accept: application/vnd.github+json` and `X-GitHub-Api-Version: 2022-11-28`.
- Retries on `429`/`5xx` with exponential backoff (max 3 retries).

**`gemini_client.py`**
- `GeminiReviewer(api_key: str, model: str)` class.
- `build_prompt(diffs, context, metadata) -> str`
- `review(prompt: str) -> str` — single `generate_content` call, returns raw Markdown.
- Contains the **full system prompt** as a module-level constant (see §7.3).
- Token estimation helper (`~4 chars ≈ 1 token`) to warn before exceeding budget.

**`diff_filter.py`**
- `CODE_EXTENSIONS` constant (whitelist).
- `EXCLUDED_FILENAMES` constant (`package-lock.json`, `yarn.lock`, `poetry.lock`, `Pipfile.lock`, etc.).
- `is_reviewable(file_path: str) -> bool`
- `trim_large_diff(file_diff, max_lines=500) -> FileDiff` — replaces patch body with `<SKIPPED: {n} lines changed, exceeds 500-line threshold>` placeholder when oversized.

**`report_writer.py`**
- `write_report(output_dir, reponame, branch, metadata, llm_output) -> Path`
- Builds the file name per §8.1.
- Prepends the header block per §8.2.
- Appends the Commit Breakdown section from `metadata.commits` (not from the LLM — deterministic).
- Writes UTF-8, LF line endings.

### 3.3 Data flow

```
CLI args
   │
   ▼
resolve_period()  ──►  (since, until) | (n_commits)
   │
   ▼
GithubClient.list_commits  ──►  [Commit, ...]
   │
   ▼  (for each commit, parallelizable but serial is fine for v1)
GithubClient.get_commit_diff  ──►  [FileDiff, ...]
   │
   ▼
diff_filter.filter_and_trim  ──►  reviewable diffs only
   │
   ▼
GithubClient.get_file_content × {README, package.json, pyproject.toml, ...}
   │
   ▼
gemini_client.build_prompt(diffs, context, metadata)
   │
   ▼
gemini_client.review  ──►  Markdown review body
   │
   ▼
report_writer.write_report  ──►  review-<repo>-<branch>-<ts>.md
```

---

## 4. CLI Specification

### 4.1 Invocation
```bash
python code-reviewer.py --reponame <name> --branch <branch> --timeperiod <period>
```

### 4.2 Arguments

| Flag | Type | Required | Default | Validation |
|---|---|---|---|---|
| `--reponame` | `str` | yes | — | Non-empty; matches `^[A-Za-z0-9._-]+$`. Owner is hardcoded to `ridwanspace`. |
| `--branch` | `str` | yes | — | Non-empty. Existence verified via GitHub API before fetching commits. |
| `--timeperiod` | `str` | yes | — | Matches one of the three formats below. |
| `--output-dir` | `str` | no | `./reports` | Directory is created if missing. |
| `--max-context-bytes` | `int` | no | `40000` | Upper bound for codebase-context block sent to Gemini. |
| `--verbose` / `-v` | flag | no | `False` | Enable DEBUG logging. |

### 4.3 `--timeperiod` formats

The value is parsed in this order; first match wins:

1. **Plain integer** (e.g., `10`) → last N commits on branch.
   - Regex: `^\d+$`
   - Bounds: `1 <= N <= 200`. Reject out-of-range with clear error.
2. **Relative days** (e.g., `7d`, `30d`) → commits in last N days (UTC, `now - Nd` to `now`).
   - Regex: `^(\d+)d$`
   - Bounds: `1 <= N <= 365`.
3. **Explicit range** (e.g., `2026-04-01:2026-04-17`) → commits whose `committer.date` is inside `[start 00:00Z, end 23:59:59Z]`.
   - Regex: `^\d{4}-\d{2}-\d{2}:\d{4}-\d{2}-\d{2}$`
   - Parsed via `dateutil.parser.isoparse`.
   - `start <= end` required; otherwise error.

Any other input → exit code `2` with message:
`ERROR: --timeperiod must be one of: '10' (last N commits), '7d' (last N days), or '2026-04-01:2026-04-17' (date range).`

---

## 5. GitHub API Flow

### 5.1 Base URL
`https://api.github.com` — standard REST v3.

### 5.2 Auth
All requests use header:
```
Authorization: Bearer <GITHUB_API_KEY>
```
`GITHUB_API_KEY` is a fine-grained PAT scoped to all `ridwanspace/*` repos with **Contents: Read** and **Pull requests: Read**.

### 5.3 Endpoints used

| Step | Endpoint | Purpose |
|---|---|---|
| Branch sanity check | `GET /repos/{owner}/{repo}/branches/{branch}` | Verify branch exists; fail fast with a clear error. |
| List commits | `GET /repos/{owner}/{repo}/commits?sha={branch}&since=...&until=...&per_page=100` | Primary commit list. For date-based periods, use `since`/`until`. For count-based, fetch pages until `N` collected. |
| Get commit detail | `GET /repos/{owner}/{repo}/commits/{sha}` | Returns `files[]` with `filename`, `status`, `additions`, `deletions`, `changes`, `patch`. |
| Get file (context) | `GET /repos/{owner}/{repo}/contents/{path}?ref={branch}` | Returns base64-encoded content. Decoded into UTF-8. |

### 5.4 Pagination
- Use `per_page=100`.
- Follow `Link: <...>; rel="next"` until absent or desired count reached.
- Hard cap: **200 commits per run** to bound Gemini token usage.

### 5.5 Rate limit awareness
Before each request, if `X-RateLimit-Remaining == 0`:
- Sleep until `X-RateLimit-Reset` (plus 2s jitter) if wait < 60s.
- Otherwise abort with: `ERROR: GitHub rate limit exhausted; resets at <ISO timestamp>.`

On `403` with body containing `rate limit`, same behavior.

On `5xx`, retry up to 3× with exponential backoff (`1s, 2s, 4s`).

### 5.6 Context files

After commits are fetched, attempt to load the following from `{branch}` (silently skip 404s):

- `README.md` (truncate to first 3000 chars)
- `package.json`
- `pyproject.toml`
- `go.mod`
- `Cargo.toml`
- `Gemfile`
- `composer.json`
- `tsconfig.json`
- `next.config.js` / `next.config.mjs` / `next.config.ts`

Concatenate into a single "Codebase Context" block, capped at `--max-context-bytes` (default 40 KB). If overflow, truncate and append `… [truncated]`.

---

## 6. Diff Filtering Logic

### 6.1 Extension whitelist
```python
CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx",
    ".py", ".sql", ".go", ".java",
    ".rb", ".php", ".swift", ".kt", ".rs",
    ".css", ".scss", ".html", ".vue",
    ".json", ".yaml", ".yml", ".toml",
    ".env.example",
}
```

### 6.2 Explicit exclusions
```python
EXCLUDED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Gemfile.lock",
    "composer.lock", "Cargo.lock", "go.sum",
}
EXCLUDED_EXTENSIONS = {
    ".md", ".mdx", ".txt", ".lock", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".min.js", ".min.css",
}
```

### 6.3 `.json` special case
A `.json` file is reviewable **only if** its basename is not in `EXCLUDED_FILENAMES`. Lockfiles and generated configs are dropped.

### 6.4 Size limits
- Per-file patch cap: **500 changed lines** (`additions + deletions`). Oversized patches are replaced with:
  `<SKIPPED: {filename} — {n} lines changed, exceeds 500-line threshold; review manually>`
- Aggregate payload soft limit: **250 KB** of patch text. If exceeded, drop lowest-signal files first (in order: `.json`, `.yaml`/`.yml`, `.toml`, `.html`, `.css`/`.scss`, then language sources in reverse commit-recency).

### 6.5 Binary / renamed / removed files
- `status == "removed"` → include filename + deletion note, no patch body.
- `status == "renamed"` → note `previous_filename → filename`, include patch if within size.
- Binary files (no `patch` field in response) → skip with note in Commit Breakdown.

---

## 7. Gemini Integration

### 7.1 Model
- **ID:** `gemini-3.1-flash-lite-preview`
- **Chosen for:** low cost, large context window, structured Markdown output reliability.

### 7.2 Token budget
- Target prompt size: **≤ 500 KB** total (system + context + diffs).
- Response: `max_output_tokens` = 8192 (sufficient for a long structured review).
- Temperature: `0.2` (favor determinism and accuracy over creativity).
- `top_p`: `0.9`.

Pre-flight check: estimate `len(prompt) / 4` tokens; if `> 800_000`, abort with:
`ERROR: Prompt exceeds safe token budget. Narrow the time period.`

### 7.3 System prompt (verbatim, baked into `gemini_client.py`)

```
You are a senior staff engineer performing an enterprise-grade code review.
You are rigorous, specific, and actionable — never vague. You cite file names
and line ranges from the diffs provided. You do NOT invent code that is not in
the diff. If you are uncertain, you say so explicitly.

Your review MUST cover, in this order, and MUST use these exact section headers:

1. Summary — 2–3 sentence executive summary of what changed and overall health.
   End the Summary with a single line: "Overall Quality Score: X/10" where X is
   an integer from 1 to 10. The score reflects: correctness, security, design,
   maintainability, and test coverage — weighted equally.

2. Critical Issues — bugs that cause incorrect behavior, data loss, security
   vulnerabilities (injection, auth bypass, exposed secrets, CSRF, SSRF,
   insecure deserialization, IDOR, missing authorization), or regressions.
   MUST include file:line reference and a concrete fix.

3. Major Issues — logic errors under specific conditions, type unsafety, missing
   error handling, N+1 queries, blocking I/O on hot paths, race conditions,
   incorrect API contracts, inconsistent status codes, response-shape drift.

4. Minor Issues — readability, naming, small inefficiencies, inconsistent
   formatting patterns, dead code, minor duplication.

5. Suggestions — refactors, abstractions, architectural improvements. Optional.

6. What Was Done Well — minimum 3 concrete positive observations, each tied to
   a specific file or change. Do not invent praise.

Every issue entry MUST follow this format:

   - **[Severity] <one-line headline>**
     - File: `path/to/file.ext:L<start>-L<end>` (or single line)
     - Problem: <what is wrong and why it matters>
     - Fix: <concrete code-level recommendation, snippet if short>

Additional rules:
- Reference ONLY files that appear in the provided diffs.
- If a concern is speculative (depends on code not shown), prefix it with
  "Speculative:" and explain what you'd need to verify.
- Do NOT produce an Executive Summary section, Conclusion, or sign-off —
  just the six sections above, in order.
- Use GitHub-flavored Markdown. Code in fenced blocks with language tags.
- Do NOT wrap your entire response in a code block.
```

### 7.4 User prompt template

```
# Repository
ridwanspace/{reponame} @ {branch}

# Period
{resolved_period}  ({commit_count} commits)

# Codebase Context
{context_block}

# Aggregated Diffs
{diff_block}
```

Where `diff_block` is each commit rendered as:

```
## Commit {sha_short} — {message_first_line}
Author: {author}
Date: {iso_date}

### {filename} ({status}, +{additions} -{deletions})
```diff
{patch}
```
(repeat per file)
```

### 7.5 SDK call

```python
import google.generativeai as genai

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="gemini-3.1-flash-lite-preview",
    system_instruction=SYSTEM_PROMPT,
    generation_config={
        "temperature": 0.2,
        "top_p": 0.9,
        "max_output_tokens": 8192,
    },
)
response = model.generate_content(user_prompt)
return response.text
```

---

## 8. Output Specification

### 8.1 File naming
```
review-<reponame>-<branch>-<YYYYMMDD-HHMMSS>.md
```
- `<branch>` has `/` replaced with `_` (e.g., `feature/x` → `feature_x`).
- Timestamp is UTC, generated at write time.
- Written to `--output-dir` (default `./reports`).

### 8.2 Report structure

The final file is assembled as:

```markdown
# Code Review Report
**Repo:** ridwanspace/<reponame>
**Branch:** <branch>
**Period:** <resolved period string>
**Commits reviewed:** <count>
**Generated:** <UTC datetime, ISO 8601>
**Model:** gemini-3.1-flash-lite-preview

---

<LLM output: Sections 1–6 per §7.3>

---

## Commit Breakdown

| SHA | Author | Date (UTC) | Message | Files | +/- |
|---|---|---|---|---|---|
| `abc1234` | name | 2026-04-17T10:22:00Z | Add auth guard | 3 | +120/-14 |
| …
```

The Commit Breakdown table is rendered **by the script** from GitHub data, not by the LLM — this guarantees accurate SHAs and counts.

### 8.3 Resolved period string
- `10d` → `Last 10 days (2026-04-07T00:00:00Z → 2026-04-17T23:59:59Z)`
- `2026-04-01:2026-04-17` → `2026-04-01 → 2026-04-17`
- `10` → `Last 10 commits on branch`

---

## 9. Error Handling

| Scenario | Exit code | User-facing message |
|---|---|---|
| Missing `.env` key | `1` | `ERROR: GITHUB_API_KEY or GEMINI_API_KEY missing from .env` |
| Invalid `--timeperiod` | `2` | See §4.3 |
| Repo not found / 404 | `3` | `ERROR: Repository 'ridwanspace/<name>' not found or no access.` |
| Branch not found | `3` | `ERROR: Branch '<branch>' not found on 'ridwanspace/<name>'.` |
| No commits in period | `0` | `No commits found in the specified period. No report generated.` (soft exit) |
| GitHub rate limit | `4` | `ERROR: GitHub rate limit exhausted; resets at <ISO>.` |
| Gemini API failure | `5` | `ERROR: Gemini call failed: <reason>. See --verbose for details.` |
| Token budget exceeded | `6` | `ERROR: Prompt exceeds safe token budget. Narrow the time period.` |
| Write failure | `7` | `ERROR: Could not write report to <path>: <reason>` |
| Unexpected exception | `99` | Stack trace if `--verbose`, short message otherwise. |

All errors go to `stderr`. Info/progress logs go to `stdout`.

---

## 10. Example Usage

### Example 1 — Last 7 days on staging
```bash
python code-reviewer.py \
  --reponame restaurant-ops-management \
  --branch staging \
  --timeperiod 7d
```
**Output:** `./reports/review-restaurant-ops-management-staging-20260417-143022.md`

### Example 2 — Explicit date range on main
```bash
python code-reviewer.py \
  --reponame restaurant-ops-management \
  --branch main \
  --timeperiod 2026-04-01:2026-04-17
```
**Output:** `./reports/review-restaurant-ops-management-main-20260417-143510.md`

### Example 3 — Last 15 commits on a feature branch
```bash
python code-reviewer.py \
  --reponame billing-service \
  --branch feature/stripe-migration \
  --timeperiod 15 \
  --output-dir ~/code-reviews
```
**Output:** `~/code-reviews/review-billing-service-feature_stripe-migration-20260417-144112.md`

---

## 11. Acceptance Criteria

A working implementation must satisfy **all** of the following:

- [ ] Runs end-to-end against a real `ridwanspace/*` repo using existing `.env` keys.
- [ ] Produces a Markdown file at the expected path for all three `--timeperiod` formats.
- [ ] Filters out lockfiles, docs, and binaries (verified with a repo that contains them).
- [ ] Handles an empty commit window gracefully (exit 0, no crash, no file written).
- [ ] Handles a non-existent branch with exit 3 and a clear message.
- [ ] Handles GitHub rate limiting without crashing (sleep-and-retry within 60s window).
- [ ] Gemini prompt includes codebase context and is under the token budget for typical 7-day windows on a mid-size repo.
- [ ] Report contains all six LLM sections in order plus the deterministic Commit Breakdown table.
- [ ] `--verbose` surfaces the full request/response timing and token estimates.

---

## 12. Future Enhancements

1. **PR review mode** — `--pr <number>` to review a single PR's diff instead of a commit window. Post the resulting Markdown as a PR comment.
2. **Slack / email delivery** — `--notify slack:<webhook>` / `--notify email:<addr>` for push-on-completion.
3. **Multi-repo batch mode** — `--repos repo-a,repo-b,repo-c` or reading from a YAML config; produces one report per repo, plus an aggregate index.
4. **CI/CD quality gate** — `--fail-on critical` exit non-zero if the LLM reports any Critical issues; pluggable into GitHub Actions.
5. **Scheduled runs** — GitHub Action cron that runs weekly, commits the report to a `reviews/` branch.
6. **Incremental mode** — persist the last reviewed SHA per `(repo, branch)` in a state file and review only what's new.
7. **Streaming output** — print review as Gemini streams it; write atomically on completion.
8. **Additional providers** — pluggable backend (Claude, OpenAI, local Ollama) behind a common `Reviewer` interface.
9. **Inline suggestions** — parse the LLM output and, when run in PR mode, post suggestions as GitHub review comments anchored to specific lines.
10. **Cost tracking** — log estimated tokens & $ per run to `./reports/.usage.jsonl`.
