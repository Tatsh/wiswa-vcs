"""
Git shell helpers used by Wiswa-driven project flows.

This module wraps the ``git`` binary via :py:mod:`asyncio.subprocess`. It is the only module
in :py:mod:`wiswa.vcs` that shells out — every other module talks to GitHub or GitLab over
HTTP.

The public surface is intentionally small and composable:

* :py:func:`changed_files` and :py:func:`diff` are read-only primitives that wrap
  ``git diff --name-only HEAD`` and ``git diff -- <path>``.
* :py:func:`restore_from_head` writes the working tree, optionally bypassing hooks, and
  falls back to ``git checkout`` when ``git restore`` fails.
* :py:func:`maybe_revert` is a conditional revert that composes the read primitives with
  a user-supplied predicate over the diff text.
* :py:func:`maybe_revert_uv_lock` is the canonical uv-lock convenience wrapper that
  combines :py:func:`maybe_revert` with an ``exclude-newer``-only predicate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
import logging
import os
import re

import anyio

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

__all__ = (
    'GIT_CONFIG_NO_HOOKS',
    'changed_files',
    'diff',
    'maybe_revert',
    'maybe_revert_uv_lock',
    'restore_from_head',
)

log = logging.getLogger(__name__)

GIT_CONFIG_NO_HOOKS: tuple[str, ...] = ('-c', f'core.hooksPath={os.devnull}')
"""``-c`` flags that point ``core.hooksPath`` at the null device.

Pass these between ``git`` and the subcommand to prevent pre-commit (or any other) hooks
from running.

:meta hide-value:
"""

_RE_UV_LOCK_EXCLUDE_NEWER = re.compile(r'^[+-]\s*exclude-newer\s*=')


def _as_anyio_path(root: anyio.Path | os.PathLike[str] | str | None) -> anyio.Path:
    if root is None:
        return anyio.Path('.')
    return anyio.Path(root) if not isinstance(root, anyio.Path) else root


async def _run(cmd: Iterable[str], *, cwd: anyio.Path) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd,
                                                cwd=str(cwd),
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout or b'', stderr or b''


async def changed_files(*, root: anyio.Path | os.PathLike[str] | str | None = None) -> set[str]:
    """
    Return the set of files that differ from ``HEAD`` in *root*'s working tree.

    Empty when *root*'s ``.git`` is missing, when the working tree matches ``HEAD``, or when
    the ``git diff`` call fails for any reason — every failure path returns an empty set so
    callers can treat "no drift" and "could not check" identically.

    Parameters
    ----------
    root : anyio.Path | os.PathLike[str] | str | None
        Working directory to inspect. Defaults to the current working directory.

    Returns
    -------
    set[str]
        Posix-style relative paths of changed files, deduplicated.
    """
    cwd = _as_anyio_path(root)
    if not await (cwd / '.git').exists():
        return set()
    rc, out, _ = await _run(('git', 'diff', '--name-only', 'HEAD'), cwd=cwd)
    if rc != 0:
        log.debug('`git diff --name-only HEAD` failed in `%s`.', cwd)
        return set()
    return {line.strip().replace('\\', '/') for line in out.decode().splitlines() if line.strip()}


async def diff(path: str | os.PathLike[str],
               *,
               root: anyio.Path | os.PathLike[str] | str | None = None) -> str:
    """
    Return the unified ``git diff`` for *path* against ``HEAD``.

    Empty string on failure or when the path is unchanged. Equivalent to
    ``git diff --no-color -a HEAD -- <path>``.

    Parameters
    ----------
    path : str | os.PathLike[str]
        File path relative to *root*.
    root : anyio.Path | os.PathLike[str] | str | None
        Working directory containing the file. Defaults to the current working directory.

    Returns
    -------
    str
        Decoded unified-diff text.
    """
    cwd = _as_anyio_path(root)
    rc, out, _ = await _run(('git', 'diff', '--no-color', '-a', 'HEAD', '--', os.fspath(path)),
                            cwd=cwd)
    if rc != 0:
        log.debug('`git diff -- %s` failed in `%s`.', path, cwd)
        return ''
    return out.decode()


async def restore_from_head(*paths: str | os.PathLike[str],
                            root: anyio.Path | os.PathLike[str] | str | None = None,
                            bypass_hooks: bool = True) -> bool:
    """
    Restore *paths* from ``HEAD`` in the staged area and the working tree.

    Tries ``git restore --source=HEAD --staged --worktree`` first and falls back to
    ``git checkout HEAD -- <paths>`` if that fails (older git versions or repository
    configurations). When *bypass_hooks* is :py:data:`True` (the default), both commands
    are invoked with :py:data:`GIT_CONFIG_NO_HOOKS` so a pre-commit hook cannot block the
    revert.

    Parameters
    ----------
    paths : str | os.PathLike[str]
        One or more file paths to restore. Empty argument list is a no-op.
    root : anyio.Path | os.PathLike[str] | str | None
        Working directory. Defaults to the current working directory.
    bypass_hooks : bool
        When :py:data:`True`, pass :py:data:`GIT_CONFIG_NO_HOOKS` so hooks do not run.

    Returns
    -------
    bool
        :py:data:`True` when one of ``git restore`` or ``git checkout`` succeeded,
        :py:data:`False` otherwise (or when *paths* is empty).
    """
    if not paths:
        return False
    cwd = _as_anyio_path(root)
    prefix: tuple[str, ...] = ('git', *GIT_CONFIG_NO_HOOKS) if bypass_hooks else ('git',)
    path_args = tuple(os.fspath(p) for p in paths)
    rc_restore, _, err = await _run(
        (*prefix, 'restore', '--source=HEAD', '--staged', '--worktree', '--', *path_args), cwd=cwd)
    if rc_restore == 0:
        return True
    log.debug('`git restore %s` failed (%s); falling back to `git checkout`.', ' '.join(path_args),
              err.decode().strip())
    rc_co, _, err_co = await _run((*prefix, 'checkout', 'HEAD', '--', *path_args), cwd=cwd)
    if rc_co == 0:
        return True
    log.warning('Could not restore %s from HEAD: %s.', ', '.join(path_args),
                err_co.decode().strip())
    return False


async def maybe_revert(path: str | os.PathLike[str],
                       *,
                       should_revert: Callable[[str], bool] | None = None,
                       root: anyio.Path | os.PathLike[str] | str | None = None,
                       bypass_hooks: bool = True) -> bool:
    """
    Conditionally restore *path* from ``HEAD``.

    Logic:

    1. If the working tree shows no changes against ``HEAD``, do nothing.
    2. If *path* is not among the changed files, do nothing.
    3. If *path* is the **only** changed file, restore it.
    4. Otherwise, fetch the diff for *path* alone and pass it to *should_revert*. Restore
       only when the predicate returns :py:data:`True`.

    When *should_revert* is :py:data:`None`, step 4 is treated as "do not revert" — a path
    that is changed alongside other files is left untouched unless the caller supplied a
    predicate that explicitly accepts the diff.

    Parameters
    ----------
    path : str | os.PathLike[str]
        Single file path (relative to *root*) to consider restoring.
    should_revert : Callable[[str], bool] | None
        Predicate that decides whether to restore when *path* is changed alongside other
        files. Receives the unified-diff text and returns :py:data:`True` to proceed.
    root : anyio.Path | os.PathLike[str] | str | None
        Working directory. Defaults to the current working directory.
    bypass_hooks : bool
        Forwarded to :py:func:`restore_from_head`.

    Returns
    -------
    bool
        :py:data:`True` when the restore was attempted and succeeded; :py:data:`False`
        otherwise.
    """
    cwd = _as_anyio_path(root)
    files = await changed_files(root=cwd)
    target = os.fspath(path).replace('\\', '/')
    if target not in files:
        return False
    if files != {target}:
        if should_revert is None:
            return False
        if not should_revert(await diff(path, root=cwd)):
            return False
    return await restore_from_head(path, root=cwd, bypass_hooks=bypass_hooks)


def _uv_lock_diff_is_only_exclude_newer(diff_text: str) -> bool:
    """
    Return :py:data:`True` when a ``uv.lock`` diff touches only ``exclude-newer`` lines.

    Parameters
    ----------
    diff_text : str
        Unified diff output, typically from ``git diff -- uv.lock``.

    Returns
    -------
    bool
        :py:data:`True` when every added or removed line matches
        ``exclude-newer = ...``; :py:data:`False` for empty diffs or any other change.
    """
    if not diff_text.strip():
        return False
    saw_exclude_newer_change = False
    for line in diff_text.splitlines():
        if line.startswith(('diff --git ', 'index ', '--- ', '+++ ', '@@', '\\')) or not line:
            continue
        marker = line[0]
        if marker == ' ':
            continue
        if marker in '+-':
            if not _RE_UV_LOCK_EXCLUDE_NEWER.match(line):
                return False
            saw_exclude_newer_change = True
            continue
        return False
    return saw_exclude_newer_change


async def maybe_revert_uv_lock(root: anyio.Path | os.PathLike[str] | str | None = None) -> bool:
    """
    Restore ``uv.lock`` from ``HEAD`` when its drift is purely incidental.

    Convenience wrapper around :py:func:`maybe_revert` with the
    ``exclude-newer``-only predicate. ``uv lock --upgrade`` can rewrite ``uv.lock`` without
    any meaningful project change (for example when the user ``uv.toml`` rolls the
    ``exclude-newer`` cut-off forward); this restores the lock so the run does not leave
    an incidental diff.

    Parameters
    ----------
    root : anyio.Path | os.PathLike[str] | str | None
        Working directory containing ``uv.lock`` and ``.git``. Defaults to the current
        working directory.

    Returns
    -------
    bool
        :py:data:`True` when ``uv.lock`` was restored, :py:data:`False` otherwise.
    """
    return await maybe_revert('uv.lock',
                              should_revert=_uv_lock_diff_is_only_exclude_newer,
                              root=root)
