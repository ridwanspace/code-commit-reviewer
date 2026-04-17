"""GitHub REST client — commits, diffs, context files, pagination, rate limits."""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.github.com"
OWNER = "ridwanspace"  # hardcoded per PRD

HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "code-reviewer/1.0",
}

# Hard cap on commits fetched per run (PRD §5.4).
_MAX_COMMITS_HARD_CAP = 200

# Extension → code-fence language tag for the context bundle.
_CONTEXT_FILES: list[tuple[str, str]] = [
    ("README.md", "markdown"),
    ("package.json", "json"),
    ("pyproject.toml", "toml"),
    ("go.mod", "go"),
    ("Cargo.toml", "toml"),
    ("Gemfile", "ruby"),
    ("composer.json", "json"),
    ("tsconfig.json", "json"),
    ("next.config.js", "javascript"),
    ("next.config.mjs", "javascript"),
    ("next.config.ts", "typescript"),
]


# --------------------------------------------------------------------------- #
# Data classes                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class Commit:
    sha: str
    message: str
    author: str
    date: datetime  # UTC
    url: str


@dataclass
class FileDiff:
    filename: str
    previous_filename: str | None
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #

class GithubError(Exception):
    """Base class for all GitHub client errors."""


class NotFound(GithubError):
    """404 — repo, branch, commit, or file not found (or no access)."""


class RateLimited(GithubError):
    """GitHub primary rate limit exhausted; caller should abort or wait."""

    def __init__(self, reset_at: datetime, message: str | None = None):
        self.reset_at = reset_at
        super().__init__(
            message
            or f"GitHub rate limit exhausted; resets at {reset_at.isoformat()}"
        )


# --------------------------------------------------------------------------- #
# Client                                                                      #
# --------------------------------------------------------------------------- #

class GithubClient:
    """Thin synchronous wrapper around the GitHub REST API."""

    def __init__(self, token: str):
        self._token = token
        self._session = requests.Session()
        # Last-seen rate headers (ints/None). Used for the pre-flight check.
        self._rate_remaining: int | None = None
        self._rate_reset: int | None = None  # epoch seconds

    # ------------------------------------------------------------------ #
    # HTTP core                                                          #
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        h = dict(HEADERS_BASE)
        h["Authorization"] = f"Bearer {self._token}"
        return h

    def _preflight_rate_check(self) -> None:
        """If last-seen headers said remaining==0, either sleep briefly or raise."""
        if self._rate_remaining is None or self._rate_reset is None:
            return  # first call — no info yet; skip.
        if self._rate_remaining > 0:
            return
        now = int(time.time())
        wait = self._rate_reset - now
        if wait < 0:
            # Reset already passed; the next call will refresh headers.
            return
        if wait < 60:
            logger.debug(
                "rate limit remaining=0; sleeping %ss until reset", wait + 2
            )
            time.sleep(wait + 2)
            return
        reset_at = datetime.fromtimestamp(self._rate_reset, tz=timezone.utc)
        raise RateLimited(reset_at)

    def _update_rate_state(self, resp: requests.Response) -> None:
        rem = resp.headers.get("X-RateLimit-Remaining")
        rst = resp.headers.get("X-RateLimit-Reset")
        if rem is not None:
            try:
                self._rate_remaining = int(rem)
            except ValueError:
                pass
        if rst is not None:
            try:
                self._rate_reset = int(rst)
            except ValueError:
                pass

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> requests.Response:
        """Issue one REST call with retry, rate-limit, and logging."""
        url = path if path.startswith("http") else BASE_URL + path
        self._preflight_rate_check()

        backoffs = [1, 2, 4]
        last_err: Exception | None = None

        for attempt in range(len(backoffs) + 1):
            start = time.monotonic()
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=30,
                )
            except requests.RequestException as e:
                last_err = e
                if attempt < len(backoffs):
                    logger.debug(
                        "%s %s network error: %s; retry in %ss",
                        method, path, e, backoffs[attempt],
                    )
                    time.sleep(backoffs[attempt])
                    continue
                raise GithubError(f"network error for {method} {path}: {e}") from e

            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._update_rate_state(resp)
            rate_remaining = resp.headers.get("X-RateLimit-Remaining")
            logger.debug(
                "%s %s -> %s in %sms (rate_remaining=%s)",
                method, path, resp.status_code, elapsed_ms, rate_remaining,
            )

            status = resp.status_code

            if 200 <= status < 300:
                return resp

            if status == 404:
                raise NotFound(f"{method} {path} -> 404")

            if status == 403:
                body_text = ""
                try:
                    body_text = resp.text or ""
                except Exception:
                    body_text = ""
                if "rate limit" in body_text.lower():
                    reset_epoch = self._rate_reset or int(time.time())
                    reset_at = datetime.fromtimestamp(reset_epoch, tz=timezone.utc)
                    raise RateLimited(reset_at)
                raise GithubError(
                    f"{method} {path} -> 403: {body_text[:200]}"
                )

            if status == 429 or 500 <= status < 600:
                last_err = GithubError(
                    f"{method} {path} -> {status}: {resp.text[:200]}"
                )
                if attempt < len(backoffs):
                    wait = backoffs[attempt]
                    logger.debug(
                        "%s %s -> %s; retry %s/%s in %ss",
                        method, path, status,
                        attempt + 1, len(backoffs), wait,
                    )
                    time.sleep(wait)
                    continue
                raise last_err

            # Any other 4xx — not retryable.
            raise GithubError(
                f"{method} {path} -> {status}: {resp.text[:200]}"
            )

        # Shouldn't reach here, but be safe.
        if last_err:
            raise GithubError(str(last_err))
        raise GithubError(f"{method} {path} failed with no response")

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def verify_branch(self, repo: str, branch: str) -> None:
        """Raise NotFound if the branch (or repo) cannot be read."""
        path = f"/repos/{OWNER}/{repo}/branches/{branch}"
        try:
            self._request("GET", path)
        except NotFound:
            # Fine-grained PATs sometimes return 404 on missing perms, so we
            # can't cleanly distinguish "repo missing" vs "branch missing" vs
            # "no access". Surface a single message that covers all cases.
            raise NotFound(
                f"Branch '{branch}' on '{OWNER}/{repo}' not found or no access."
            )

    def list_commits(
        self,
        repo: str,
        branch: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_count: int | None = None,
    ) -> list[Commit]:
        """List commits on a branch, paginating up to the hard cap of 200."""
        params: dict[str, Any] = {"sha": branch, "per_page": 100}
        if since is not None:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        if until is not None:
            params["until"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")

        effective_cap = _MAX_COMMITS_HARD_CAP
        if max_count is not None:
            effective_cap = min(effective_cap, max_count)

        collected: list[Commit] = []
        seen_shas: set[str] = set()

        path: str | None = f"/repos/{OWNER}/{repo}/commits"
        next_params: dict[str, Any] | None = params

        while path is not None and len(collected) < effective_cap:
            resp = self._request("GET", path, params=next_params)
            try:
                payload = resp.json()
            except ValueError as e:
                raise GithubError(f"invalid JSON from commits endpoint: {e}") from e

            if not isinstance(payload, list):
                raise GithubError(
                    f"unexpected commits payload shape: {type(payload).__name__}"
                )

            for item in payload:
                sha = item.get("sha")
                if not sha or sha in seen_shas:
                    continue
                seen_shas.add(sha)
                collected.append(_parse_commit(item))
                if len(collected) >= effective_cap:
                    break

            # Follow Link rel=next, if any, and reset params (next URL already
            # has the query string baked in).
            next_url = _parse_link_next(resp.headers.get("Link"))
            if next_url and len(collected) < effective_cap:
                path = next_url
                next_params = None
            else:
                path = None

        return collected

    def get_commit_diff(self, repo: str, sha: str) -> list[FileDiff]:
        """Return per-file diffs for a single commit."""
        path = f"/repos/{OWNER}/{repo}/commits/{sha}"
        resp = self._request("GET", path)
        try:
            payload = resp.json()
        except ValueError as e:
            raise GithubError(f"invalid JSON from commit endpoint: {e}") from e

        files = payload.get("files") or []
        diffs: list[FileDiff] = []
        for f in files:
            filename = f.get("filename") or ""
            patch = f.get("patch")
            if patch is None:
                logger.debug(
                    "commit %s file %s has no patch (likely binary or oversized)",
                    sha[:7], filename,
                )
            diffs.append(
                FileDiff(
                    filename=filename,
                    previous_filename=f.get("previous_filename"),
                    status=f.get("status") or "",
                    additions=int(f.get("additions") or 0),
                    deletions=int(f.get("deletions") or 0),
                    changes=int(f.get("changes") or 0),
                    patch=patch,
                )
            )
        return diffs

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        """Return the decoded UTF-8 text of a file at a ref, or None on 404."""
        api_path = f"/repos/{OWNER}/{repo}/contents/{path}"
        try:
            resp = self._request("GET", api_path, params={"ref": ref})
        except NotFound:
            return None

        try:
            payload = resp.json()
        except ValueError as e:
            raise GithubError(f"invalid JSON from contents endpoint: {e}") from e

        encoding = payload.get("encoding")
        content = payload.get("content")
        if encoding != "base64" or not isinstance(content, str):
            logger.warning(
                "unexpected encoding %r for %s@%s; skipping", encoding, path, ref,
            )
            return None

        try:
            raw = base64.b64decode(content)
        except Exception as e:
            logger.warning("base64 decode failed for %s@%s: %s", path, ref, e)
            return None

        return raw.decode("utf-8", errors="replace")

    def fetch_context_bundle(self, repo: str, branch: str, max_bytes: int) -> str:
        """Concatenate a small set of manifest/README files into one string."""
        parts: list[str] = []
        total = 0
        truncated = False

        for filename, lang in _CONTEXT_FILES:
            if total >= max_bytes:
                truncated = True
                break

            body = self.get_file_content(repo, filename, branch)
            if body is None:
                continue

            if filename == "README.md":
                body = body[:3000]

            chunk = f"### {filename}\n```{lang}\n{body}\n```\n"

            # If adding the whole chunk would overflow, add what fits and mark
            # truncated. We still want the header + as much body as possible.
            if total + len(chunk) > max_bytes:
                remaining = max_bytes - total
                if remaining > 0:
                    parts.append(chunk[:remaining])
                    total += remaining
                truncated = True
                break

            parts.append(chunk)
            total += len(chunk)

        if not parts:
            return ""

        out = "".join(parts)
        if truncated:
            out += "\n… [truncated]\n"
        return out


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def _parse_link_next(link_header: str | None) -> str | None:
    """Extract the URL marked rel=\"next\" from a GitHub Link header."""
    if not link_header:
        return None
    for url, rel in _LINK_RE.findall(link_header):
        if rel == "next":
            return url
    return None


def _parse_commit(item: dict[str, Any]) -> Commit:
    """Convert one /commits list entry into a Commit dataclass."""
    sha = item.get("sha") or ""
    url = item.get("html_url") or item.get("url") or ""

    commit_obj = item.get("commit") or {}
    message = commit_obj.get("message") or ""

    # Prefer login; fall back to commit.author.name when the committer email
    # doesn't resolve to a GitHub user (author is null).
    author = ""
    author_obj = item.get("author")
    if isinstance(author_obj, dict):
        login = author_obj.get("login")
        if login:
            author = login
    if not author:
        inner_author = commit_obj.get("author") or {}
        author = inner_author.get("name") or ""

    # Committer-date semantics (matches the REST since/until filter).
    date_str = ""
    committer_obj = commit_obj.get("committer") or {}
    if isinstance(committer_obj, dict):
        date_str = committer_obj.get("date") or ""
    if not date_str:
        inner_author = commit_obj.get("author") or {}
        date_str = inner_author.get("date") or ""

    date = _parse_iso_utc(date_str)

    return Commit(sha=sha, message=message, author=author, date=date, url=url)


def _parse_iso_utc(s: str) -> datetime:
    """Parse a GitHub RFC3339 timestamp (e.g. '2026-04-17T12:34:56Z') as UTC."""
    if not s:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    # GitHub always emits 'Z'-suffixed UTC. Handle that plus general offsets.
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1])
            return dt.replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
