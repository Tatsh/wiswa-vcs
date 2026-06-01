local utils = import 'utils.libsonnet';

{
  uses_user_defaults: true,
  project_name: 'wiswa-vcs',
  version: '0.1.0',
  description: 'Cross-host VCS metadata sync and mirroring helpers used by Wiswa.',
  keywords: ['command line', 'github', 'gitlab', 'mirror', 'sync', 'vcs'],
  primary_module: 'wiswa',
  primary_module_qualified: 'wiswa.vcs',
  want_main: true,
  want_flatpak: false,
  publishing+: { flathub: 'sh.tat.wiswa-vcs' },
  want_snap: false,
  appimage+: {
    exclusions: ['wiswa-sync-gh-gl'],
  },
  pyinstaller+: {
    macos_exclusions: ['wiswa-sync-gh-gl'],
    windows_exclusions: ['wiswa-sync-gh-gl'],
  },
  python_deps+: {
    main+: {
      anyio: utils.latestPypiPackageVersionCaret('anyio'),
      gidgethub: utils.latestPypiPackageVersionCaret('gidgethub'),
      gidgetlab: utils.latestPypiPackageVersionCaret('gidgetlab'),
      keyring: utils.latestPypiPackageVersionCaret('keyring'),
      niquests: utils.latestPypiPackageVersionCaret('niquests'),
      packaging: utils.latestPypiPackageVersionCaret('packaging'),
      platformdirs: utils.latestPypiPackageVersionCaret('platformdirs'),
      'wiswa-typing': utils.latestPypiPackageVersionCaret('wiswa-typing'),
    },
    tests+: {
      'pytest-asyncio': utils.latestPypiPackageVersionCaret('pytest-asyncio'),
    },
  },
  pyproject+: {
    project+: {
      scripts: {
        'wiswa-sync-gh-gl': 'wiswa.vcs.commands.sync_gh_gl:main',
      },
    },
    tool+: {
      commitizen+: {
        version_files: [
          '.wiswa.jsonnet',
          'CITATION.cff',
          'README.md',
          'docs/badges.rst',
          'docs/index.rst',
          'man/wiswa-vcs.1',
          'package.json',
          'wiswa/vcs/__init__.py',
        ],
      },
      pytest+: {
        ini_options+: {
          asyncio_mode: 'auto',
        },
      },
      uv+: {
        'exclude-newer-package': {
          'wiswa-typing': false,
        },
      },
    },
  },
}
