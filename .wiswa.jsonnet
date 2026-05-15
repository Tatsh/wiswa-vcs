local utils = import 'utils.libsonnet';

{
  uses_user_defaults: true,
  project_name: 'wiswa-vcs',
  version: '0.0.0',
  description: 'Cross-host VCS metadata sync and mirroring helpers used by Wiswa.',
  keywords: ['command line', 'github', 'gitlab', 'mirror', 'sync', 'vcs'],
  security_policy_supported_versions: { '0.0.x': ':white_check_mark:' },
  python_deps+: {
    main+: {
      anyio: utils.latestPypiPackageVersionCaret('anyio'),
      bascom: utils.latestPypiPackageVersionCaret('bascom'),
      click: utils.latestPypiPackageVersionCaret('click'),
      gidgethub: utils.latestPypiPackageVersionCaret('gidgethub'),
      gidgetlab: utils.latestPypiPackageVersionCaret('gidgetlab'),
      niquests: utils.latestPypiPackageVersionCaret('niquests'),
    },
    tests+: {
      'pytest-asyncio': utils.latestPypiPackageVersionCaret('pytest-asyncio'),
    },
    docs+: {
      'sphinx-click': utils.latestPypiPackageVersionCaret('sphinx-click'),
    },
  },
  pyproject+: {
    project+: {
      scripts+: {
        'wiswa-sync-gh-gl': 'wiswa.vcs.commands.sync_gh_gl:main',
      },
    },
    tool+: {
      pytest+: {
        ini_options+: {
          asyncio_mode: 'auto',
        },
      },
    },
  },
}
