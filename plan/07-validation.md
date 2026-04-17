# Plan 07 — Validation against Acceptance Criteria

**Goal:** Walk every PRD §11 acceptance item against a real repo and a forced-failure repo. Capture outputs.

**PRD refs:** §10 (examples), §11 (acceptance).

---

## Test repos

Use any two `ridwanspace/*` repos with:
- **Repo A:** active commit history (≥ 10 commits in the last 7 days preferred). Call this `<ACTIVE_REPO>` below.
- **Repo B:** quiet repo or use a date range known to have no commits. Call this `<QUIET_REPO>`.

---

## Scenarios

### S1 — Last N days
```bash
python code-reviewer.py --reponame <ACTIVE_REPO> --branch main --timeperiod 7d
```
- ✅ Exits 0.
- ✅ File exists at `./reports/review-<repo>-main-*.md`.
- ✅ Header shows `Period: Last 7 days (…)`.
- ✅ LLM body contains all 6 section headers (may warn but should still render).
- ✅ Commit Breakdown table non-empty.

### S2 — Explicit date range
```bash
python code-reviewer.py --reponame <ACTIVE_REPO> --branch main \
    --timeperiod 2026-04-01:2026-04-17
```
- ✅ Same as S1, with header `Period: 2026-04-01 → 2026-04-17`.

### S3 — Last N commits
```bash
python code-reviewer.py --reponame <ACTIVE_REPO> --branch main --timeperiod 10
```
- ✅ Exits 0.
- ✅ Commit Breakdown table has ≤ 10 rows.
- ✅ Header shows `Period: Last 10 commits on branch`.

### S4 — Branch with slash (filename sanitization)
```bash
python code-reviewer.py --reponame <ACTIVE_REPO> --branch feature/any-slash --timeperiod 5
```
Requires such a branch to exist. If none, create one and push a trivial commit.
- ✅ Filename contains `feature_any-slash`, not `feature/any-slash`.

### S5 — Empty window
```bash
python code-reviewer.py --reponame <QUIET_REPO> --branch main \
    --timeperiod 1999-01-01:1999-01-02
```
- ✅ Exits 0.
- ✅ `No commits found …` on stdout.
- ✅ No file created.

### S6 — Non-existent branch
```bash
python code-reviewer.py --reponame <ACTIVE_REPO> --branch definitely-not-a-branch \
    --timeperiod 7d
```
- ✅ Exits 3.
- ✅ stderr contains `Branch 'definitely-not-a-branch' not found`.

### S7 — Non-existent repo
```bash
python code-reviewer.py --reponame definitely-not-a-repo --branch main --timeperiod 7d
```
- ✅ Exits 3.

### S8 — Invalid period
```bash
python code-reviewer.py --reponame <ACTIVE_REPO> --branch main --timeperiod abc
python code-reviewer.py --reponame <ACTIVE_REPO> --branch main --timeperiod 500    # out of range
python code-reviewer.py --reponame <ACTIVE_REPO> --branch main \
    --timeperiod 2026-04-17:2026-04-01                                              # reversed
```
- ✅ All three exit 2 with a clear message.

### S9 — Missing env key
```bash
env -u GITHUB_API_KEY python code-reviewer.py --reponame <ACTIVE_REPO> \
    --branch main --timeperiod 7d
```
- ✅ Exits 1.

### S10 — Diff filtering
Pick a commit range known to include a `package-lock.json` change plus a source file.
- ✅ Source file appears in the Gemini prompt (check `--verbose` output).
- ✅ `package-lock.json` does NOT appear in the prompt.
- ✅ `README.md` changes are excluded from the diff block but README content still appears in the Codebase Context.

### S11 — Oversized file
Create (or find) a commit with a >500-line change.
- ✅ That file's patch in the prompt is replaced with the SKIPPED placeholder.
- ✅ File still appears in the Commit Breakdown table with its real line counts.

### S12 — Rate limit
Run S1 in a tight loop (or set GitHub token to a nearly-exhausted one).
- ✅ At 0 remaining and reset < 60s: run sleeps, then completes.
- ✅ At 0 remaining and reset > 60s: exits 4.

### S13 — Verbose mode
```bash
python code-reviewer.py --reponame <ACTIVE_REPO> --branch main --timeperiod 7d -v
```
- ✅ Shows per-request HTTP timing, rate-remaining, token estimate.

---

## Post-validation deliverables

- [ ] Attach 1 sample report (pick any S1/S2/S3) to the PR description.
- [ ] Update README (if introduced) with the three PRD §10 examples verbatim.
- [ ] Log any deviations from PRD in a `KNOWN-ISSUES.md` and link from the index.

## What counts as "done"

All of S1–S13 pass as specified. Failures in S10/S11 (edge cases) may be acceptable as a follow-up if the core S1/S2/S3 path is solid — call that out in the PR rather than silently skipping.
