"""Installs the validation workflow into the configured repository (ADR-034).

The workflow file is a fixed template shipped with the plugin (templates/iac-validate.yml).
The only thing filled in is the infrastructure root, which the config schema restricts to
letters, digits, '.', '_', '-' and '/'. The model does not write workflow content, and the
Write/Edit limit of ADR-022 is unchanged: this tool is the one path to .github/workflows.

Like publishing, it never touches the default branch: the file is committed on its own
branch, pushed without force, read back, and offered as a pull request. Merging is the
user's decision, so the workflow runs only after they have reviewed it.
"""
import hashlib
import os

from config import schema
from lib.errors import ExternalUnavailable, Refused

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(PLUGIN_ROOT, "templates", "iac-validate.yml")
WORKFLOW_PATH = ".github/workflows/iac-validate.yml"
BRANCH = "iac/validation-workflow"
PLACEHOLDER = "__INFRA_ROOT__"
TITLE = "Add the iac-validate workflow"
MESSAGE = "Add the iac-validate workflow\n\nInstalled from the iac-azure-agent template."


def render(infra_root):
    if not infra_root:
        raise Refused("infra_root is not configured; run /iac-setup")
    root = schema.check_infra_root(infra_root)
    try:
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ExternalUnavailable("the workflow template could not be read: %s" % (e.strerror or e))
    return text.replace(PLACEHOLDER, root)


def _on_default(ws, ref):
    code, out, _ = ws._git("show", "%s:%s" % (ref, WORKFLOW_PATH))
    return out if code == 0 else None


def _ref(ws):
    ws.require()
    ws.fetch()
    ref = ws._remote_ref()
    if not ref:
        raise Refused("the default branch %r was not found on GitHub; the workflow is "
                      "installed with a pull request into it" % ws.default_branch)
    return ref


def status(ws):
    """Compares the default branch on GitHub (after a fetch) with the template. Read-only."""
    wanted = render(ws.infra_root)
    current = _on_default(ws, _ref(ws))
    state = "absent" if current is None else ("current" if current == wanted else "differs")
    return {"path": WORKFLOW_PATH, "default_branch": ws.default_branch, "state": state,
            "meaning": {"absent": "the workflow is not on the default branch",
                        "current": "the default branch holds exactly the template",
                        "differs": "the file on the default branch is not the current template "
                                   "(older template, another infrastructure root, or edited)"}[state]}


def pr_text(ws):
    return ("Adds `%s`, the validation workflow from the iac-azure-agent template.\n\n"
            "On every pull request that touches `%s/` it compiles and lints the Bicep; a failure "
            "fails the check. Parameter files and Checkov findings are reported only: they "
            "depend on things the runner does not have (environment variables, the findings "
            "you accepted).\n\n"
            "- No Azure sign-in, no secrets, nothing is deployed.\n"
            "- `permissions: contents: read`; triggered by `pull_request`, never `pull_request_target`.\n"
            "- The checkout action is pinned to a commit.\n\n"
            "The file was written by the tool from a fixed template, not by the model. "
            "Review it before merging: it runs in your repository from then on.\n"
            % (WORKFLOW_PATH, ws.infra_root))


def install(ws, pub):
    """Returns a report. Raises Refused or ExternalUnavailable; a stage that completed and
    was read back is listed in `verified` on the exception path too (see cli.py)."""
    wanted = render(ws.infra_root)
    ref = _ref(ws)
    out = {"path": WORKFLOW_PATH, "repository": ws.repo["slug"], "base": ws.default_branch,
           "verified": [], "not_done": []}
    if _on_default(ws, ref) == wanted:
        out.update(changed=False, state="current")
        out["verified"].append("%s on %s already holds the template" % (WORKFLOW_PATH, ws.default_branch))
        return out
    if ws.dirty():
        raise Refused("the working copy has uncommitted changes; they would be carried onto "
                      "the workflow branch. Nothing was changed.")
    previous = ws.branch()
    # The branch name carries the content, so a rerun continues on the same branch and a
    # new template version never collides with an old, unmerged one.
    branch = "%s-%s" % (BRANCH, hashlib.sha256(wanted.encode("utf-8")).hexdigest()[:8])
    out["branch"] = branch
    code, _, _ = ws._git("rev-parse", "-q", "--verify", "refs/heads/%s" % branch)
    if code == 0:
        ws._ok("checkout", "--quiet", branch)
    else:
        ws._ok("checkout", "--quiet", "--no-track", "-b", branch, ref)
    try:
        full = os.path.join(ws.dir, *WORKFLOW_PATH.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(wanted)
        changed = ws.dirty()
        if any(p != WORKFLOW_PATH for p, _ in changed):
            raise Refused("unexpected changes in the working copy: %s. Nothing was committed."
                          % ", ".join(p for p, _ in changed[:20]))
        if changed:
            ws._ok("add", "--", WORKFLOW_PATH)
            code, _, err = ws._git("commit", "--quiet", "-m", MESSAGE, "--", WORKFLOW_PATH)
            if code != 0:
                low = err.lower()
                if "tell me who you are" in low or "user.email" in low or "empty ident" in low:
                    raise Refused("git has no author identity. Set user.name and user.email in "
                                  "your git configuration yourself; nothing was committed.")
                raise ExternalUnavailable("error: git commit failed: %s" % " ".join(err.split())[:300])
        commit = ws.head()
        touched = [p for p, _ in ws.committed_changes()]
        if touched != [WORKFLOW_PATH]:
            raise Refused("the workflow branch %s holds other changes (%s); nothing was pushed"
                          % (branch, ", ".join(touched[:20]) or "none"))
        out["commit"] = commit
        pub.push(branch, commit)
        out["verified"].append("remote branch %s is at %s" % (branch, commit))
        pr, created = pub.open_pr(branch, TITLE, pr_text(ws))
        url = str(pr.get("url") or "")
        if not url.startswith("https://github.com/%s/pull/" % ws.repo["slug"]):
            raise ExternalUnavailable("GitHub returned an unexpected pull request address")
        out["pull_request"] = {"url": url, "created": created, "head": pr.get("headRefOid")}
        if pr.get("headRefOid") == commit:
            out["verified"].append("pull request %s is open at %s" % (url, commit))
        else:
            out["not_done"].append("the pull request head (%s) does not match the commit yet"
                                   % pr.get("headRefOid"))
    except Exception as e:  # noqa: BLE001 - report what was read back, then re-raise
        e.args = ("%s | completed and verified before the failure: %s"
                  % (e, "; ".join(out["verified"]) or "nothing"),)
        raise
    finally:
        if previous and previous != branch and not ws.dirty():
            ws._git("checkout", "--quiet", previous)
    out.update(changed=True, state="pull_request_open")
    out["not_done"] += ["merge: the pull request is yours to review and merge; the workflow "
                        "runs only once it is on %s" % ws.default_branch]
    return out
