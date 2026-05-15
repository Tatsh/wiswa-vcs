"""Type definitions shared across :py:mod:`wiswa_vcs` modules."""
from __future__ import annotations

from typing import TypedDict

__all__ = ('Badge', 'GitLabConfig')


class Badge(TypedDict):
    """A GitLab project-level badge definition."""

    image_url: str
    """Badge image URL."""
    link_url: str
    """Target URL the badge links to."""
    name: str
    """Human-readable badge name; used as the stable identifier on the project."""


class GitLabConfig(TypedDict, total=False):
    """Optional opinionated tables applied during GitLab project sync."""

    default_branch_protection: dict[str, object]
    """PATCH body applied to the default branch's protection settings."""
    project_approvals: dict[str, object]
    """POST body applied to merge-request approval rules."""
    project_settings: dict[str, object]
    """Top-level PUT body for ``PUT /projects/:id``."""
    push_rules: dict[str, object]
    """PUT body applied to push rules."""
