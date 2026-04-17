# Plan 01 — Scaffolding, `.env`, and CLI

**Goal:** A runnable `code-reviewer.py` that parses all CLI args, loads `.env`, resolves `--timeperiod` into a concrete window, and prints the resolved plan without making any network calls.

**PRD refs:** §2, §3.1, §3.2 (entrypoint), §4, §9 (exit 1, 2).

---

## Tasks

### 1. Dependencies
- Create `requirements.txt`:
  ```
  requests>=2.31
  python-dotenv>=1.0
  google-generativeai>=0.8
  python-dateutil>=2.8
  ```
- Install into a venv (`python -m venv .venv && .venv/bin/pip install -r requirements.txt`).

### 2. File skeletons (empty-but-importable)
Create these files with a single-line module docstring so imports don't break as modules are filled in:
- `code-reviewer.py`
- `github_client.py`
- `gemini_client.py`
- `diff_filter.py`
- `report_writer.py`

### 3. `.env` loading
In `code-reviewer.py`, early in `main()`:
- Call `dotenv.load_dotenv()` (no path arg — picks up CWD `.env`).
- Read `GITHUB_API_KEY` and `GEMINI_API_KEY` from `os.environ`.
- If either missing → print PRD §9 error to stderr, exit `1`.

### 4. CLI parser
Use `argparse`. Arguments per PRD §4.2:

| Flag | Dest | Type | Required | Default |
|---|---|---|---|---|
| `--reponame` | `reponame` | `str` | yes | — |
| `--branch` | `branch` | `str` | yes | — |
| `--timeperiod` | `timeperiod` | `str` | yes | — |
| `--output-dir` | `output_dir` | `str` | no | `./reports` |
| `--max-context-bytes` | `max_context_bytes` | `int` | no | `40000` |
| `--verbose` / `-v` | `verbose` | flag | no | `False` |

Post-parse validation:
- `reponame` matches `^[A-Za-z0-9._-]+$` → else exit 2.
- `branch` non-empty → else exit 2.

### 5. Period resolver (`resolve_period`)
Signature:
```python
def resolve_period(raw: str, now: datetime) -> Period
```
Where `Period` is a dataclass:
```python
@dataclass
class Period:
    kind: Literal["count", "days", "range"]
    since: datetime | None   # UTC
    until: datetime | None   # UTC
    count: int | None
    display: str             # human-readable per PRD §8.3
```

Parse order (first match wins, per PRD §4.3):
1. `^\d+$` → count. Validate `1..200`.
2. `^(\d+)d$` → days. Validate `1..365`. Compute `since = now - days`, `until = now`.
3. `^\d{4}-\d{2}-\d{2}:\d{4}-\d{2}-\d{2}$` → range. Parse via `dateutil.parser.isoparse`. `since = start@00:00Z`, `until = end@23:59:59Z`. Require `since <= until`.
4. Else → exit 2 with the PRD §4.3 error message.

### 6. Logging
- Configure `logging.basicConfig(level=DEBUG if args.verbose else INFO, format='%(levelname)s %(message)s')`.
- Info → progress (one line per phase). Debug → HTTP timings, token estimates.

### 7. Dry-run smoke test
End `main()` with:
```python
log.info("Resolved: repo=ridwanspace/%s branch=%s period=%s",
         args.reponame, args.branch, period.display)
log.info("Dry run — network wiring comes in Plan 02.")
sys.exit(0)
```

---

## Acceptance

- [ ] `python code-reviewer.py --reponame foo --branch main --timeperiod 7d` prints the resolved period and exits 0.
- [ ] Missing env key → stderr message, exit 1.
- [ ] `--timeperiod abc` → stderr message, exit 2.
- [ ] `--timeperiod 2026-04-17:2026-04-01` (reversed) → exit 2.
- [ ] `--timeperiod 500` → exit 2 (out of bounds).
- [ ] `-v` enables DEBUG logs.

## Risks / gotchas

- `argparse` treats unknown flags as errors — that's what we want. Don't use `parse_known_args`.
- `dateutil.isoparse("2026-04-01")` returns naive datetime; apply `.replace(tzinfo=timezone.utc)` explicitly.
- GitHub expects `since`/`until` as RFC3339 (`2026-04-17T00:00:00Z`). Format at call-site in Plan 02, not here.
