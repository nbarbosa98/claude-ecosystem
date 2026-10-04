#!/usr/bin/env python3
"""iac-azure-agent validation CLI. JSON on stdout; exit codes in lib/errors.py.

  cli.py [--project-dir DIR] run [ID]    run every check on the working copy
  cli.py tools                           which validation tools are installed

Checks: structure, bicep-only, secret-scan, bicep-build, bicep-build-params, bicep-lint,
security-scan (Checkov). Each result is passed, failed, warning, skipped or unavailable.
With ID (a request in VALIDATION) the results are recorded on the request together with a
hash of the files that were checked, replacing earlier results. Without ID nothing is saved.

The exit code is 0 whenever the checks ran, whatever they found; read `verdict`.
Nothing here contacts Azure.
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
from validate import checks  # noqa: E402
from workspace.manager import BRANCH_PREFIX, Workspace  # noqa: E402


def handler(a):
    if a.cmd == "tools":
        return {"tools": checks.tool_versions()}
    root = paths.resolve_project_root(a.project_dir)
    ws = Workspace(root, ConfigStore(root).load())
    ws.require()
    store = StateStore(root)
    if a.id:
        rec = store.load(a.id)
        if rec["state"] != "VALIDATION":
            raise Refused("results are recorded in VALIDATION (request is in %s)" % rec["state"])
        if ws.branch() != BRANCH_PREFIX + a.id:
            raise Refused("the working copy is on %r, not on this request's branch %s"
                          % (ws.branch(), BRANCH_PREFIX + a.id))
    before = ws.tree_hash()
    results = checks.run_all(ws)
    tree = ws.tree_hash()
    if tree != before:
        raise Refused("files under the infrastructure root changed while validation ran; "
                      "nothing was recorded, run it again")
    versions = checks.tool_versions()
    out = {"results": results, "tree_hash": tree, "tools": versions, "not_run": checks.NOT_RUN,
           "branch": ws.branch(), "recorded": False}
    if a.id:
        def apply(r, envs):
            for c in r["validation"]:
                c["stale"] = True
            for res in results:
                detail = res["detail"]
                if res["findings"]:
                    detail += " | " + "; ".join(_line(f) for f in res["findings"][:10])
                machine.add_validation(r, res["check"], res["result"], detail[:machine.MAX_TEXT])
            machine.set_validation_run(r, tree, versions)
        rec, _ = store.mutate(a.id, apply)
        out["recorded"] = True
        out["id"] = a.id
        summary = machine.summarise_checks(rec["validation"])
    else:
        summary = machine.summarise_checks([{"check": r["check"], "result": r["result"]} for r in results])
    out["verdict"] = summary["verdict"]
    out["counts"] = summary["counts"]
    out["meaning"] = {
        "passed": "every check ran and passed",
        "passed_with_warnings": "every check ran; review the warnings",
        "failed": "at least one check failed; fix it in IMPLEMENTATION and run again",
        "incomplete": "at least one check was skipped or unavailable. That is not a pass.",
    }.get(summary["verdict"], "no results")
    return out


def _line(f):
    where = "%s:%s" % (f["file"], f.get("line")) if f.get("file") else ""
    what = f.get("code") or f.get("kind") or ""
    return " ".join(x for x in (where, what, f.get("message", "")) if x)


def main(argv=None):
    p = argparse.ArgumentParser(prog="validate/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("id", nargs="?")
    sub.add_parser("tools")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
