#!/usr/bin/env python3
"""iac-azure-agent GitHub CLI. JSON on stdout; exit codes in lib/errors.py.

  cli.py [--project-dir DIR] preview ID
      What would be published: files, branch, validation verdict. Changes nothing.
  cli.py [--project-dir DIR] publish ID --message TEXT [--title TEXT] [--notes TEXT]
      Commit the validated files on iac/<ID>, push, read the remote back, open a pull
      request into the default branch, and record branch, commit and pull request.
  cli.py [--project-dir DIR] verify ID
      Read GitHub again: does the remote branch still match the recorded commit, and what
      state is the pull request in. Changes nothing.

publish never touches the default branch, never forces, never merges. It refuses unless
validation ran on exactly the files being committed. `verified` in the output lists what
was read back from GitHub; anything not listed there was not confirmed.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.store import ConfigStore  # noqa: E402
from github.publisher import Publisher, check_message, pr_body, preflight  # noqa: E402
from lib import paths  # noqa: E402
from lib.cli_util import run  # noqa: E402
from lib.errors import Refused, ToolError  # noqa: E402
from setup import permissions  # noqa: E402
from state import machine  # noqa: E402
from state.store import StateStore  # noqa: E402
from workspace.manager import Workspace  # noqa: E402

PR_URL_PREFIX = "https://github.com/"


def publish(a, ws, store, rec):
    perms = permissions.check(store.project_root)
    if not perms["ok"]:
        raise Refused("publishing is refused until the required permission rules are in the "
                      "user's Claude Code settings: %s"
                      % "; ".join(perms["missing_rules"] + perms["conflicts"] + perms["problems"]))
    message = check_message(a.message)
    title = check_message(a.title or message.splitlines()[0]).splitlines()[0]
    facts = preflight(ws, rec)
    body = pr_body(rec, facts, a.notes)
    pub = Publisher(ws)
    branch = facts["branch"]
    out = {"id": rec["id"], "repository": ws.repo["slug"], "branch": branch,
           "base": ws.default_branch, "files": [{"path": p, "action": x} for p, x in facts["changes"]],
           "validation": facts["validation"]["verdict"], "verified": [], "not_done": []}

    store.mutate(rec["id"], lambda r, e: machine.step_start(r, "publish"))
    try:
        commit = pub.commit(message)
        out["commit"] = commit
        if ws.tree_hash() != facts["tree_hash"]:
            raise Refused("the files changed while publishing; nothing was pushed")
        pub.push(branch, commit)
        out["verified"].append("remote branch %s is at %s" % (branch, commit))
        published = {"tree_hash": facts["tree_hash"], "remote_verified_at": machine.now()}
        store.mutate(rec["id"], lambda r, e: machine.set_git(r, branch, commit, None, published))
        pr, created = pub.open_pr(branch, title, body)
        url = str(pr.get("url") or "")
        if not url.startswith(PR_URL_PREFIX + ws.repo["slug"] + "/pull/"):
            raise ToolError("GitHub returned an unexpected pull request address; it was not recorded")
        out["pull_request"] = {"url": url, "created": created, "head": pr.get("headRefOid"),
                               "base": pr.get("baseRefName")}
        if pr.get("headRefOid") == commit:
            out["verified"].append("pull request %s is open at %s" % (url, commit))
        else:
            out["not_done"].append("the pull request head (%s) does not match the commit yet; "
                                   "run verify" % pr.get("headRefOid"))
        store.mutate(rec["id"], lambda r, e: machine.set_git(r, None, None, url))
    except ToolError as e:
        store.mutate(rec["id"], lambda r, e2: machine.step_end(r, "failed", str(e)[:1000]))
        e.args = ("%s | completed and verified before the failure: %s"
                  % (e, "; ".join(out["verified"]) or "nothing"),)
        raise
    store.mutate(rec["id"], lambda r, e: machine.step_end(r, "succeeded"))
    out["not_done"] += ["merge: the pull request is yours to review and merge",
                        "deployment: nothing has been deployed"]
    return out


def verify(ws, rec):
    ws.require()
    g = rec.get("git") or {}
    if not g.get("commit"):
        raise Refused("nothing has been published for this request")
    pub = Publisher(ws)
    seen = pub.remote_head(g["branch"])
    out = {"id": rec["id"], "branch": g["branch"], "recorded_commit": g["commit"],
           "remote_commit": seen, "remote_matches": seen == g["commit"], "pull_request": None}
    if g.get("pr_url"):
        number = g["pr_url"].rsplit("/", 1)[-1]
        code, text, err = pub.gh(["pr", "view", number, "--repo", ws.repo["slug"], "--json",
                                  "url,state,headRefOid,mergedAt,baseRefName"])
        if code == 0:
            try:
                out["pull_request"] = json.loads(text)
            except ValueError:
                out["pull_request"] = {"error": "unreadable answer from gh"}
        else:
            out["pull_request"] = {"error": " ".join(err.split())[:200]}
    if seen is None:
        out["note"] = ("The remote branch no longer exists. That is normal after a merged pull "
                       "request is cleaned up; check the pull request state.")
    return out


def handler(a):
    root = paths.resolve_project_root(a.project_dir)
    ws = Workspace(root, ConfigStore(root).load())
    store = StateStore(root)
    rec = store.load(a.id)
    if a.cmd == "preview":
        facts = preflight(ws, rec)
        return {"id": rec["id"], "repository": ws.repo["slug"], "branch": facts["branch"],
                "base": ws.default_branch, "tree_hash": facts["tree_hash"],
                "files": [{"path": p, "action": x} for p, x in facts["changes"]],
                "uncommitted": len(facts["uncommitted"]), "validation": facts["validation"],
                "accepted_incomplete": facts["accepted_incomplete"],
                "pull_request_body": pr_body(rec, facts),
                "will": ["commit the files above on %s" % facts["branch"],
                         "push that branch to %s (never forced)" % ws.repo["slug"],
                         "open a pull request into %s" % ws.default_branch],
                "will_not": ["push to %s" % ws.default_branch, "merge", "deploy"]}
    if a.cmd == "publish":
        return publish(a, ws, store, rec)
    if a.cmd == "verify":
        return verify(ws, rec)
    raise AssertionError(a.cmd)


def main(argv=None):
    p = argparse.ArgumentParser(prog="github/cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preview").add_argument("id")
    sub.add_parser("verify").add_argument("id")
    s = sub.add_parser("publish")
    s.add_argument("id")
    s.add_argument("--message", required=True)
    s.add_argument("--title")
    s.add_argument("--notes")
    return run(handler, p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
