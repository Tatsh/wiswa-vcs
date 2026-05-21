"""Type definitions for GitHub and GitLab payloads consumed by wiswa-vcs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ('AccessLevel', 'AccessLevelEntry', 'Badge', 'BranchProtectionOverrides',
           'ProjectApprovals', 'ProjectSettings', 'PushRules', 'RemoteSettings', 'Repository',
           'RepositoryConfig', 'RepositoryLicense', 'RepositoryOwner', 'Ruleset',
           'RulesetBypassActor', 'RulesetConditions', 'RulesetRefName', 'RulesetRule',
           'SecurityAndAnalysis', 'SecurityFeatureToggle')

AccessLevel: TypeAlias = Literal['disabled', 'enabled', 'private']
"""
Allowed values for GitLab feature ``*_access_level`` settings.

:meta hide-value:
"""


class RepositoryOwner(TypedDict, total=False):
    """Subset of the GitHub *simple-user* object embedded in a repository response."""

    avatar_url: str
    """Avatar image URL."""
    html_url: str
    """Public profile URL."""
    id: int
    """Account identifier."""
    login: str
    """Account login name."""
    node_id: str
    """Opaque GraphQL node identifier."""
    site_admin: bool
    """Whether the account is a GitHub staff member."""
    type: Literal['User', 'Organization', 'Bot']
    """Account kind."""
    url: str
    """API resource URL for the account."""


class RepositoryLicense(TypedDict, total=False):
    """Subset of the GitHub *nullable-license-simple* object embedded in a repository response."""

    key: str
    """SPDX-style license identifier (for example ``mit``)."""
    name: str
    """Human-readable license name."""
    node_id: str
    """Opaque GraphQL node identifier."""
    spdx_id: str
    """SPDX license identifier (for example ``MIT``)."""
    url: str | None
    """API resource URL for the license, or ``None`` when GitHub does not host one."""


class Repository(TypedDict, total=False):
    """
    Subset of a ``GET /repos/{owner}/{repo}`` JSON body.

    Lists the identification, metadata, counts, and feature toggles that callers most
    commonly need. GitHub returns many additional fields that are intentionally omitted to
    keep this surface manageable.
    """

    allow_forking: bool
    """Whether forks of the repository are allowed."""
    archived: bool
    """Whether the repository is archived and therefore read-only."""
    created_at: str
    """ISO-8601 timestamp at which the repository was created."""
    default_branch: str
    """Name of the repository's default branch."""
    description: str | None
    """Short repository description."""
    disabled: bool
    """Whether the repository has been administratively disabled."""
    fork: bool
    """Whether the repository itself is a fork of another."""
    forks_count: int
    """Total number of forks."""
    full_name: str
    """Repository slug in ``owner/repo`` form."""
    has_discussions: bool
    """Whether GitHub Discussions is enabled."""
    has_issues: bool
    """Whether the issue tracker is enabled."""
    has_pages: bool
    """Whether GitHub Pages is published."""
    has_projects: bool
    """Whether classic Projects are enabled."""
    has_wiki: bool
    """Whether the wiki is enabled."""
    homepage: str | None
    """External homepage URL displayed on the project page."""
    html_url: str
    """Public web URL of the repository."""
    id: int
    """Repository identifier."""
    is_template: bool
    """Whether the repository is published as a template."""
    language: str | None
    """Primary language as detected by GitHub Linguist."""
    license: RepositoryLicense | None
    """License metadata, or ``None`` when no license file is detected."""
    name: str
    """Repository name without the owner prefix."""
    node_id: str
    """Opaque GraphQL node identifier."""
    open_issues_count: int
    """Number of open issues."""
    owner: RepositoryOwner
    """The user or organisation that owns the repository."""
    private: bool
    """Whether the repository is private."""
    pushed_at: str
    """ISO-8601 timestamp of the most recent push."""
    size: int
    """Repository size in kilobytes."""
    stargazers_count: int
    """Total number of stars."""
    topics: Sequence[str]
    """Repository topics shown as tags on the project page."""
    updated_at: str
    """ISO-8601 timestamp of the most recent metadata update."""
    url: str
    """API resource URL for the repository."""
    visibility: Literal['public', 'private', 'internal']
    """Repository visibility setting."""
    watchers_count: int
    """Total number of watchers."""
    web_commit_signoff_required: bool
    """Whether commits made via the web UI must include a sign-off."""


class Badge(TypedDict):
    """A GitLab project-level badge definition."""

    image_url: str
    """Badge image URL."""
    link_url: str
    """Target URL the badge links to."""
    name: str
    """Human-readable badge name; used as the stable identifier on the project."""


class RulesetBypassActor(TypedDict):
    """An actor entry in a GitHub repository ruleset's ``bypass_actors`` array."""

    actor_id: int
    """Numeric actor identifier (a role id when ``actor_type`` is ``RepositoryRole``)."""
    actor_type: Literal['DeployKey', 'EnterpriseTeam', 'Integration', 'OrganizationAdmin',
                        'RepositoryRole', 'Team']
    """Actor kind."""
    bypass_mode: Literal['always', 'pull_request']
    """When the bypass applies."""


class RulesetRefName(TypedDict):
    """``ref_name`` condition in a GitHub repository ruleset."""

    exclude: Sequence[str]
    """Ref patterns to exclude."""
    include: Sequence[str]
    """Ref patterns to include."""


class RulesetConditions(TypedDict):
    """``conditions`` block of a GitHub repository ruleset."""

    ref_name: RulesetRefName
    """Ref-name include/exclude patterns the ruleset applies to."""


class RulesetRule(TypedDict, total=False):
    """A single entry in a GitHub repository ruleset's ``rules`` array."""

    type: Literal['copilot_code_review', 'creation', 'deletion', 'non_fast_forward', 'pull_request',
                  'required_linear_history', 'required_signatures', 'update']
    """Rule type."""
    parameters: Mapping[str, Any]
    """Rule-specific parameters; the shape depends on :py:attr:`type`."""


class Ruleset(TypedDict):
    """Body of a ``POST /repos/{owner}/{repo}/rulesets`` GitHub API call."""

    bypass_actors: Sequence[RulesetBypassActor]
    """Actors allowed to bypass this ruleset."""
    conditions: RulesetConditions
    """Conditions controlling when the ruleset applies."""
    enforcement: Literal['active', 'disabled', 'evaluate']
    """Enforcement mode."""
    name: str
    """Human-readable ruleset name."""
    rules: Sequence[RulesetRule]
    """Rules enforced by this ruleset."""
    target: Literal['branch', 'tag']
    """Ref kind the ruleset targets."""


class SecurityFeatureToggle(TypedDict):
    """An on/off toggle for a single GitHub security feature."""

    status: Literal['disabled', 'enabled']
    """Feature enablement state."""


class SecurityAndAnalysis(TypedDict, total=False):
    """``security_and_analysis`` block of a GitHub repository configuration."""

    dependabot_security_updates: SecurityFeatureToggle
    """Dependabot security updates toggle."""
    secret_scanning: SecurityFeatureToggle
    """Secret scanning toggle."""
    secret_scanning_non_provider_patterns: SecurityFeatureToggle
    """Non-provider secret pattern matching toggle."""
    secret_scanning_push_protection: SecurityFeatureToggle
    """Push protection for detected secrets."""
    secret_scanning_validity_checks: SecurityFeatureToggle
    """Validity-check probing for detected secrets."""


class RepositoryConfig(TypedDict, total=False):
    """Body of the ``PATCH /repos/{owner}/{repo}`` GitHub API call."""

    allow_auto_merge: bool
    """Allow auto-merge on pull requests."""
    allow_merge_commit: bool
    """Allow merge commits."""
    allow_rebase_merge: bool
    """Allow rebase merges."""
    allow_squash_merge: bool
    """Allow squash merges."""
    allow_update_branch: bool
    """Allow the *Update branch* button on out-of-date pull requests."""
    archived: bool
    """Whether the repository is archived."""
    delete_branch_on_merge: bool
    """Delete the source branch when a pull request is merged."""
    dependabot_on_actions_enabled: bool
    """Run Dependabot updates via GitHub Actions."""
    dependency_graph_autosubmit_action_enabled: bool
    """Submit dependency graph entries via GitHub Actions."""
    dependency_graph_autosubmit_action_use_labeled_runners: bool
    """Restrict autosubmit action runs to labelled runners."""
    description: str
    """Short repository description."""
    enable_max_pushes_checkbox: bool
    """Show the *Max pushes* option in the UI."""
    enable_repository_funding_links: bool
    """Surface funding links on the repository page."""
    has_discussions: bool
    """Enable GitHub Discussions."""
    has_downloads: bool
    """Enable repository downloads."""
    has_issues: bool
    """Enable the issue tracker."""
    has_pages: bool
    """Enable GitHub Pages."""
    has_projects: bool
    """Enable classic Projects."""
    has_wiki: bool
    """Enable the wiki."""
    homepage: str
    """External homepage URL."""
    include_lfs_objects: bool
    """Include LFS objects in cloning/archive operations."""
    security_and_analysis: SecurityAndAnalysis
    """Security-and-analysis feature toggles."""
    squash_merge_commit_message: Literal['BLANK', 'COMMIT_MESSAGES', 'PR_BODY']
    """Source for squash merge commit messages."""
    squash_merge_commit_title: Literal['COMMIT_OR_PR_TITLE', 'PR_TITLE']
    """Source for squash merge commit titles."""
    use_squash_pr_title_as_default: bool
    """Default the squash pull request title to the original pull request title."""
    vulnerability_updates_grouping_enabled: bool
    """Group vulnerability updates."""
    web_commit_signoff_required: bool
    """Require sign-off for commits authored through the web UI."""


class AccessLevelEntry(TypedDict, total=False):
    """
    A single entry in a GitLab protected-branch access-control array.

    GitLab accepts a list of these for ``allowed_to_push``, ``allowed_to_merge``, and
    ``allowed_to_unprotect``. Exactly one of ``access_level``, ``user_id``, or ``group_id`` is
    required per entry.
    """

    access_level: int
    """Numeric access level (for example ``40`` for *Maintainer*)."""
    group_id: int
    """Group identifier granted access."""
    user_id: int
    """User identifier granted access."""


class BranchProtectionOverrides(TypedDict, total=False):
    """
    Extra fields merged into a ``POST /projects/:id/protected_branches`` request body.

    The ``name``, ``merge_access_level``, and ``push_access_level`` fields are always set by
    :py:func:`wiswa.vcs.gitlab.protect_branches` and must not appear here.
    """

    allow_force_push: bool | str
    """Whether force-pushing the protected branch is permitted."""
    allowed_to_merge: Sequence[AccessLevelEntry]
    """Explicit list of users, groups, or access levels allowed to merge."""
    allowed_to_push: Sequence[AccessLevelEntry]
    """Explicit list of users, groups, or access levels allowed to push."""
    allowed_to_unprotect: Sequence[AccessLevelEntry]
    """Explicit list of users, groups, or access levels allowed to unprotect."""
    code_owner_approval_required: bool | str
    """Require Code Owner approval before merging into the protected branch."""
    unprotect_access_level: int
    """Numeric access level required to unprotect the branch."""


class ProjectApprovals(TypedDict, total=False):
    """``POST /projects/:id/approvals`` request body."""

    approvals_before_merge: int
    """Minimum number of approvals required before merging (deprecated by GitLab)."""
    disable_overriding_approvers_per_merge_request: bool | str
    """Forbid per-merge-request overrides of the project approver list."""
    merge_requests_author_approval: bool | str
    """Allow merge-request authors to self-approve."""
    merge_requests_disable_committers_approval: bool | str
    """Forbid users who committed to a merge request from approving it."""
    require_password_to_approve: bool | str
    """Require the approver to re-enter their password before approving."""
    require_reauthentication_to_approve: bool | str
    """Require approvers to re-authenticate before approving."""
    reset_approvals_on_push: bool | str
    """Reset existing approvals whenever new commits are pushed to the source branch."""
    selective_code_owner_removals: bool | str
    """Reset only Code Owner approvals on a new push instead of all approvals."""


class ProjectSettings(TypedDict, total=False):
    """``PUT /projects/:id`` request body covering settings exercised by Wiswa-driven syncs.

    GitLab accepts many additional project setting keys; the entries below are the ones set
    by :py:func:`wiswa.vcs.sync.sync_github_to_gitlab` and the most commonly overridden
    visibility, feature, and merge-strategy fields.
    """

    analytics_access_level: AccessLevel
    """Visibility of the project analytics view."""
    auto_cancel_pending_pipelines: Literal['disabled', 'enabled']
    """Whether redundant pending pipelines are auto-cancelled on new pushes."""
    auto_devops_deploy_strategy: Literal['continuous', 'manual', 'timed_incremental']
    """Deployment strategy used by Auto DevOps."""
    auto_devops_enabled: bool | str
    """Enable Auto DevOps for the project."""
    build_timeout: int
    """Maximum job runtime in seconds."""
    builds_access_level: AccessLevel
    """Visibility of CI/CD jobs and pipelines."""
    ci_default_git_depth: int
    """Default shallow-clone depth for CI runners."""
    container_registry_access_level: AccessLevel
    """Visibility of the project container registry."""
    default_branch: str
    """Name of the project default branch."""
    description: str
    """Short project description shown on the project page."""
    environments_access_level: AccessLevel
    """Visibility of the project environments view."""
    feature_flags_access_level: AccessLevel
    """Visibility of the feature-flags view."""
    forking_access_level: AccessLevel
    """Visibility of the fork project action."""
    homepage_url: str
    """External URL displayed as the project homepage."""
    infrastructure_access_level: AccessLevel
    """Visibility of the infrastructure-as-code view."""
    issue_branch_template: str
    """Template used when creating a branch from an issue."""
    issues_access_level: AccessLevel
    """Visibility of the issue tracker."""
    issues_enabled: bool | str
    """Enable the issue tracker (legacy toggle; prefer ``issues_access_level``)."""
    lfs_enabled: bool | str
    """Enable Git LFS for the project."""
    merge_commit_template: str
    """Template used when generating merge commit messages."""
    merge_method: Literal['ff', 'merge', 'rebase_merge']
    """Strategy used when accepting a merge request."""
    merge_requests_access_level: AccessLevel
    """Visibility of merge requests."""
    monitor_access_level: AccessLevel
    """Visibility of the monitoring views."""
    only_allow_merge_if_all_discussions_are_resolved: bool | str
    """Block merging until every thread is resolved."""
    only_allow_merge_if_pipeline_succeeds: bool | str
    """Block merging until the latest pipeline succeeds."""
    packages_enabled: bool | str
    """Enable the project package registry."""
    pages_access_level: AccessLevel
    """Visibility of GitLab Pages."""
    releases_access_level: AccessLevel
    """Visibility of the releases view."""
    remove_source_branch_after_merge: bool | str
    """Delete the source branch when a merge request is merged."""
    repository_access_level: AccessLevel
    """Visibility of the project repository itself."""
    request_access_enabled: bool | str
    """Allow non-members to request access to the project."""
    requirements_access_level: AccessLevel
    """Visibility of the requirements management view."""
    resolve_outdated_diff_discussions: bool | str
    """Automatically resolve discussions on outdated diff lines."""
    security_and_compliance_access_level: AccessLevel
    """Visibility of the security and compliance dashboard."""
    service_desk_enabled: bool | str
    """Enable the GitLab Service Desk."""
    snippets_access_level: AccessLevel
    """Visibility of project snippets."""
    squash_commit_template: str
    """Template used when generating squash commit messages."""
    squash_option: Literal['always', 'default_off', 'default_on', 'never']
    """Default squashing behaviour for merge requests."""
    suggestion_commit_message: str
    """Commit message used when a reviewer applies a suggestion."""
    topics: Sequence[str]
    """Project topics shown as tags on the project page."""
    visibility: Literal['internal', 'private', 'public']
    """Project visibility setting."""
    wiki_access_level: AccessLevel
    """Visibility of the project wiki."""


class PushRules(TypedDict, total=False):
    """``PUT`` or ``POST /projects/:id/push_rule`` request body."""

    author_email_regex: str
    """Regex that every commit author email must match."""
    branch_name_regex: str
    """Regex that every new branch name must match."""
    commit_committer_check: bool | str
    """Require the committer email to match an authenticated GitLab user."""
    commit_committer_name_check: bool | str
    """Require the committer name to match the authenticated GitLab user's name."""
    commit_message_negative_regex: str
    """Regex that commit messages must not match."""
    commit_message_regex: str
    """Regex that every commit message must match."""
    deny_delete_tag: bool | str
    """Forbid tag deletion in this project."""
    file_name_regex: str
    """Regex that committed file paths must not match."""
    max_file_size: int
    """Maximum committed file size in megabytes; ``0`` disables the check."""
    member_check: bool | str
    """Restrict commits to authenticated GitLab users."""
    prevent_secrets: bool | str
    """Block commits that look like they contain secrets."""
    reject_non_dco_commits: bool | str
    """Reject commit messages that do not include a Developer Certificate of Origin sign-off."""
    reject_unsigned_commits: bool | str
    """Reject commits that are not GPG-signed."""


class RemoteSettings(TypedDict, total=False):
    """GitLab REST API tables applied to a project during setup."""

    default_branch_protection: BranchProtectionOverrides
    """Extra fields merged into the default branch's protection settings."""
    project_approvals: ProjectApprovals
    """POST body applied to merge-request approval rules."""
    project_settings: ProjectSettings
    """Top-level PUT body for ``PUT /projects/:id``."""
    push_rules: PushRules
    """PUT body applied to push rules."""
