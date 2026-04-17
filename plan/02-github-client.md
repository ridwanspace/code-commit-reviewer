# Plan 02 — `github_client.py`

**Goal:** A self-contained client that verifies branches, lists commits with pagination, fetches per-commit diffs, loads context files, and degrades gracefully under rate limiting.

**PRD refs:** §3.2, §5, §9 (exits 3, 4).

---

## Tasks

### 1. Module shape

```python
BASE_URL = "https://api.github.com"
OWNER = "ridwanspace"  # hardcoded per PRD
HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "code-reviewer/1.0",
}

@dataclass
class Commit:
    sha: str
    message: str             # full message
    author: str              # author.login or author.name fallback
    date: datetime           # committer.date, UTC
    url: str

@dataclass
class FileDiff:
    filename: str
    previous_filename: str | None
    status: str              # added|modified|removed|renamed
    additions: int
    deletions: int
    changes: int
    patch: str | None        # None for binary or oversized

class GithubError(Exception): ...
class NotFound(GithubError): ...
class RateLimited(GithubError):
    def __init__(self, reset_at: datetime): ...

class GithubClient:
    def __init__(self, token: str): ...
```

### 2. HTTP core (`_request`)

Single internal method for every call:
```python
def _request(self, method, path, params=None) -> requests.Response
```
Behavior:
- Build URL = `BASE_URL + path`.
- Set `Authorization: Bearer {token}`.
- Inspect `X-RateLimit-Remaining` before issuing: if `0` and `reset - now < 60s`, sleep `reset - now + 2`. Else raise `RateLimited`.
- On `200`: return response.
- On `404`: raise `NotFound`.
- On `403` + body contains `rate limit`: raise `RateLimited(reset_at)`.
- On `429` / `5xx`: exponential backoff (`1s, 2s, 4s`), max 3 retries, then raise `GithubError`.
- Log `DEBUG` on every call: `method, path, status, ms, rate_remaining`.

### 3. `verify_branch(repo, branch) -> None`
- `GET /repos/{OWNER}/{repo}/branches/{branch}`.
- 404 → `NotFound` with which (repo vs branch) inferred from message.
- Called first in orchestration for fail-fast.

### 4. `list_commits(repo, branch, *, since=None, until=None, max_count=None) -> list[Commit]`
- `GET /repos/{OWNER}/{repo}/commits?sha={branch}&per_page=100`.
- Add `since=<rfc3339>` / `until=<rfc3339>` when provided (format with `.strftime("%Y-%m-%dT%H:%M:%SZ")`).
- Follow `Link: <...>; rel="next"` until absent.
- Stop early if `max_count` provided and reached.
- Hard cap at **200** regardless of `max_count` (PRD §5.4).
- Deduplicate by SHA defensively (pagination rarely overlaps, but be safe).

### 5. `get_commit_diff(repo, sha) -> list[FileDiff]`
- `GET /repos/{OWNER}/{repo}/commits/{sha}`.
- Map each entry in `.files[]` into `FileDiff`.
- Missing `patch` → set `patch=None`. Log DEBUG that it's likely binary.

### 6. `get_file_content(repo, path, ref) -> str | None`
- `GET /repos/{OWNER}/{repo}/contents/{path}?ref={ref}`.
- 404 → return `None` (caller decides how to react).
- Decode base64 `content` field; UTF-8 decode with `errors="replace"`.
- If `encoding` is not `base64`, log a warning and return None.

### 7. `fetch_context_bundle(repo, branch, max_bytes) -> str`
Per PRD §5.6, try each of:
```
README.md (truncate to 3000 chars first)
package.json
pyproject.toml
go.mod
Cargo.toml
Gemfile
composer.json
tsconfig.json
next.config.js
next.config.mjs
next.config.ts
```
- Concat with per-file header `### <path>\n\`\`\`<lang>\n<body>\n\`\`\`\n`.
- Stop adding files once total length exceeds `max_bytes`.
- If truncation occurs, append `\n… [truncated]\n`.
- Return `""` if nothing loaded (caller logs a warning — reviewer still works).

---

## Acceptance

- [ ] `verify_branch("nonexistent-repo", "main")` raises `NotFound`.
- [ ] `list_commits` returns commits within the window and no more than 200.
- [ ] `get_commit_diff` returns `FileDiff` objects including a `removed` file with `patch=None` when applicable.
- [ ] `fetch_context_bundle` returns a non-empty string on any real repo with a README.
- [ ] Rate-limit header of 0 with a <60s reset triggers sleep, not an exception.

## Manual validation script

Quick REPL harness (not committed):
```python
from dotenv import load_dotenv; load_dotenv()
import os
from github_client import GithubClient
gc = GithubClient(os.environ["GITHUB_API_KEY"])
gc.verify_branch("restaurant-ops-management", "staging")
commits = gc.list_commits("restaurant-ops-management", "staging", max_count=5)
print(len(commits), commits[0].sha, commits[0].message[:60])
diff = gc.get_commit_diff("restaurant-ops-management", commits[0].sha)
print([(f.filename, f.additions, f.deletions) for f in diff[:5]])
```

## Risks / gotchas

- Fine-grained PATs sometimes return 404 instead of 403 on missing perms. Error message should say "not found or no access" to cover both.
- Commit `author` can be null when the committer email doesn't match a GitHub user — fall back to `commit.author.name`.
- `since`/`until` are **committer** date semantics on the REST endpoint; match that in period-resolver docs.
- Pagination `Link` header parsing: use a small regex — don't pull in `requests-toolbelt` just for this.
