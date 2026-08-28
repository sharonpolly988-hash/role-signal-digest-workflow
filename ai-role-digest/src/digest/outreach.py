"""
Draft LinkedIn outreach messages for accounting and financial analyst roles.
"""

from __future__ import annotations

import logging
import os

from .models import OutreachDraft, ScoredPost

log = logging.getLogger(__name__)

OUTREACH_MODE = os.environ.get("OUTREACH_MODE", "template").lower()

CONNECTION_REQUEST_LIMIT = 200
DIRECT_MESSAGE_LIMIT = 8000


ROLE_TITLES = (
    "Senior Financial Analyst",
    "Financial Analyst",
    "Senior Accountant",
    "Staff Accountant",
    "Accounting Analyst",
    "Financial Reporting Accountant",
    "Financial Accountant",
    "Corporate Accountant",
)


def _truncate(text: str, limit: int) -> str:
    normalized = " ".join(text.split())

    if len(normalized) <= limit:
        return normalized

    suffix = "..."
    return normalized[: limit - len(suffix)].rstrip() + suffix


def _detect_role(scored: ScoredPost) -> str:
    """
    Detect the job title from the LinkedIn post without copying
    the job description into the outreach message.
    """
    text = scored.post.text.lower()

    for role in ROLE_TITLES:
        if role.lower() in text:
            return role

    return "accounting opportunity"


def _fallback_draft(scored: ScoredPost) -> OutreachDraft:
    name = (
        scored.poster_name.split()[0]
        if scored.poster_name
        else "there"
    )

    role = _detect_role(scored)

    title = f"{role} via LinkedIn"

    connection = _truncate(
        f"Hi {name}, I saw your post about the {role} opening and it caught my attention. "
        "I have 3+ years of accounting experience and am exploring opportunities in New York. "
        "I'd love to connect.",
        CONNECTION_REQUEST_LIMIT,
    )

    direct = (
        f"Hi {name},\n\n"
        f"Thanks for connecting. I came across your post about the {role} opening "
        "and wanted to reach out.\n\n"
        "I have 3+ years of accounting experience, with a background in month-end close, "
        "financial reporting, reconciliations, and variance analysis. I'm currently exploring "
        "accounting and finance opportunities in New York, and this position seems well aligned "
        "with my experience.\n\n"
        "I'd love to learn more about the role and the team. If you're open to it, "
        "I'd be happy to have a quick conversation.\n\n"
        "Best,\n"
        "Zhixin"
    )

    return OutreachDraft(
        title=_truncate(title, 120),
        connection_request=connection,
        direct_message=direct[:DIRECT_MESSAGE_LIMIT],
    )


def draft_reach_out(
    scored: list[ScoredPost],
    mode: str | None = None,
) -> list[ScoredPost]:

    if not scored:
        return []

    selected_mode = (mode or OUTREACH_MODE).lower()

    if selected_mode == "template":
        drafted = [
            item.model_copy(
                update={"outreach": _fallback_draft(item)}
            )
            for item in scored
        ]
    else:
        raise ValueError(
            f"Unknown OUTREACH_MODE: {selected_mode}"
        )

    log.info("outreach mode: %s", selected_mode)
    log.info(
        "outreach: drafted messages for %d posts",
        len(drafted),
    )

    return drafted