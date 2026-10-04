#!/usr/bin/env python3
"""iac-azure-agent setup CLI. Read-only. JSON on stdout.

  cli.py [--project-dir DIR] status

Reports whether this project is set up: configured repository and unset fields, the
required permission rules, and which external tools are installed. A tool that is missing
is reported as missing, never as working. Nothing is saved.
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.store import ConfigStore  # noqa: E402
from discovery.planner import FIRST_RUN_QUESTION  # noqa: E402
from lib import paths  # noqa: E402
from lib.cli_util import run  # noqa: E402
from setup import permissions  # noqa: E402

TOOLS = {
    "git": (["git", "--version"], "source control"),
    "gh": (["gh", "--version"], "GitHub access: repository inspection now; pull requests from Milestone 4. Also run `gh auth setup-git` so git can clone"),
    "az": (["az", "version", "--output", "none"], "Azure sign-in, what-if and deployment (Milestone 5)"),
    "bicep": (["bicep", "--version"], "Bicep build and lint"),
    "checkov": (["checkov", "--version"], "static security analysis of the Bicep"),
}
SETUP_FIELDS = ("repository", "infra_root", "region", "environments", "default_environment",
                "production_environments", "naming", "tagging", "deployment_auth")


def probe(name):
    cmd, purpose = TOOLS[name]
    exe = shutil.which(cmd[0])
    if not exe:
        return {"installed": False, "purpose": purpose}
    out = {"installed": True, "purpose": purpose}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        line = (p.stdout or p.stderr).strip().splitlines()
        out["runs"] = p.returncode == 0
        if line and name != "az":
            out["version"] = line[0][:120]
    except (OSError, subprocess.TimeoutExpired):
        out["runs"] = False
    return out


def handler(a):
    root = paths.resolve_project_root(a.project_dir)
    store = ConfigStore(root)
    cfg = store.load()
    repo = (cfg or {}).get("repository")
    unset = [f for f in SETUP_FIELDS if f not in (cfg or {})]
    perms = permissions.check(root)
    tools = {name: probe(name) for name in sorted(TOOLS)}
    todo = []
    if not repo:
        todo.append("Ask the first-run question, inspect the repository (repo/cli.py inspect), "
                    "and save it only after the user confirms.")
    elif "infra_root" not in (cfg or {}):
        todo.append("Confirm the infrastructure root directory with the user and save it.")
    if not perms["ok"]:
        todo.append("The required permission rules are missing or overridden; the user must "
                    "add them to their Claude Code settings. Deployment approval is refused "
                    "until they are present.")
    if not tools["gh"]["installed"]:
        todo.append("Install the GitHub CLI and run `gh auth login`; repository access cannot "
                    "be checked without it.")
    return {"project_root": root, "config_path": store.path, "first_run": not repo,
            "first_run_question": FIRST_RUN_QUESTION if not repo else None,
            "repository": repo, "unset_fields": unset, "permission_rules": perms,
            "tools": tools, "todo": todo, "ready": bool(repo) and perms["ok"]}


def main(argv=None):
    p = argparse.ArgumentParser(prog="setup/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
