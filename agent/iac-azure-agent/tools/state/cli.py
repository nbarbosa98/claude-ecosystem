#!/usr/bin/env python3
"""iac-azure-agent workflow state CLI. Output is JSON on stdout; exit codes in lib/errors.py.

Records and moves:
  create --intent TEXT                      list
  show ID                                   resume ID
  advance ID --to STATE [--accept-incomplete]
  back ID --to STATE --reason TEXT
  complete ID [--accept-incomplete]         cancel ID --reason TEXT
Approvals (the hash is computed by the tool; the caller echoes the short hash the user saw):
  approve ID --kind architecture|deployment --confirm SHORT_HASH
  confirm-risk ID --phrase "ACCEPT-RISK <hash> <flags>"
Steps:
  step-start ID --name TEXT                 step-end ID --result succeeded|failed [--detail TEXT]
Data:
  add-requirement ID --text T               add-question ID --text T
  answer ID --question Q --answer T         defer ID --question Q --assumption T
  add-assumption ID --text T                resolve-assumption ID --assumption A --confirm|--reject
  set-architecture ID (--json J | --file F) set-target ID --tenant G --subscription G --resource-group RG --environment E
  add-file ID --path P --action created|modified|deleted
  add-validation ID --check C --result R [--detail T]
  add-verification ID --check C --result R [--detail T]
  set-git ID [--branch B] [--commit SHA] [--pr-url URL]
  set-plan ID (--json J | --file F)         set-deployment ID --status S [--detail T]

All commands accept --project-dir DIR (default: cwd; the project is the nearest ancestor
containing .git).
"""
import argparse
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import paths  # noqa: E402
from lib.cli_util import load_json_arg, run  # noqa: E402
from state import machine  # noqa: E402
from state.store import StateStore  # noqa: E402

M = machine


def view(rec, envs):
    return {"summary": M.summary(rec, envs), "record": rec}


MUTATIONS = {
    "advance": lambda a, r, e: M.advance(r, a.to, a.accept_incomplete, e),
    "back": lambda a, r, e: M.back(r, a.to, a.reason, e),
    "complete": lambda a, r, e: M.complete(r, a.accept_incomplete),
    "cancel": lambda a, r, e: M.cancel(r, a.reason),
    "approve": lambda a, r, e: M.approve(r, a.kind, a.confirm, e),
    "confirm-risk": lambda a, r, e: M.confirm_high_risk(r, a.phrase, e),
    "step-start": lambda a, r, e: M.step_start(r, a.name),
    "step-end": lambda a, r, e: M.step_end(r, a.result, a.detail),
    "add-requirement": lambda a, r, e: M.add_requirement(r, a.text),
    "add-question": lambda a, r, e: M.add_question(r, a.text),
    "answer": lambda a, r, e: M.answer_question(r, a.question, a.answer),
    "defer": lambda a, r, e: M.defer_question(r, a.question, a.assumption),
    "add-assumption": lambda a, r, e: M.add_assumption(r, a.text),
    "resolve-assumption": lambda a, r, e: M.resolve_assumption(r, a.assumption, a.confirm),
    "set-architecture": lambda a, r, e: M.set_architecture(r, load_json_arg(a.json, a.file)),
    "set-target": lambda a, r, e: M.set_target(r, a.tenant, a.subscription, a.resource_group, a.environment),
    "add-file": lambda a, r, e: M.add_file(r, a.path, a.action),
    "add-validation": lambda a, r, e: M.add_validation(r, a.check, a.result, a.detail),
    "add-verification": lambda a, r, e: M.add_verification(r, a.check, a.result, a.detail),
    "set-git": lambda a, r, e: M.set_git(r, a.branch, a.commit, a.pr_url),
    "set-plan": lambda a, r, e: M.set_plan(r, load_json_arg(a.json, a.file)),
    "set-deployment": lambda a, r, e: M.set_deployment(r, a.status, a.detail),
}


def handler(a):
    store = StateStore(paths.resolve_project_root(a.project_dir))
    if a.cmd == "create":
        rec = store.create(a.intent)
        return view(rec, store.production_envs())
    if a.cmd == "list":
        return {"project_root": store.project_root, "requests": store.list()}
    if a.cmd == "show":
        envs = store.production_envs()
        # Display what the rules say now; the stored record is updated by the next change.
        rec = M.reconcile(copy.deepcopy(store.load(a.id)), envs)
        return view(rec, envs)
    if a.cmd == "resume":
        holder = {}

        def fn(rec, envs):
            holder["interrupted"] = M.resume(rec)[1]
        rec, _ = store.mutate(a.id, fn)
        out = view(rec, store.production_envs())
        out["interrupted_step_found"] = holder["interrupted"]
        return out
    rec, _ = store.mutate(a.id, lambda r, e: MUTATIONS[a.cmd](a, r, e))
    return view(rec, store.production_envs())


def main(argv=None):
    p = argparse.ArgumentParser(prog="state/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, with_id=True):
        s = sub.add_parser(name)
        if with_id:
            s.add_argument("id")
        return s

    cmd("create", False).add_argument("--intent", required=True)
    cmd("list", False)
    cmd("show")
    cmd("resume")
    s = cmd("advance")
    s.add_argument("--to", required=True)
    s.add_argument("--accept-incomplete", action="store_true")
    s = cmd("back")
    s.add_argument("--to", required=True)
    s.add_argument("--reason", required=True)
    cmd("complete").add_argument("--accept-incomplete", action="store_true")
    cmd("cancel").add_argument("--reason", required=True)
    s = cmd("approve")
    s.add_argument("--kind", required=True, choices=("architecture", "deployment"))
    s.add_argument("--confirm", required=True)
    cmd("confirm-risk").add_argument("--phrase", required=True)
    cmd("step-start").add_argument("--name", required=True)
    s = cmd("step-end")
    s.add_argument("--result", required=True, choices=("succeeded", "failed"))
    s.add_argument("--detail")
    for name in ("add-requirement", "add-question", "add-assumption"):
        cmd(name).add_argument("--text", required=True)
    s = cmd("answer")
    s.add_argument("--question", required=True)
    s.add_argument("--answer", required=True)
    s = cmd("defer")
    s.add_argument("--question", required=True)
    s.add_argument("--assumption", required=True)
    s = cmd("resolve-assumption")
    s.add_argument("--assumption", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--confirm", dest="confirm", action="store_true")
    g.add_argument("--reject", dest="confirm", action="store_false")
    for name in ("set-architecture", "set-plan"):
        s = cmd(name)
        s.add_argument("--json")
        s.add_argument("--file")
    s = cmd("set-target")
    for f in ("--tenant", "--subscription", "--resource-group", "--environment"):
        s.add_argument(f, required=True)
    s = cmd("add-file")
    s.add_argument("--path", required=True)
    s.add_argument("--action", required=True)
    for name in ("add-validation", "add-verification"):
        s = cmd(name)
        s.add_argument("--check", required=True)
        s.add_argument("--result", required=True)
        s.add_argument("--detail")
    s = cmd("set-git")
    s.add_argument("--branch")
    s.add_argument("--commit")
    s.add_argument("--pr-url")
    s = cmd("set-deployment")
    s.add_argument("--status", required=True)
    s.add_argument("--detail")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
