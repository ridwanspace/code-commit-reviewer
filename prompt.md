Create a PRD.md for a Python script called `code-reviewer.py` that acts as an 
enterprise-grade automated code review agent, similar to CodeRabbit or Reviewpad.

---

## Context & Resources

The project already has a `.env` file with:
- `GITHUB_API_KEY` — a fine-grained GitHub PAT with access to all repositories 
  under the owner `ridwanspace`, with Contents: Read and Pull requests: Read permissions.
- `GEMINI_API_KEY` — a Gemini API key with access to model `gemini-3.1-flash-lite-preview`.

Both keys are confirmed working. The script must load them from `.env` using `python-dotenv`.

---

## CLI Interface

The script is invoked as:

  python code-reviewer.py --reponame <name> --branch <branch> --timeperiod <period>

Arguments:
- `--reponame`: Repository name only (e.g., `restaurant-ops-management`). 
  Owner is always hardcoded as `ridwanspace`.
- `--branch`: Branch name to review (e.g., `staging`, `main`).
- `--timeperiod`: Supports 3 formats:
    - Last N days:     `7d` or `30d`
    - Date range:      `2026-04-01:2026-04-17`
    - Last N commits:  `10` (plain integer)

---

## What the Script Does

1. **Fetch commits** from the GitHub API for the given repo, branch, and time period.
2. **Filter commits** to only those within the resolved time range or count.
3. **Fetch diffs** for each commit, but filter out non-code files. Only include 
   files with extensions: `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.sql`, `.go`, 
   `.java`, `.rb`, `.php`, `.swift`, `.kt`, `.rs`, `.css`, `.scss`, `.html`, 
   `.vue`, `.json` (config only, not lockfiles), `.yaml`, `.yml`, `.toml`, `.env.example`.
   Exclude: `.md`, `.mdx`, `.txt`, `.lock`, `.log`, `package-lock.json`, `yarn.lock`.
4. **Fetch codebase context**: Read key files from the repo to inform the review —
   specifically look for `README.md`, `package.json`, `pyproject.toml`, or any 
   top-level config that describes the stack and architecture.
5. **Aggregate all diffs** from the time period into a single payload.
6. **Send to Gemini** (`gemini-3.1-flash-lite-preview`) with an enterprise-grade 
   system prompt (see below) and the aggregated diff + codebase context.
7. **Write output** to a `.md` file named:
   `review-<reponame>-<branch>-<timestamp>.md`

---

## Gemini System Prompt (Enterprise Code Reviewer)

The system prompt sent to Gemini must instruct it to behave like an enterprise 
code review agent. It should:

- Identify **bugs, logic errors, and regressions**
- Flag **security vulnerabilities** (injection, auth bypass, exposed secrets, 
  insecure defaults)
- Review **type safety and null handling**
- Assess **code structure and separation of concerns**
- Check **naming consistency and readability**
- Identify **duplicate code or missed abstractions**
- Flag **performance concerns** (N+1 queries, unnecessary re-renders, blocking ops)
- Review **error handling completeness**
- Assess **API design** (REST consistency, response shapes, status codes)
- Flag **missing or incorrect authorization checks**
- Provide an **overall quality score** out of 10
- Output must be **structured, actionable, and grouped by severity**: 
  Critical > Major > Minor > Suggestions

The reviewer must reference specific **file names and line-level context** 
from the diff when giving feedback.

---

## Output Format (.md file)

The output `.md` file must follow this structure:

# Code Review Report
**Repo:** ridwanspace/<reponame>  
**Branch:** <branch>  
**Period:** <resolved date range or commit range>  
**Commits reviewed:** <count>  
**Generated:** <datetime>  
**Model:** gemini-3.1-flash-lite-preview  

---

## Summary
<2-3 sentence executive summary of the changes and overall quality>

**Overall Quality Score: X/10**

---

## Critical Issues
<bugs, security holes, auth bypasses — must fix before merge>

## Major Issues
<logic errors, type unsafety, missing error handling — should fix>

## Minor Issues
<readability, naming, small inefficiencies — nice to fix>

## Suggestions
<refactoring ideas, abstractions, improvements — optional>

## What Was Done Well
<positive observations — at least 3 points>

---

## Commit Breakdown
<for each commit: SHA, message, files changed, one-line summary of what it does>

---

## PRD Requirements

The PRD.md must include:

1. **Overview** — purpose, problem being solved, target user
2. **Tech stack** — Python 3.10+, `requests`, `python-dotenv`, `google-generativeai`, 
   `argparse`, `python-dateutil`
3. **Architecture** — module breakdown: `github_client.py`, `gemini_client.py`, 
   `diff_filter.py`, `report_writer.py`, `code-reviewer.py` (entrypoint)
4. **CLI spec** — full argument definitions with types, defaults, validation rules
5. **GitHub API flow** — endpoints used, pagination handling, rate limit awareness
6. **Diff filtering logic** — file extension whitelist, size limits per diff 
   (skip files with >500 lines changed to avoid token overflow)
7. **Gemini integration** — model, token budget considerations, prompt structure, 
   how codebase context is prepended
8. **Output spec** — file naming, directory, markdown structure
9. **Error handling** — invalid repo, branch not found, no commits in period, 
   API rate limits, token limit exceeded
10. **Example usage** — at least 3 CLI examples with expected output filenames
11. **Future enhancements** — PR review mode, Slack/email delivery, multi-repo 
    batch mode, severity thresholds for CI/CD gate

Write the PRD.md to the current working directory. Be specific, detailed, and 
implementation-ready — a developer should be able to build the full script 
from the PRD alone without ambiguity.
