# wiswa-typing migration TODOs

Tracks inline types in `wiswa-vcs` that should be replaced with shared types from the
`wiswa-typing` package once a new `wiswa-typing` release publishes them. Every entry below
also has a matching `# TODO(wiswa-typing):` comment in the source file so `grep` can find
the call sites.

When a `wiswa-typing` release lands:

1. Bump the `wiswa-typing` lower bound in `pyproject.toml` and `.wiswa.jsonnet`.
2. Resolve each entry below — import the published name, drop the inline declaration,
   remove the `# TODO(wiswa-typing):` comment.
3. Delete the entry from this file.

## Pending

| File                     | Symbol / inline type                                             | Proposed `wiswa.typing` name           | Notes                                                                                                                                                                                                                                |
| ------------------------ | ---------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `wiswa/vcs/github.py`    | `Literal['legacy', 'workflow']` return of `get_pages_build_type` | `wiswa.typing.github.PagesBuildType`   | One-line `TypeAlias` next to the existing `Literal` aliases in `wiswa/typing/github.py`.                                                                                                                                             |
| `wiswa/vcs/configure.py` | `_DESIRED_GITHUB_RULESETS: list[dict[str, Any]]`                 | `list[wiswa.typing.github.Ruleset]`    | A `Ruleset` TypedDict covering `name`, `target` (`Literal['tag', 'branch']`), `enforcement`, `bypass_actors`, `conditions`, and `rules`. Rule shapes vary by `type`; either union of TypedDicts or a single `total=False` TypedDict. |
| `wiswa/vcs/configure.py` | `_github_repo_config(settings) -> dict[str, object]`             | `wiswa.typing.github.RepositoryConfig` | TypedDict covering the `PATCH /repos/:slug` body (allow*\*, has*_, security*and_analysis, squash_merge*_, etc.). Mostly `bool`s, a few enums and a nested `SecurityAndAnalysis` TypedDict.                                           |
