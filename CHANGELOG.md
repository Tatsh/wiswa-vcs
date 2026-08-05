<!-- markdownlint-configure-file {"MD024": { "siblings_only": true } } -->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.1/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

### Fixed

- `wiswa-sync-gh-gl` no longer abandons the whole sync when GitLab refuses one part of it.
  - Project settings rejected with a `400` or `422` (for example
    `Pages access level is not allowed for the project visibility level`) are identified from the
    error, dropped from the `PUT /projects/:id` body, and the rest are retried, so the description,
    topics, and homepage still apply. A refusal that names no recognisable setting abandons only
    the settings update.
  - Push rule and merge-request approval failures are logged and skipped.
  - A refusal of branch protection, tag protection, badge synchronisation, or housekeeping with a
    `400`, `403`, `404`, or `422` is logged, and the remaining steps still run; any other response
    to those steps, such as a `401` or a server error, still aborts the sync.
- `wiswa-sync-gh-gl` reports GitHub and GitLab API failures as a single message instead of a full
  traceback. Pass `--debug` for the traceback.

## [0.1.0] - 2026-06-01

### Added

- Two opt-in keyword-only parameters on `github.configure_project`, both defaulting to off:
  - `sha_pinning_required` requires GitHub Actions to be pinned to a full-length commit SHA.
  - `immutable_oidc_subject` opts in to the immutable OIDC subject claim format.

### Fixed

- Ruleset synchronisation no longer aborts the whole configure flow when GitHub rejects a ruleset
  with a `422` whose `errors` payload is a list of strings (as the rulesets endpoint returns),
  which made gidgethub raise `TypeError` instead of `HTTPException`; such failures are now logged
  and the remaining rulesets still apply.

## [0.0.1] - 2026-05-21

### Added

- `wiswa-sync-gh-gl` command and supporting library for mirroring a GitHub repository's
  description, homepage, topics, protected branches and tags, and project badges to a GitLab
  project, and for triggering GitLab housekeeping afterwards.
- GitHub configure flow and release helpers in `wiswa.vcs.github`, consolidating the
  authentication and configuration entry points previously scattered across separate modules.
- GitLab configure flow in `wiswa.vcs.gitlab`, with the same consolidated authentication and
  configuration shape as the GitHub side.
- `--gitlab-config` option on `wiswa-sync-gh-gl` that accepts either an inline JSON document or a
  path to a JSON file on disk.
- TypedDict-based typing surface for cross-host API payloads, including a typed `ProjectSettings`
  contract used by the sync layer.
- Sphinx-click-based documentation for `wiswa-sync-gh-gl`.

### Changed

- Repackaged the project as the implicit `wiswa` namespace, moving the code from `wiswa_vcs` to
  `wiswa.vcs` and aligning the wheel build target with the namespace layout.
- Adopted the shared `wiswa.typing` namespace for cross-package type definitions.
- Broadened `sync.badges_file` to accept any `anyio.Path` or `os.PathLike`, so callers no longer
  need to materialise a concrete path type.
- The `USER_AGENT` sent to both GitHub and GitLab now carries the installed package version.
- `gitlab.parse_badges` now yields badges lazily instead of returning a fully materialised list.

[unreleased]: https://github.com/Tatsh/wiswa-vcs/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Tatsh/wiswa-vcs/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/Tatsh/wiswa-vcs/releases/tag/v0.0.1
