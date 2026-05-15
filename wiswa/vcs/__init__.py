"""wiswa-vcs package: cross-host VCS metadata sync helpers used by Wiswa."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = ('__version__',)

try:
    __version__ = version('wiswa-vcs')
except PackageNotFoundError:
    __version__ = '0.0.0'
