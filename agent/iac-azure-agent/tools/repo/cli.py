#!/usr/bin/env python3
"""iac-azure-agent repository CLI. Read-only. JSON on stdout; exit codes in lib/errors.py.

  cli.py [--project-dir DIR] inspect [REPO]

REPO is owner/name or a github.com URL; without it the configured repository is inspected.
Reports access, default branch, existing Bicep, parameter files, workflows, READMEs,
candidate infrastructure directories, and a proposed layout when there is no Bicep yet.
Exit 5 means the check could not be made (gh missing, not signed in, network): nothing
was verified. It never saves anything; use config/cli.py set-repo after the user confirms.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.store import ConfigStore  # noqa: E402
from lib import paths, repo_id  # noqa: E402
from lib.cli_util import run  # noqa: E402
from lib.errors import Refused  # noqa: E402
from repo import inspector  # noqa: E402


def handler(a):
    root = paths.resolve_project_root(a.project_dir)
    cfg = ConfigStore(root).load() or {}
    current = cfg.get("repository")
    if a.repo:
        target = repo_id.parse(a.repo)
    elif current:
        target = {k: current[k] for k in ("owner", "name", "slug", "url")}
    else:
        raise Refused("no repository given and none configured for this project")
    out = inspector.inspect(target)
    out["project_root"] = root
    out["configured_repository"] = current["slug"] if current else None
    out["is_configured_repository"] = bool(current) and current["slug"] == target["slug"]
    out["configured_infra_root"] = cfg.get("infra_root")
    if current and current["slug"] != target["slug"]:
        out["switch_required"] = ("This project is configured for %s. Saving %s is a switch and "
                                  "needs the user's explicit confirmation." % (current["slug"], target["slug"]))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="repo/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inspect").add_argument("repo", nargs="?")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
