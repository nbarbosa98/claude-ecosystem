#!/usr/bin/env python3
"""iac-azure-agent config CLI. Output is JSON on stdout; exit codes in lib/errors.py.

  cli.py [--project-dir DIR] show
  cli.py [--project-dir DIR] path
  cli.py [--project-dir DIR] set KEY VALUE
  cli.py [--project-dir DIR] unset KEY
  cli.py [--project-dir DIR] set-repo REPO [--default-branch B] [--confirm-switch-from CURRENT]
  cli.py [--project-dir DIR] clear --confirm-project PROJECT_ROOT

KEY: infra_root, region, environments (comma list), default_environment,
production_environments (comma list), deployment_auth, repository.default_branch,
naming.<name>, tagging.<name>, preferences.<name>.
The project is the nearest ancestor of --project-dir (default: cwd) that contains .git.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import schema  # noqa: E402
from config.store import ConfigStore  # noqa: E402
from lib import paths  # noqa: E402
from lib.cli_util import run  # noqa: E402


def handler(a):
    store = ConfigStore(paths.resolve_project_root(a.project_dir))
    base = {"project_root": store.project_root, "config_path": store.path}
    if a.cmd == "path":
        return dict(base, store_root=paths.store_root())
    if a.cmd == "show":
        cfg = store.load()
        unset = [] if cfg is None else sorted(
            f for f in ("repository", "infra_root", "region", "environments",
                        "default_environment", "production_environments", "naming",
                        "tagging", "deployment_auth") if f not in cfg)
        return dict(base, configured=cfg is not None, config=cfg,
                    unset_fields=unset if cfg is not None else None,
                    auth_methods=list(schema.AUTH_METHODS))
    if a.cmd == "set":
        return dict(base, saved=True, config=store.set_value(a.key, a.value))
    if a.cmd == "unset":
        return dict(base, saved=True, config=store.unset_value(a.key))
    if a.cmd == "set-repo":
        cfg, previous = store.set_repo(a.repo, a.confirm_switch_from, a.default_branch)
        return dict(base, saved=True, previous_repository=previous,
                    repository=cfg["repository"], config=cfg)
    if a.cmd == "clear":
        return dict(base, deleted=store.clear(a.confirm_project))
    raise AssertionError(a.cmd)


def main(argv=None):
    p = argparse.ArgumentParser(prog="config/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("path")
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("value")
    u = sub.add_parser("unset")
    u.add_argument("key")
    r = sub.add_parser("set-repo")
    r.add_argument("repo")
    r.add_argument("--default-branch")
    r.add_argument("--confirm-switch-from")
    c = sub.add_parser("clear")
    c.add_argument("--confirm-project", required=True)
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
