#!/usr/bin/env python3
"""iac-azure-agent working-copy CLI. JSON on stdout; exit codes in lib/errors.py.

  cli.py [--project-dir DIR] status           where the working copy is and what state it is in
  cli.py [--project-dir DIR] clone            clone the configured repository under the project
  cli.py [--project-dir DIR] sync             fetch; fast-forward the default branch if clean
  cli.py [--project-dir DIR] begin ID         create or return to the local branch iac/<ID>
  cli.py [--project-dir DIR] inventory        existing Bicep: files, modules, resource types
  cli.py [--project-dir DIR] record-files ID  record the files changed for a request

The working copy is <project>/.iac-azure-agent/workspace/<owner>--<name>/. Nothing here
commits or pushes, and nothing discards local changes. record-files takes the list from
git, not from the caller, and refuses when anything outside the infrastructure root changed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.store import ConfigStore  # noqa: E402
from lib import paths  # noqa: E402
from lib.cli_util import run  # noqa: E402
from lib.errors import Refused  # noqa: E402
from state import machine  # noqa: E402
from state.store import StateStore  # noqa: E402
from workspace import bicep_scan  # noqa: E402
from workspace.manager import BRANCH_PREFIX, Workspace  # noqa: E402


def record_files(ws, store, rec_id):
    ws.require()
    rec = store.load(rec_id)
    if rec["state"] != "IMPLEMENTATION":
        raise Refused("files are recorded in IMPLEMENTATION (request is in %s)" % rec["state"])
    if ws.branch() != BRANCH_PREFIX + rec_id:
        raise Refused("the working copy is on %r, not on this request's branch %s; run begin %s"
                      % (ws.branch(), BRANCH_PREFIX + rec_id, rec_id))
    changes = ws.changes()
    outside = [p for p, _ in changes if not ws.inside_infra(p)]
    if outside:
        raise Refused("files outside the infrastructure root %r were changed: %s. Nothing was "
                      "recorded. Revert them yourself or ask the user; the agent writes only "
                      "under the infrastructure root."
                      % (ws.infra_root, ", ".join(bicep_scan.clean(p) for p in outside[:20])))
    if not changes:
        raise Refused("no changed files in the working copy; nothing to record")

    def apply(r, envs):
        r["files"] = []
        for path, action in changes:
            machine.add_file(r, path, action)
    rec, _ = store.mutate(rec_id, apply)
    return {"id": rec_id, "recorded": rec["files"], "tree_hash": ws.tree_hash(),
            "branch": ws.branch()}


def handler(a):
    root = paths.resolve_project_root(a.project_dir)
    ws = Workspace(root, ConfigStore(root).load())
    if a.cmd == "status":
        return ws.status()
    if a.cmd == "clone":
        return ws.clone()
    if a.cmd == "sync":
        return ws.sync()
    if a.cmd == "begin":
        store = StateStore(root)
        rec = store.load(a.id)
        if rec["status"] != "active" or rec["state"] != "IMPLEMENTATION":
            raise Refused("a work branch is created in IMPLEMENTATION, after the architecture "
                          "is approved (request is %s, %s)" % (rec["state"], rec["status"]))
        return ws.begin(a.id)
    if a.cmd == "inventory":
        ws.require()
        files = ws.infra_files() if os.path.isdir(ws.infra_dir()) else []
        out = bicep_scan.inventory(ws.dir, files)
        out.update(path=ws.dir, infra_root=ws.infra_root, branch=ws.branch(),
                   infra_root_exists=os.path.isdir(ws.infra_dir()))
        return out
    if a.cmd == "record-files":
        return record_files(ws, StateStore(root), a.id)
    raise AssertionError(a.cmd)


def main(argv=None):
    p = argparse.ArgumentParser(prog="workspace/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "clone", "sync", "inventory"):
        sub.add_parser(name)
    for name in ("begin", "record-files"):
        sub.add_parser(name).add_argument("id")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
