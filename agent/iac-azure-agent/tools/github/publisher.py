"""Publishes a request's validated files: commit, push, read the remote back, pull request.

Strategy (docs/decisions.md ADR-025): always the request's own branch `iac/<id>` and a
pull request into the default branch. Never the default branch itself, never a force push,
never a merge. The pull request is the review step.

Before anything is committed the tool checks, and refuses otherwise:
  - the request is active and in GIT_REVIEW;
  - the working copy is the configured repository, on the request's branch;
  - every changed file is under the infrastructure root;
  - validation ran on exactly these files (tree hash) and its verdict is not failed or
    none; skipped or unavailable checks must have been accepted when leaving VALIDATION.

Success is claimed only for what was read back: the remote branch head is compared with
the local commit after the push, and the pull request is fetched after it is created.
Each stage that completed is recorded even when a later one fails, so a rerun continues
where it stopped.
"""
import json
import re
import subprocess

from lib import secret_guard
from lib.errors import ExternalUnavailable, InvalidInput, Refused
from state import machine
from workspace.manager import BRANCH_PREFIX

TIMEOUT = 120
MAX_SUBJECT = 100
# Tenant, subscription and object IDs are not secrets, but they do not belong in commit
# messages or pull request text, which may be public. Requirements can mention them.
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def redact(text):
    return GUID.sub("<id removed>", text)


def gh(args, cwd=None, stdin=None):
    try:
        p = subprocess.run(["gh"] + args, cwd=cwd, input=stdin, capture_output=True, text=True,
                           timeout=TIMEOUT)
    except FileNotFoundError:
        raise ExternalUnavailable("gh_missing: the GitHub CLI (gh) is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise ExternalUnavailable("network: gh did not answer within %d seconds" % TIMEOUT)
    return p.returncode, p.stdout, p.stderr


def preflight(ws, rec):
    """Raises Refused unless the request's files may be published. Returns facts for the report."""
    if rec["status"] != "active" or rec["state"] != "GIT_REVIEW":
        raise Refused("publishing happens in GIT_REVIEW, after validation (request is %s, %s)"
                      % (rec["state"], rec["status"]))
    ws.require()
    branch = BRANCH_PREFIX + rec["id"]
    if ws.branch() != branch:
        raise Refused("the working copy is on %r, not on this request's branch %s"
                      % (ws.branch(), branch))
    if branch == ws.default_branch:
        raise Refused("refusing to publish to the default branch")
    changes = ws.changes()
    outside = [p for p, _ in changes if not ws.inside_infra(p)]
    if outside:
        raise Refused("files outside the infrastructure root were changed: %s. Nothing was "
                      "committed." % ", ".join(outside[:20]))
    run = rec.get("validation_run")
    if not run:
        raise Refused("no validation run is recorded for this request; nothing unvalidated is published")
    tree = ws.tree_hash()
    if run["tree_hash"] != tree:
        raise Refused("the files changed after validation ran. Move back to IMPLEMENTATION, "
                      "record the files and validate again; nothing was committed.")
    summary = machine.summarise_checks(rec["validation"])
    if summary["verdict"] in ("failed", "none"):
        raise Refused("validation verdict is %s; nothing unvalidated is published" % summary["verdict"])
    accepted = []
    if summary["verdict"] == "incomplete":
        accepted = [c for e in rec.get("accepted_incomplete", []) if e.get("what") == "validation"
                    for c in e.get("checks", [])]
        missing = summary["checks"]["skipped"] + summary["checks"]["unavailable"]
        if sorted(set(missing) - set(accepted)):
            raise Refused("validation is incomplete and was not accepted: %s" % ", ".join(missing))
    recorded = sorted((f["path"], f["action"]) for f in rec["files"])
    if recorded != sorted(changes):
        raise Refused("the changed files differ from the files recorded for this request; "
                      "run workspace/cli.py record-files after moving back to IMPLEMENTATION")
    return {"branch": branch, "changes": changes, "tree_hash": tree, "validation": summary,
            "accepted_incomplete": accepted, "uncommitted": ws.dirty()}


def check_message(message):
    if not isinstance(message, str) or not message.strip():
        raise InvalidInput("a commit message is required")
    message = message.strip()
    subject = message.splitlines()[0]
    if len(subject) > MAX_SUBJECT:
        raise InvalidInput("the first line of the commit message is longer than %d characters" % MAX_SUBJECT)
    secret_guard.check(message, "commit message")
    if GUID.search(message):
        raise InvalidInput("the commit message contains a GUID (tenant, subscription or object "
                           "ID); leave identifiers out of commit messages")
    return message


def pr_body(rec, facts, extra=None):
    """Built from the record, so the pull request states what was actually validated."""
    v = facts["validation"]
    lines = ["Request `%s`: %s" % (rec["id"], " ".join(rec["intent"].split())), ""]
    if extra:
        lines += [extra.strip(), ""]
    arch = rec.get("architecture") or {}
    if arch.get("objective"):
        lines += ["## Objective", "", arch["objective"], ""]
    lines += ["## Files", ""]
    lines += ["- `%s` (%s)" % (p, a) for p, a in facts["changes"]]
    lines += ["", "## Validation", "", "Verdict: **%s**. Files hash `%s`." % (v["verdict"], facts["tree_hash"][:12]), ""]
    for res in machine.CHECK_RESULTS:
        if v["checks"][res]:
            lines.append("- %s: %s" % (res, ", ".join(v["checks"][res])))
    if facts["accepted_incomplete"]:
        lines += ["", "Accepted by the user as not run: %s. These are not passes."
                  % ", ".join(sorted(set(facts["accepted_incomplete"])))]
    lines += ["", "Not run: Azure deployment validation and what-if. Nothing has been deployed.",
              "", "## Confirmed requirements", ""]
    lines += ["- %s" % " ".join(r["text"].split()) for r in rec["requirements"]] or ["- none"]
    assumed = [a for a in rec["assumptions"] if a["status"] == "assumed"]
    lines += ["", "## Assumptions (not confirmed)", ""]
    lines += ["- %s" % " ".join(a["text"].split()) for a in assumed] or ["- none"]
    body = redact("\n".join(lines) + "\n")
    secret_guard.check(body, "pull request body")
    return body


class Publisher:
    def __init__(self, ws, run_gh=gh):
        self.ws = ws
        self.gh = run_gh

    def commit(self, message):
        """Stages only the infrastructure root. Returns the commit SHA (existing HEAD when
        there is nothing new to commit, as on a rerun)."""
        ws = self.ws
        if ws.dirty():
            ws._ok("add", "--", ws.infra_root)
            code, _, err = ws._git("commit", "--quiet", "-m", message, "--", ws.infra_root)
            if code != 0:
                low = err.lower()
                if "tell me who you are" in low or "user.email" in low or "empty ident" in low:
                    raise Refused("git has no author identity. Set user.name and user.email in "
                                  "your git configuration yourself; nothing was committed.")
                raise ExternalUnavailable("error: git commit failed: %s" % " ".join(err.split())[:300])
        head = ws.head()
        if not head or ws.dirty():
            raise ExternalUnavailable("error: the working copy is not clean after the commit; "
                                      "nothing was pushed")
        return head

    def remote_head(self, branch):
        code, out, err = self.ws._git("ls-remote", "--heads", "origin", "refs/heads/%s" % branch)
        if code != 0:
            raise ExternalUnavailable("network: could not read the remote branch: %s"
                                      % " ".join(err.split())[:200])
        return out.split()[0] if out.strip() else None

    def push(self, branch, commit):
        """Plain push, never forced. Returns after reading the remote head back."""
        ws = self.ws
        code, _, err = ws._git("push", "--quiet", "origin", "refs/heads/%s:refs/heads/%s" % (branch, branch))
        if code != 0:
            low = err.lower()
            if "non-fast-forward" in low or "fetch first" in low or "rejected" in low:
                raise Refused("GitHub rejected the push: the remote branch %s has commits this "
                              "working copy does not have. Nothing was forced. Someone else "
                              "changed it; this needs a person to look at it." % branch)
            if "protected branch" in low or "permission" in low or "403" in low or "denied" in low:
                raise Refused("GitHub refused the push (permission or branch protection): %s"
                              % " ".join(err.split())[:200])
            raise ExternalUnavailable("push failed and nothing was verified: %s" % " ".join(err.split())[:300])
        seen = self.remote_head(branch)
        if seen != commit:
            raise ExternalUnavailable("the push returned success but the remote branch is at %s, "
                                      "not %s. Do not treat this as published." % (seen, commit))
        return seen

    def find_pr(self, branch):
        code, out, err = self.gh(["pr", "list", "--repo", self.ws.repo["slug"], "--head", branch,
                                  "--state", "open", "--json", "url,number,headRefOid,baseRefName"])
        if code != 0:
            raise ExternalUnavailable("could not list pull requests: %s" % " ".join(err.split())[:200])
        try:
            items = json.loads(out or "[]")
        except ValueError:
            raise ExternalUnavailable("gh returned unreadable output listing pull requests")
        return items[0] if items else None

    def open_pr(self, branch, title, body):
        existing = self.find_pr(branch)
        if existing:
            return existing, False
        code, out, err = self.gh(["pr", "create", "--repo", self.ws.repo["slug"],
                                  "--base", self.ws.default_branch, "--head", branch,
                                  "--title", title, "--body-file", "-"], stdin=body)
        if code != 0:
            raise ExternalUnavailable("the branch was pushed but the pull request was not "
                                      "created: %s" % " ".join(err.split())[:300])
        created = self.find_pr(branch)
        if not created:
            raise ExternalUnavailable("gh reported a pull request but none is open for %s; "
                                      "do not treat it as created" % branch)
        return created, True
