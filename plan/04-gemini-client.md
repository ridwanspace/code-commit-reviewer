# Plan 04 — `gemini_client.py`

**Goal:** Assemble the prompt and make a single Gemini call that returns a six-section Markdown review body.

**PRD refs:** §3.2 (`gemini_client`), §7, §9 (exits 5, 6).

---

## Tasks

### 1. Module-level constants

```python
MODEL_ID = "gemini-3.1-flash-lite-preview"
TEMPERATURE = 0.2
TOP_P = 0.9
MAX_OUTPUT_TOKENS = 8192

# Abort if estimated tokens exceed this
TOKEN_BUDGET_HARD_LIMIT = 800_000
TOKEN_CHARS_PER_TOKEN = 4  # rough estimate
```

### 2. System prompt

Paste the PRD §7.3 system prompt **verbatim** as a module-level string constant `SYSTEM_PROMPT`. Do not paraphrase — the downstream parser (commit-breakdown section-splitting in Plan 05) relies on the exact section headers.

### 3. Data types

```python
@dataclass
class ReviewRequest:
    reponame: str
    branch: str
    period_display: str
    commit_count: int
    context_block: str                    # from github_client.fetch_context_bundle
    commits: list[tuple[Commit, list[FileDiff]]]  # filtered + trimmed

class GeminiError(Exception): ...
class TokenBudgetExceeded(GeminiError): ...
```

### 4. Prompt builder — `build_user_prompt(req: ReviewRequest) -> str`

Produce (matches PRD §7.4):

```
# Repository
ridwanspace/{reponame} @ {branch}

# Period
{period_display}  ({commit_count} commits)

# Codebase Context
{context_block or "(no context files found)"}

# Aggregated Diffs
{diff_block}
```

`diff_block` per commit:

```
## Commit {sha[:7]} — {first_line_of_message}
Author: {author}
Date: {iso_date}

### {filename} ({status}, +{additions} -{deletions})
```diff
{patch}
```
```

Rules:
- `first_line_of_message` = `message.split("\n", 1)[0]`, max 120 chars, ellipsis on overflow.
- `iso_date` = `date.strftime("%Y-%m-%dT%H:%M:%SZ")`.
- If `patch is None` → render a single-line note instead of the fenced block: `_(no patch — binary or removed)_`.
- Use triple-backtick-diff fencing. Watch for diffs containing ``` — escape by bumping to 4-backtick fence if detected (rare but possible).

### 5. Token pre-flight — `estimate_tokens(text: str) -> int`

`return len(text) // TOKEN_CHARS_PER_TOKEN`

Called on `SYSTEM_PROMPT + user_prompt`. If > `TOKEN_BUDGET_HARD_LIMIT`, raise `TokenBudgetExceeded` — orchestration maps to exit 6.

### 6. Client class

```python
class GeminiReviewer:
    def __init__(self, api_key: str):
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=MODEL_ID,
            system_instruction=SYSTEM_PROMPT,
            generation_config={
                "temperature": TEMPERATURE,
                "top_p": TOP_P,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            },
        )

    def review(self, req: ReviewRequest) -> str:
        prompt = build_user_prompt(req)
        est = estimate_tokens(SYSTEM_PROMPT + prompt)
        log.info("Prompt ~%d tokens (%d chars diff)", est, len(prompt))
        if est > TOKEN_BUDGET_HARD_LIMIT:
            raise TokenBudgetExceeded(est)
        try:
            resp = self._model.generate_content(prompt)
        except Exception as e:
            raise GeminiError(str(e)) from e
        if not resp.text:
            raise GeminiError("empty response from Gemini")
        return resp.text.strip()
```

### 7. Output sanity check

After `response.text` returns:
- Warn (don't fail) if the text is missing any of: `Summary`, `Critical Issues`, `Major Issues`, `Minor Issues`, `Suggestions`, `What Was Done Well`. Matching can be loose: `re.search(rf"(?mi)^#+\s*{header}", text)`.
- Warn if `Overall Quality Score:` is missing — the report writer will still function, it just won't surface a score.

---

## Acceptance

- [ ] `build_user_prompt` on a fixture of 2 commits / 3 files produces well-formed Markdown with no leaked `None`.
- [ ] `estimate_tokens("a" * 400_000)` returns `100_000`.
- [ ] With a tiny fixture, `GeminiReviewer.review(...)` returns a string containing at least 3 of the 6 expected section headers.
- [ ] A forced 10 MB prompt raises `TokenBudgetExceeded` without calling the API.
- [ ] API-layer exceptions surface as `GeminiError` with the original message.

## Risks / gotchas

- `google-generativeai` surfaces safety blocks via `response.prompt_feedback` — inspect it on empty text and include reason in the raised error.
- The model ID `gemini-3.1-flash-lite-preview` is a preview SKU; if the SDK rejects it, the orchestration layer will map to exit 5 with the raw message, which is helpful for debugging.
- Don't set a system role in `generate_content` — use `system_instruction=` in the model constructor (SDK-idiomatic).
- Keep the SDK pinned (`>=0.8`) — earlier versions don't support `system_instruction`.
