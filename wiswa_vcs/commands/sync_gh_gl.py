"""``wiswa-sync-gh-gl`` CLI: mirror GitHub metadata to a GitLab project."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
import asyncio
import json
import logging

from bascom import setup_logging
from wiswa_vcs.sync import sync_github_to_gitlab
import click
import niquests

if TYPE_CHECKING:
    from wiswa_vcs.typing import GitLabConfig

__all__ = ('main',)

log = logging.getLogger(__name__)


def _load_gitlab_config(raw: str | None) -> GitLabConfig:
    if not raw:
        return {}
    if raw.lstrip().startswith('{'):
        text = raw
        source = 'JSON literal'
    else:
        path = Path(raw)
        if not path.is_file():
            msg = (f'--gitlab-config must be a JSON object or the path to an existing JSON file; '
                   f'{raw!r} is neither.')
            raise click.BadParameter(msg)
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as e:
            msg = f'Could not read --gitlab-config file `{path}`: {e}.'
            raise click.BadParameter(msg) from e
        source = f'file `{path}`'
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as e:
        msg = f'Could not decode --gitlab-config {source}: {e}.'
        raise click.BadParameter(msg) from e
    if not isinstance(loaded, dict):
        msg = f'--gitlab-config must decode to a JSON object (from {source}).'
        raise click.BadParameter(msg)
    return cast('GitLabConfig', loaded)


@click.command(context_settings={'help_option_names': ('-h', '--help')})
@click.option('--badges-file',
              default='docs/badges.rst',
              envvar='BADGES_FILE',
              help='reStructuredText badges file to sync; skipped if it does not exist.',
              show_default=True,
              type=click.Path(dir_okay=False, path_type=Path))
@click.option('-d', '--debug', help='Enable debug level logging.', is_flag=True)
@click.option('--default-branch',
              envvar='DEFAULT_BRANCH',
              help='Default branch name; always added to the protected branches list.',
              required=True)
@click.option('--github-repo-uri',
              envvar='GITHUB_REPO_URI',
              help=('Source GitHub repository URI '
                    '(for example `https://github.com/owner/repo.git`) or `owner/repo` slug.'),
              required=True)
@click.option('--github-token',
              envvar=('GH_TOKEN', 'GITHUB_TOKEN'),
              help='GitHub personal access token used to read repository metadata.',
              required=True)
@click.option('--gitlab-config',
              envvar='GITLAB_CONFIG_JSON',
              help=('JSON object literal, or the path to a JSON file, with optional '
                    '`project_settings`, `push_rules`, `project_approvals`, and '
                    '`default_branch_protection` tables.'))
@click.option('--gitlab-repo-uri',
              envvar='GITLAB_REPO_URI',
              help='HTTPS URI of the destination GitLab repository.',
              required=True)
@click.option('--gitlab-token',
              envvar='GITLAB_TOKEN',
              help='GitLab personal access token with the `api` scope.',
              required=True)
@click.option('--no-mirror-overrides',
              help=('Skip the read-only mirror project setting overrides '
                    '(merge requests, CI/CD, LFS, and service desk are otherwise disabled).'),
              is_flag=True)
def main(*, badges_file: Path, debug: bool, default_branch: str, github_repo_uri: str,
         github_token: str, gitlab_config: str | None, gitlab_repo_uri: str, gitlab_token: str,
         no_mirror_overrides: bool) -> None:
    """Mirror GitHub metadata, protected refs, and badges to a GitLab project."""  # noqa: DOC501
    setup_logging(debug=debug, loggers={'wiswa_vcs': {}})
    config = _load_gitlab_config(gitlab_config)
    resolved_badges_file: Path | None = badges_file if badges_file.is_file() else None

    async def _run() -> None:
        async with niquests.AsyncSession() as session:
            await sync_github_to_gitlab(session,
                                        apply_mirror_overrides=not no_mirror_overrides,
                                        badges_file=resolved_badges_file,
                                        default_branch=default_branch,
                                        github_repo_uri=github_repo_uri,
                                        github_token=github_token,
                                        gitlab_config=config,
                                        gitlab_repo_uri=gitlab_repo_uri,
                                        gitlab_token=gitlab_token)

    try:
        asyncio.run(_run())
    except Exception as e:
        log.exception('Sync failed.')
        raise click.Abort from e
