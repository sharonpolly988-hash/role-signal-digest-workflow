"""Score posts with free local rules or Gemini structured output."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

from .feedback_learning import apply_feedback_boost, passes_feedback_hard_filters
from .models import Post, ScoredPost
from .quality import evaluate_post_quality

log = logging.getLogger(__name__)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
SCORING_MODE = os.environ.get("SCORING_MODE", "rules").lower()
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "7"))
MAX_CONCURRENT = 5
RUBRIC_PATH = Path("config/rubric.md")
DEFAULT_LLM_MAX_POSTS_PER_RUN = 5
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 256
DEFAULT_LLM_POST_CHAR_LIMIT = 3000

TARGET_TERMS = (
    "staff accountant",
    "senior accountant",
    "financial analyst",
    "senior financial analyst",
    "accountant",
    "accounting analyst",
    "financial accountant",
    "financial reporting accountant",
    "corporate accountant",
    "accounting",
    "financial analysis",
    "fp&a",
)

STRONG_TARGET_TERMS = (
    "staff accountant",
    "senior accountant",
    "financial analyst",
    "senior financial analyst",
)

ACCOUNTING_SKILL_TERMS = (
    "gaap",
    "general ledger",
    "month-end close",
    "month end close",
    "journal entry",
    "journal entries",
    "reconciliation",
    "reconciliations",
    "financial reporting",
    "financial statements",
    "accrual",
    "accruals",
    "prepaid",
    "prepaids",
    "balance sheet",
    "income statement",
)

ANALYSIS_TERMS = (
    "financial analysis",
    "variance analysis",
    "budget",
    "budgeting",
    "forecast",
    "forecasting",
    "fp&a",
    "financial modeling",
    "financial model",
)

POSITIVE_LOCATION_TERMS = (
    "new york",
    "new york city",
    "nyc",
    "manhattan",
    "brooklyn",
    "queens",
    "bronx",
    "staten island",
)

REJECT_TERMS = (
    "account executive",
    "bookkeeper",
    "bookkeeping",
    "payroll specialist",
    "payroll coordinator",
    "tax preparer",
    "tax associate",
    "audit associate",
    "auditor",
    "accounts payable clerk",
    "accounts receivable clerk",
    "controller",
    "assistant controller",
    "director of accounting",
    "chief accounting officer",
    "vice president",
)

TOO_SENIOR_TERMS = (
    "director",
    "principal",
    "controller",
    "assistant controller",
    "vice president",
    "vp",
    "chief accounting officer",
)

CANDIDATE_JOB_SEARCH_PATTERNS = (
    r"\bi(?:'| a)?m currently seeking\b",
    r"\bi(?:'| a)?m seeking\b",
    r"\bi(?:'| a)?m looking for (?:a|my next|new|an?) .{0,40}\b(role|opportunity|job)\b",
    r"\bopen to work\b",
    r"\bseeking (?:a|my next|new|an?) .{0,40}\b(role|opportunity|job)\b",
)
EXPIRED_POST_TERMS = (
    "applications are closed",
    "applications closed",
    "broken link",
    "no longer accepting",
    "no longer available",
    "no longer exists",
    "position has been filled",
    "role has been filled",
    "role is filled",
)


class _ScoreResult(BaseModel):
    score: int = Field(ge=0, le=10)
    role_match: bool
    reason: str


def _rubric() -> str:
    return RUBRIC_PATH.read_text()


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _llm_max_posts_per_run() -> int:
    return _positive_env_int("LLM_MAX_POSTS_PER_RUN", DEFAULT_LLM_MAX_POSTS_PER_RUN)


def _gemini_max_output_tokens() -> int:
    return _positive_env_int(
        "GEMINI_MAX_OUTPUT_TOKENS",
        DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
    )


def _llm_post_char_limit() -> int:
    return _positive_env_int("LLM_POST_CHAR_LIMIT", DEFAULT_LLM_POST_CHAR_LIMIT)


def _matches_word(term: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(term.lower()) + r"\b", text) is not None


def _requires_too_many_years(text: str, max_years: int = 6) -> bool:
    for match in re.finditer(r"(\d+)\s*\+?\s*years?", text):
        if int(match.group(1)) > max_years:
            return True
    return False


def _looks_like_candidate_job_search(post_text: str) -> bool:
    return any(re.search(pattern, post_text) for pattern in CANDIDATE_JOB_SEARCH_PATTERNS)


def _base_hard_reject_reasons(post: Post) -> list[str]:
    post_text = post.text.lower()
    reasons: list[str] = []

    if _looks_like_candidate_job_search(post_text):
        reasons.append("not_hiring_post")
    if any(term in post_text for term in EXPIRED_POST_TERMS):
        reasons.append("expired_post")
    if any(_matches_word(term, post_text) for term in TOO_SENIOR_TERMS) or _requires_too_many_years(
        post_text
    ):
        reasons.append("too_senior")
    if any(term in post_text for term in REJECT_TERMS):
        reasons.append("not_relevant_domain")
    return reasons


def _post_key(post: Post) -> str:
    return post.id or post.url


def _prefilter_posts(
    posts: list[Post],
    feedback_config: dict | None = None,
) -> list[Post]:
    kept: list[Post] = []
    feedback_config = feedback_config or {}
    seen_keys: set[str] = set()
    for post in posts:
        context = f"{post.author_headline}\n{post.text}".lower()
        has_target_signal = any(term in context for term in TARGET_TERMS)
        key = _post_key(post)
        reject_reasons = []
        if key in seen_keys:
            reject_reasons.append("duplicate")
        else:
            seen_keys.add(key)
        reject_reasons.extend(evaluate_post_quality(post.text).reasons)
        reject_reasons.extend(_base_hard_reject_reasons(post))
        if not has_target_signal:
            reject_reasons.append("not_relevant_domain")
        if not passes_feedback_hard_filters(post, feedback_config):
            reject_reasons.append("feedback_rule")

        if not reject_reasons:
            kept.append(post)

    log.info("prefilter: %d posts -> %d plausible posts", len(posts), len(kept))
    return kept


async def _score_one(
    client: genai.client.AsyncClient,
    sem: asyncio.Semaphore,
    post: Post,
    system: str,
) -> Optional[ScoredPost]:
    user_msg = (
        f"{system}\n\n"
        "Evaluate this public hiring post. Return only the requested structured "
        "result.\n\n"
        f"Author headline: {post.author_headline}\n\n"
        f"Post text:\n{post.text[: _llm_post_char_limit()]}"
    )
    async with sem:
        for attempt in range(3):
            try:
                interaction = await client.interactions.create(
                    model=os.environ.get("GEMINI_MODEL", GEMINI_MODEL),
                    input=user_msg,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": _ScoreResult.model_json_schema(),
                    },
                    generation_config={
                        "max_output_tokens": _gemini_max_output_tokens(),
                        "thinking_level": "minimal",
                    },
                )
                if not interaction.output_text:
                    log.warning("parse returned None for post %s", post.id)
                    return None
                r = _ScoreResult.model_validate_json(interaction.output_text)
                return ScoredPost(
                    post=post,
                    score=r.score,
                    role_match=r.role_match,
                    reason=r.reason,
                    poster_name=post.author_name,
                    poster_url=post.author_url,
                )
            except genai_errors.APIError as exc:
                if exc.code != 429:
                    log.error("Gemini scoring error for post %s: %s", post.id, exc)
                    return None
                wait = 2 ** (attempt + 2)
                log.warning(
                    "Gemini rate limited, waiting %ds (attempt %d)",
                    wait,
                    attempt + 1,
                )
                await asyncio.sleep(wait)
            except Exception as exc:
                log.error("Gemini scoring error for post %s: %s", post.id, exc)
                return None
    return None


async def _score_all(posts: list[Post]) -> list[ScoredPost]:
    system = _rubric()
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with genai.Client(api_key=os.environ["GEMINI_API_KEY"]).aio as client:
        tasks = [_score_one(client, sem, p, system) for p in posts]
        results = await asyncio.gather(*tasks)
    return [result or _rule_score(post) for post, result in zip(posts, results)]


def _rule_score(post: Post) -> ScoredPost:
    """Deterministically score a post without calling an external model."""
    text = f"{post.author_headline}\n{post.text}".lower()
    matched: list[str] = []
    score = 4  # The prefilter already established hiring + target-role intent.

    def add_signal(label: str, terms: tuple[str, ...], points: int) -> None:
        nonlocal score
        if any(term in text for term in terms):
            score += points
            matched.append(label)

    add_signal("target role match", STRONG_TARGET_TERMS, 2)
    add_signal("accounting/reporting skills", ACCOUNTING_SKILL_TERMS, 1)
    add_signal("financial analysis skills", ANALYSIS_TERMS, 1)
    add_signal("New York location", POSITIVE_LOCATION_TERMS, 2)

    final_score = min(10, score)
    reason = "Rule-based match: " + ", ".join(matched or ["general accounting/finance target-role signal"])
    return ScoredPost(
        post=post,
        score=final_score,
        role_match=final_score >= SCORE_THRESHOLD,
        reason=reason + ".",
        poster_name=post.author_name,
        poster_url=post.author_url,
    )


def _top_rule_candidates(
    posts: list[Post],
    limit: int,
    feedback_config: dict | None = None,
) -> list[Post]:
    """Use free local scoring to select the small set that Gemini evaluates."""
    feedback_config = feedback_config or {}
    ranked = sorted(
        enumerate(posts),
        key=lambda item: (
            -apply_feedback_boost(_rule_score(item[1]), feedback_config).score,
            item[0],
        ),
    )
    return [post for _, post in ranked[:limit]]


def score_and_filter(
    posts: list[Post],
    feedback_config: dict | None = None,
    mode: str | None = None,
) -> list[ScoredPost]:
    feedback_config = feedback_config or {}
    candidates = _prefilter_posts(posts, feedback_config=feedback_config)
    selected_mode = (mode or os.environ.get("SCORING_MODE", SCORING_MODE)).lower()
    if selected_mode == "gemini":
        if os.environ.get("GEMINI_API_KEY"):
            llm_candidates = _top_rule_candidates(
                candidates,
                _llm_max_posts_per_run(),
                feedback_config,
            )
            log.info(
                "Gemini budget gate: evaluating %d of %d candidates",
                len(llm_candidates),
                len(candidates),
            )
            scored = asyncio.run(_score_all(llm_candidates))
        else:
            log.warning("GEMINI_API_KEY is missing; falling back to rule scoring")
            scored = [_rule_score(post) for post in candidates]
    elif selected_mode == "rules":
        scored = [_rule_score(post) for post in candidates]
    else:
        raise ValueError(f"Unknown SCORING_MODE: {selected_mode}")
    scored = [apply_feedback_boost(s, feedback_config) for s in scored]
    kept = [s for s in scored if s.score >= SCORE_THRESHOLD and s.role_match]
    log.info(
        "scoring (%s): %d posts → %d candidates → %d scored → %d above threshold %d",
        selected_mode,
        len(posts),
        len(candidates),
        len(scored),
        len(kept),
        SCORE_THRESHOLD,
    )
    return kept
