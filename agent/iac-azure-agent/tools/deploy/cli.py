#!/usr/bin/env python3
"""iac-azure-agent Azure CLI. JSON on stdout; exit codes in lib/errors.py.

  cli.py context
      Which tenant, subscription and identity az is signed in as. Read-only.
  cli.py [--project-dir DIR] plan ID --environment ENV --tenant GUID --subscription GUID
                                     [--location REGION] [--resource-group RG]
      Azure-side validation and what-if for the published code. Records the target and the
      change set on the request. Changes nothing in Azure.
  cli.py [--project-dir DIR] deploy ID
      The only command that changes Azure. Runs only in DEPLOYMENT with a valid approval,
      and only if a fresh what-if still equals the approved change set.
  cli.py [--project-dir DIR] status ID [--record]
      The real state of the request's deployment in Azure. With --record, an interrupted
      deploy step is resolved from Azure's answer.
  cli.py [--project-dir DIR] verify ID
      Reads each planned resource back from Azure and records verification checks.

--tenant and --subscription are the ones the user confirmed; az must already be signed in
to them. The tool never signs in, switches subscription, grants access or deletes anything.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.store import ConfigStore  # noqa: E402
from deploy import executor  # noqa: E402
from deploy.azrun import Az  # noqa: E402
from lib import paths  # noqa: E402
from lib.cli_util import run  # noqa: E402
from state.store import StateStore  # noqa: E402
from workspace.manager import Workspace  # noqa: E402


def handler(a):
    if a.cmd == "context":
        return {"context": Az().context()}
    root = paths.resolve_project_root(a.project_dir)
    cfg = ConfigStore(root).load()
    ws = Workspace(root, cfg)
    store = StateStore(root)
    rec = store.load(a.id)
    az = Az(cwd=ws.dir)
    if a.cmd == "plan":
        return executor.plan(az, ws, store, rec, a.environment, a.tenant, a.subscription,
                             a.location or (cfg or {}).get("region"), a.resource_group)
    if a.cmd == "deploy":
        return executor.deploy(az, ws, store, rec)
    if a.cmd == "status":
        return executor.status(az, rec, store, a.record)
    if a.cmd == "verify":
        return executor.verify(az, store, rec)
    raise AssertionError(a.cmd)


def main(argv=None):
    p = argparse.ArgumentParser(prog="deploy/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("context")
    s = sub.add_parser("plan")
    s.add_argument("id")
    s.add_argument("--environment", required=True)
    s.add_argument("--tenant", required=True)
    s.add_argument("--subscription", required=True)
    s.add_argument("--location")
    s.add_argument("--resource-group")
    sub.add_parser("deploy").add_argument("id")
    s = sub.add_parser("status")
    s.add_argument("id")
    s.add_argument("--record", action="store_true")
    sub.add_parser("verify").add_argument("id")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
