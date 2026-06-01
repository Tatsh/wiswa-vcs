# wiswa-vcs

<!-- WISWA-GENERATED-README:START -->

[![Python versions](https://img.shields.io/pypi/pyversions/wiswa-vcs.svg?color=blue&logo=python&logoColor=white)](https://www.python.org/)
[![PyPI - Version](https://img.shields.io/pypi/v/wiswa-vcs)](https://pypi.org/project/wiswa-vcs/)
[![GitHub tag (with filter)](https://img.shields.io/github/v/tag/Tatsh/wiswa-vcs)](https://github.com/Tatsh/wiswa-vcs/tags)
[![License](https://img.shields.io/github/license/Tatsh/wiswa-vcs)](https://github.com/Tatsh/wiswa-vcs/blob/master/LICENSE.txt)
[![GitHub commits since latest release (by SemVer including pre-releases)](https://img.shields.io/github/commits-since/Tatsh/wiswa-vcs/v0.1.0/master)](https://github.com/Tatsh/wiswa-vcs/compare/v0.1.0...master)
[![CodeQL](https://github.com/Tatsh/wiswa-vcs/actions/workflows/codeql.yml/badge.svg)](https://github.com/Tatsh/wiswa-vcs/actions/workflows/codeql.yml)
[![QA](https://github.com/Tatsh/wiswa-vcs/actions/workflows/qa.yml/badge.svg)](https://github.com/Tatsh/wiswa-vcs/actions/workflows/qa.yml)
[![Tests](https://github.com/Tatsh/wiswa-vcs/actions/workflows/tests.yml/badge.svg)](https://github.com/Tatsh/wiswa-vcs/actions/workflows/tests.yml)
[![Coverage Status](https://coveralls.io/repos/github/Tatsh/wiswa-vcs/badge.svg?branch=master)](https://coveralls.io/github/Tatsh/wiswa-vcs?branch=master)
[![Dependabot](https://img.shields.io/badge/Dependabot-enabled-blue?logo=dependabot)](https://github.com/dependabot)
[![Documentation Status](https://readthedocs.org/projects/wiswa-vcs/badge/?version=latest)](https://wiswa-vcs.readthedocs.org/?badge=latest)
[![mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![uv](https://img.shields.io/badge/uv-261230?logo=astral)](https://docs.astral.sh/uv/)
[![pytest](https://img.shields.io/badge/pytest-zz?logo=Pytest&labelColor=black&color=black)](https://docs.pytest.org/en/stable/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Downloads](https://static.pepy.tech/badge/wiswa-vcs/month)](https://pepy.tech/project/wiswa-vcs)
[![Stargazers](https://img.shields.io/github/stars/Tatsh/wiswa-vcs?logo=github&style=flat)](https://github.com/Tatsh/wiswa-vcs/stargazers)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Prettier](https://img.shields.io/badge/Prettier-black?logo=prettier)](https://prettier.io/)

[![@Tatsh](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fpublic.api.bsky.app%2Fxrpc%2Fapp.bsky.actor.getProfile%2F%3Factor=did%3Aplc%3Auq42idtvuccnmtl57nsucz72&query=%24.followersCount&label=Follow+%40Tatsh&logo=bluesky&style=social)](https://bsky.app/profile/Tatsh.bsky.social)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-Tatsh-black?logo=buymeacoffee)](https://buymeacoffee.com/Tatsh)
[![Libera.Chat](https://img.shields.io/badge/Libera.Chat-Tatsh-black?logo=liberadotchat)](irc://irc.libera.chat/Tatsh)
[![Mastodon Follow](https://img.shields.io/mastodon/follow/109370961877277568?domain=hostux.social&style=social)](https://hostux.social/@Tatsh)
[![Patreon](https://img.shields.io/badge/Patreon-Tatsh2-F96854?logo=patreon)](https://www.patreon.com/Tatsh2)

<!-- WISWA-GENERATED-README:STOP -->

Library and CLI for synchronising metadata between VCS hosts.

Currently provides:

- `wiswa-sync-gh-gl` — mirror a GitHub repository's description, homepage, topics, protected
  branches and tags, and project badges to a GitLab project; trigger GitLab housekeeping.

Used as a library by [wiswa](https://github.com/Tatsh/wiswa) for GitLab project configuration
during project generation, and by the Wiswa-generated `sync-to-gitlab` GitHub Actions workflow
to keep mirrored projects in sync after every push.

## Installation

```shell
pipx install wiswa-vcs
```

## `wiswa-sync-gh-gl`

```shell
GITLAB_TOKEN=... wiswa-sync-gh-gl \
  --github-repository OWNER/REPO \
  --gitlab-repository-uri https://gitlab.com/group/project.git \
  --default-branch master
```

Most options also accept environment variables (see `wiswa-sync-gh-gl --help`), which is how the
generated GitHub Actions workflow drives the command.
