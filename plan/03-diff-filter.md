# Plan 03 — `diff_filter.py`

**Goal:** Decide which files are reviewable, trim oversized patches, and keep the aggregate payload under the payload soft-limit before it reaches Gemini.

**PRD refs:** §3.2 (`diff_filter`), §6, §7.2.

---

## Tasks

### 1. Constants

```python
CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx",
    ".py", ".sql", ".go", ".java",
    ".rb", ".php", ".swift", ".kt", ".rs",
    ".css", ".scss", ".html", ".vue",
    ".json", ".yaml", ".yml", ".toml",
}
SPECIAL_FILENAMES = {".env.example"}  # whole-filename match

EXCLUDED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Gemfile.lock",
    "composer.lock", "Cargo.lock", "go.sum",
}
EXCLUDED_EXTENSIONS = {
    ".md", ".mdx", ".txt", ".lock", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
}
EXCLUDED_SUFFIXES = {".min.js", ".min.css"}  # compound suffixes

PER_FILE_CHANGE_CAP = 500
AGGREGATE_SOFT_LIMIT_BYTES = 250_000

# Priority for dropping under aggregate pressure (highest = drop first)
DROP_PRIORITY_EXT = [".json", ".yaml", ".yml", ".toml", ".html", ".css", ".scss"]
```

### 2. `is_reviewable(filename: str) -> bool`

Rules (in order):
1. Basename in `EXCLUDED_FILENAMES` → `False`.
2. Any suffix in `EXCLUDED_SUFFIXES` matches → `False`.
3. Suffix in `EXCLUDED_EXTENSIONS` → `False`.
4. Basename in `SPECIAL_FILENAMES` → `True`.
5. Suffix in `CODE_EXTENSIONS` → `True`.
6. Otherwise → `False`.

Use `os.path.splitext` for suffix, `os.path.basename` for filename. Lowercase both for comparison.

### 3. `trim_large_diff(fd: FileDiff) -> FileDiff`

If `fd.patch is None` → return as-is (binary/removed).

If `fd.additions + fd.deletions > PER_FILE_CHANGE_CAP`:
- Set `patch = f"<SKIPPED: {fd.filename} — {fd.additions + fd.deletions} lines changed, exceeds {PER_FILE_CHANGE_CAP}-line threshold; review manually>"`
- Keep other fields intact so the commit-breakdown table is still accurate.

Return a new `FileDiff` (don't mutate — makes testing easier).

### 4. `filter_commit_files(files: list[FileDiff]) -> list[FileDiff]`

- Apply `is_reviewable` to filename (and `previous_filename` if present — include if either is reviewable).
- For `renamed` status, prepend a one-line note to the patch: `# renamed from: {previous_filename}\n`.
- Return filtered list with `trim_large_diff` applied.

### 5. `enforce_aggregate_budget(commits: list[tuple[Commit, list[FileDiff]]]) -> list[tuple[Commit, list[FileDiff]]]`

Walk commits and sum `len(patch or "")` across all files. If total ≤ `AGGREGATE_SOFT_LIMIT_BYTES`, return as-is.

Otherwise drop files until under budget, using this order:
1. Within the **oldest** commits first (reverse chronological is PRESERVED, so we drop from the tail of the list — oldest commits assumed last per GitHub default sort).
2. Within each commit, drop by `DROP_PRIORITY_EXT` order (json first, then yaml, …).
3. Finally, by language files in the same oldest-first order.

Log INFO each time a file is dropped with reason: `dropped foo.json (low-signal, aggregate budget)`.

If after all drops we're still over budget, return whatever's left and log a WARN — Gemini pre-flight in Plan 04 will decide whether to abort.

### 6. Helper: `total_patch_bytes(commits)` — exposed for logging and for the token-estimate pre-flight.

---

## Acceptance

- [ ] `is_reviewable("src/app.tsx")` → True.
- [ ] `is_reviewable("package-lock.json")` → False.
- [ ] `is_reviewable("docs/guide.md")` → False.
- [ ] `is_reviewable(".env.example")` → True.
- [ ] `is_reviewable("dist/bundle.min.js")` → False.
- [ ] A `FileDiff` with `additions=600, deletions=0` produces a SKIPPED patch.
- [ ] Budget enforcement drops `.json` before `.py` when both present.
- [ ] Function is pure — no I/O, no logging-dependent behavior.

## Risks / gotchas

- Multi-dot filenames (`foo.test.tsx`) — `os.path.splitext` only splits the last suffix, which is correct here.
- Case sensitivity: GitHub preserves case, but compare lowercased to be safe (`.JSON` shouldn't slip through).
- "Drop by commit recency" requires commits in a known order — document that the caller passes commits newest-first (GitHub default).
