# Plan 05 — `report_writer.py`

**Goal:** Emit a valid, well-formed Markdown report combining the LLM body with a deterministic Commit Breakdown table.

**PRD refs:** §3.2 (`report_writer`), §8, §9 (exit 7).

---

## Tasks

### 1. File naming — `build_output_path(output_dir, reponame, branch, ts) -> Path`

- Sanitize `branch`: `branch.replace("/", "_")`.
- Timestamp: `ts.strftime("%Y%m%d-%H%M%S")` (UTC).
- Filename: `review-{reponame}-{branch_safe}-{ts}.md`.
- Create `output_dir` with `Path(output_dir).mkdir(parents=True, exist_ok=True)`.
- Return full `Path`.

### 2. Header block

```python
def render_header(meta: ReportMeta) -> str:
    return textwrap.dedent(f"""\
        # Code Review Report
        **Repo:** ridwanspace/{meta.reponame}
        **Branch:** {meta.branch}
        **Period:** {meta.period_display}
        **Commits reviewed:** {meta.commit_count}
        **Generated:** {meta.generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}
        **Model:** gemini-3.1-flash-lite-preview

        ---

        """)
```

`ReportMeta` dataclass:
```python
@dataclass
class ReportMeta:
    reponame: str
    branch: str
    period_display: str
    commit_count: int
    generated_at: datetime
    commits: list[tuple[Commit, list[FileDiff]]]
```

### 3. Commit Breakdown table (deterministic, NOT from LLM)

```python
def render_commit_breakdown(commits) -> str:
    lines = [
        "## Commit Breakdown\n",
        "| SHA | Author | Date (UTC) | Message | Files | +/- |",
        "|---|---|---|---|---|---|",
    ]
    for commit, files in commits:
        sha7 = commit.sha[:7]
        msg = _escape_pipe(commit.message.split("\n", 1)[0])[:80]
        adds = sum(f.additions for f in files)
        dels = sum(f.deletions for f in files)
        lines.append(
            f"| `{sha7}` | {commit.author} | {commit.date:%Y-%m-%dT%H:%M:%SZ} | {msg} | {len(files)} | +{adds}/-{dels} |"
        )
    return "\n".join(lines) + "\n"
```

Helper:
```python
def _escape_pipe(s: str) -> str:
    return s.replace("|", "\\|")
```

### 4. Full assembly — `write_report(...)`

```python
def write_report(
    output_dir: str,
    meta: ReportMeta,
    llm_body: str,
) -> Path:
    path = build_output_path(output_dir, meta.reponame, meta.branch, meta.generated_at)
    parts = [
        render_header(meta),
        llm_body.strip(),
        "\n\n---\n\n",
        render_commit_breakdown(meta.commits),
    ]
    try:
        path.write_text("".join(parts), encoding="utf-8", newline="\n")
    except OSError as e:
        raise ReportWriteError(f"{path}: {e}") from e
    return path
```

`ReportWriteError` is a module-local exception — orchestration maps it to exit 7.

### 5. LLM body cleanup (light-touch)

Before appending to the report:
- Strip leading/trailing whitespace.
- If the model wrapped the whole response in a single fenced block, unwrap it (defensive; the system prompt forbids this but models slip sometimes). Detect via `body.startswith("```") and body.rstrip().endswith("```")`.

Do **not** attempt to restructure the body further. If sections are missing, the earlier warn in Plan 04 already informed the user.

### 6. Empty-window path

If `meta.commit_count == 0`, `write_report` should not be called at all — orchestration short-circuits. Document this in the module docstring.

---

## Acceptance

- [ ] Given a fixture of 3 commits and a canned LLM body, the written file:
  - [ ] Parses as valid Markdown (manual eyeball + `python -c "import markdown; markdown.markdown(...)"` if available).
  - [ ] Has all the `**Repo:**` / `**Branch:**` / etc. header lines.
  - [ ] Contains the LLM body unchanged (modulo trim/unwrap).
  - [ ] Ends with a populated Commit Breakdown table.
- [ ] Filename matches `review-{repo}-{branch}-{YYYYMMDD-HHMMSS}.md`.
- [ ] `feature/x` branch → filename contains `feature_x`.
- [ ] Unwritable `--output-dir` → raises `ReportWriteError` (orchestration → exit 7).

## Risks / gotchas

- Pipes (`|`) inside commit messages break Markdown tables — always `_escape_pipe` before rendering.
- Long commit messages — truncate to 80 chars for the table, not the LLM prompt (prompt preserves full message).
- UTF-8 encoding explicit; default on Linux is usually UTF-8 but be defensive.
- Do not use `os.linesep` — force `\n` so files are identical across OSes.
