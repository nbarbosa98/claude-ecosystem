#!/usr/bin/env python3
"""iac-azure-agent discovery CLI. Read-only: it changes no record. JSON on stdout.

  cli.py [--project-dir DIR] next ID        the next small batch of questions for a request
  cli.py [--project-dir DIR] proposal ID    the stored proposal rendered for the user
  cli.py topics                             every topic in the catalog
  cli.py template                           an empty architecture proposal to fill in
"""
import argparse
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.store import ConfigStore  # noqa: E402
from discovery import catalog, planner, proposal  # noqa: E402
from lib import paths  # noqa: E402
from lib.cli_util import run  # noqa: E402
from state import machine  # noqa: E402
from state.store import StateStore  # noqa: E402


def handler(a):
    if a.cmd == "topics":
        return {"categories": list(catalog.CATEGORIES), "topics": catalog.TOPICS}
    if a.cmd == "template":
        return {"template": proposal.template(),
                "required": [k for k, _ in proposal.TEXT_SECTIONS] + ["resources", "cost", "risks", "unresolved"]}
    root = paths.resolve_project_root(a.project_dir)
    store = StateStore(root)
    envs = store.production_envs()
    rec = machine.reconcile(copy.deepcopy(store.load(a.id)), envs)
    if a.cmd == "next":
        config = ConfigStore(root).load()
        out = planner.plan(rec, config, envs)
        out.update(id=rec["id"], state=rec["state"],
                   assumptions_hash=machine.summary(rec, envs)["assumptions_hash"],
                   assumptions=[{"id": x["id"], "text": x["text"]} for x in rec["assumptions"]
                                if x["status"] == "assumed"])
        if rec["state"] != "DISCOVERY":
            out["note"] = "request is in %s, not DISCOVERY" % rec["state"]
        return out
    if a.cmd == "proposal":
        pending = machine.pending_approval(rec, envs)
        h = pending["hash"] if pending and pending["kind"] == "architecture" else None
        return {"id": rec["id"], "state": rec["state"],
                "problems": proposal.problems(rec.get("architecture")) if rec.get("architecture") else ["no proposal recorded"],
                "approval_hash": h, "markdown": proposal.render(rec, h)}
    raise AssertionError(a.cmd)


def main(argv=None):
    p = argparse.ArgumentParser(prog="discovery/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next").add_argument("id")
    sub.add_parser("proposal").add_argument("id")
    sub.add_parser("topics")
    sub.add_parser("template")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
