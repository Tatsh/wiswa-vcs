local utils = import 'utils.libsonnet';

{
  uses_user_defaults: true,
  project_name: 'wiswa-vcs',
  version: '0.0.0',
  description: 'Cross-host VCS metadata sync and mirroring helpers used by Wiswa.',
  keywords: ['command line', 'github', 'gitlab', 'mirror', 'sync', 'vcs'],
  primary_module: 'wiswa',
  primary_module_qualified: 'wiswa.vcs',
  want_appimage: false,
  want_pyinstaller: false,
  want_main: true,
  want_flatpak: false,
  python_deps+: {
    main+: {
      anyio: utils.latestPypiPackageVersionCaret('anyio'),
      gidgethub: utils.latestPypiPackageVersionCaret('gidgethub'),
      gidgetlab: utils.latestPypiPackageVersionCaret('gidgetlab'),
      keyring: utils.latestPypiPackageVersionCaret('keyring'),
      niquests: utils.latestPypiPackageVersionCaret('niquests'),
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
