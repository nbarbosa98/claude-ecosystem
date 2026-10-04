"""Milestone 4: publishing. GitHub is a local bare repository (real git push and ls-remote
against it) and a fake `gh` that keeps pull requests in a JSON file."""
import json
import os
import sys

from helpers import CONFIG_CLI, STATE_CLI, TOOLS
from test_workspace_validate import MAIN, STORAGE, VALIDATE_CLI, WORKSPACE_CLI, WorkCase, git

from config.store import ConfigStore
from lib.errors import EXIT_EXTERNAL, EXIT_INVALID, EXIT_REFUSED
from state import machine as M
from state.store import StateStore

GITHUB_CLI = os.path.join(TOOLS, "github", "cli.py")

FAKE_GH_PR = '''#!%s
import json, os, subprocess, sys
state_file = os.environ["FAKE_GH_STATE"]
fail = os.environ.get("FAKE_GH_FAIL", "")
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
try:
    state = json.load(open(state_file))
except (OSError, ValueError):
    state = {"prs": [], "calls": []}
state["calls"].append(args)
def save():
    json.dump(state, open(state_file, "w"))
def oid(branch):
    bare = os.path.join(os.environ["FAKE_GH_REMOTES"], opt("--repo") + ".git")
    p = subprocess.run(["git", "--git-dir", bare, "rev-parse", "-q", "--verify", "refs/heads/" + branch],
                       capture_output=True, text=True)
    return p.stdout.strip() or None
if args[:2] == ["pr", "list"]:
    save()
    if fail == "list":
        sys.stderr.write("gh: HTTP 502\\n"); sys.exit(1)
    head = opt("--head")
    print(json.dumps([dict(p, headRefOid=oid(p["head"])) for p in state["prs"] if p["head"] == head]))
elif args[:2] == ["pr", "create"]:
    if fail == "create":
        save(); sys.stderr.write("gh: GraphQL: was submitted too quickly\\n"); sys.exit(1)
    n = len(state["prs"]) + 1
    pr = {"number": n, "url": "https://github.com/%%s/pull/%%d" %% (opt("--repo"), n), "head": opt("--head"),
          "baseRefName": opt("--base"), "title": opt("--title"), "body": sys.stdin.read(), "state": "OPEN"}
    state["prs"].append(pr); save(); print(pr["url"])
elif args[:2] == ["pr", "view"]:
    save()
    pr = [p for p in state["prs"] if str(p["number"]) == args[2]][0]
    print(json.dumps({"url": pr["url"], "state": pr["state"], "headRefOid": oid(pr["head"]),
                      "mergedAt": None, "baseRefName": pr["baseRefName"]}))
else:
    save(); sys.stderr.write("fake gh: unsupported\\n"); sys.exit(1)
''' % sys.executable


class PublishCase(WorkCase):
    def setUp(self):
        super().setUp()
        self.install("gh", FAKE_GH_PR)
        self.gh_state = os.path.join(self.tmp, "gh-state.json")
        self.env = dict(self.env, FAKE_GH_STATE=self.gh_state, FAKE_GH_REMOTES=self.remotes)
        self.only_fakes = dict(self.only_fakes, FAKE_GH_STATE=self.gh_state, FAKE_GH_REMOTES=self.remotes)
        self.bare = os.path.join(self.remotes, "example-org/infra.git")
        self.write_settings()

    def ready(self):
        """A request in GIT_REVIEW whose files were validated."""
        rid = self.request_in("VALIDATION")
        code, out = self.cli(VALIDATE_CLI, "run", rid, env=self.env)
        self.assertEqual(out["verdict"], "passed", out)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW")
        self.assertEqual(code, 0, out)
        return rid

    def publish(self, rid, *extra, env=None, message="Add change to log storage"):
        return self.cli(GITHUB_CLI, "publish", rid, "--message", message, *extra,
                        env=dict(self.env, **(env or {})))

    def remote_branches(self):
        out = git(["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"], self.bare, self.full_env)
        return dict(line.split() for line in out.splitlines())

    def gh_calls(self):
        with open(self.gh_state) as f:
            return json.load(f)

    def rec(self, rid):
        return StateStore(self.project).load(rid)


class Publish(PublishCase):
    def test_happy_path_commits_pushes_verifies_and_opens_a_pull_request(self):
        rid = self.ready()
        main_before = self.remote_branches()["main"]
        code, out = self.publish(rid, "--notes", "First storage change.")
        self.assertEqual(code, 0, out)
        branch = "iac/" + rid
        remote = self.remote_branches()
        self.assertEqual(remote[branch], out["commit"])            # independently read back
        self.assertEqual(remote["main"], main_before)              # default branch untouched
        self.assertEqual(out["pull_request"]["url"], "https://github.com/example-org/infra/pull/1")
        self.assertTrue(out["pull_request"]["created"])
        self.assertEqual(len(out["verified"]), 2)
        self.assertIn("merge", " ".join(out["not_done"]))
        self.assertEqual(out["files"], [{"path": "infra/modules/storage.bicep", "action": "modified"}])
        g = self.rec(rid)["git"]
        self.assertEqual((g["branch"], g["commit"], g["pr_url"]),
                         (branch, out["commit"], "https://github.com/example-org/infra/pull/1"))
        self.assertEqual(g["published"]["tree_hash"], self.ws().tree_hash())
        self.assertEqual(self.rec(rid)["step"]["status"], "succeeded")
        pr = self.gh_calls()["prs"][0]
        self.assertEqual((pr["baseRefName"], pr["head"]), ("main", branch))
        for expected in ("Verdict: **passed**", "infra/modules/storage.bicep", "First storage change.",
                         "Nothing has been deployed", "Logs kept 90 days"):
            self.assertIn(expected, pr["body"])
        # Only the infrastructure root was committed.
        names = git(["show", "--name-only", "--format=", out["commit"]], self.ws().dir, self.full_env)
        self.assertEqual(names.split(), ["infra/modules/storage.bicep"])
        code, nxt = self.cli(STATE_CLI, "show", rid)
        self.assertEqual(nxt["summary"]["state"], "GIT_REVIEW")

    def test_preview_changes_nothing(self):
        rid = self.ready()
        before = (self.rec(rid), self.remote_branches(), self.ws().head(), self.ws().dirty())
        code, out = self.cli(GITHUB_CLI, "preview", rid, env=self.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["branch"], "iac/" + rid)
        self.assertIn("merge", out["will_not"])
        self.assertEqual(before, (self.rec(rid), self.remote_branches(), self.ws().head(), self.ws().dirty()))
        self.assertFalse(os.path.exists(self.gh_state))

    def test_files_changed_after_validation_are_not_published(self):
        rid = self.ready()
        self.write("infra/modules/storage.bicep", STORAGE + "// edited after validation\n")
        code, out = self.publish(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("changed after validation", out["message"])
        self.assertNotIn("iac/" + rid, self.remote_branches())
        self.assertIsNone(self.rec(rid)["git"])

    def test_file_outside_the_infrastructure_root_blocks_publishing(self):
        rid = self.ready()
        self.write("src/app.py", "print('tampered')\n")
        code, out = self.publish(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("src/app.py", out["message"])
        self.assertNotIn("iac/" + rid, self.remote_branches())

    def test_wrong_state_wrong_branch_and_missing_rules(self):
        rid = self.request_in("VALIDATION")
        code, out = self.publish(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("GIT_REVIEW", out["message"])
        code, out = self.cli(VALIDATE_CLI, "run", rid, env=self.env)
        self.cli(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW")
        rid2 = rid
        git(["checkout", "--quiet", "main"], self.ws().dir, self.full_env)
        code, out = self.publish(rid2)
        self.assertEqual(code, EXIT_REFUSED)
        git(["checkout", "--quiet", "iac/" + rid2], self.ws().dir, self.full_env)
        os.unlink(os.path.join(self.project, ".claude", "settings.json"))
        code, out = self.publish(rid2)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("permission rules", out["message"])
        self.assertNotIn("iac/" + rid2, self.remote_branches())

    def test_unaccepted_incomplete_validation_cannot_be_published(self):
        rid = self.request_in("VALIDATION")
        os.unlink(os.path.join(self.bin, "checkov"))
        self.cli(VALIDATE_CLI, "run", rid, env=self.only_fakes)
        store = StateStore(self.project)
        # Force the state without the acceptance, as a tampered record would.
        store.mutate(rid, lambda r, e: r.update(state="GIT_REVIEW"))
        code, out = self.cli(GITHUB_CLI, "publish", rid, "--message", "x", env=self.only_fakes)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("incomplete", out["message"])

    def test_accepted_incomplete_validation_is_published_and_says_so(self):
        rid = self.request_in("VALIDATION")
        os.unlink(os.path.join(self.bin, "checkov"))
        self.cli(VALIDATE_CLI, "run", rid, env=self.only_fakes)
        self.cli(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW", "--accept-incomplete")
        code, out = self.cli(GITHUB_CLI, "publish", rid, "--message", "Add storage", env=self.only_fakes)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["validation"], "incomplete")
        body = self.gh_calls()["prs"][0]["body"]
        self.assertIn("Accepted by the user as not run: security-scan", body)
        self.assertIn("unavailable: security-scan", body)

    def test_secret_in_commit_message_is_refused(self):
        rid = self.ready()
        code, out = self.publish(rid, message="Add token " + "ghp_" + "A1b2C3d4" * 5)
        self.assertEqual(code, EXIT_INVALID)
        self.assertNotIn("iac/" + rid, self.remote_branches())
        code, out = self.publish(rid, message="x" * 150)
        self.assertEqual(code, EXIT_INVALID)

    def test_remote_branch_changed_by_someone_else_is_never_forced(self):
        rid = self.ready()
        branch = "iac/" + rid
        other = os.path.join(self.tmp, "seed-example-org-infra")
        git(["checkout", "--quiet", "-b", branch], other, self.full_env)
        self.commit(other, {"infra/theirs.bicep": "param x string\n"}, "someone else")
        git(["push", "--quiet", "origin", branch], other, self.full_env)
        theirs = self.remote_branches()[branch]
        code, out = self.publish(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("Nothing was forced", out["message"])
        self.assertEqual(self.remote_branches()[branch], theirs)
        self.assertEqual(self.rec(rid)["step"]["status"], "failed")
        self.assertIsNone(self.rec(rid)["git"])

    def test_pull_request_failure_is_reported_as_partial_and_a_rerun_finishes(self):
        rid = self.ready()
        code, out = self.publish(rid, env={"FAKE_GH_FAIL": "create"})
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertIn("pull request was not created", out["message"])
        self.assertIn("remote branch iac/%s is at" % rid, out["message"])   # what did happen
        g = self.rec(rid)["git"]
        self.assertEqual(g["commit"], self.remote_branches()["iac/" + rid])
        self.assertNotIn("pr_url", g)
        self.assertEqual(self.rec(rid)["step"]["status"], "failed")
        code, blocked = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT_APPROVAL")
        self.assertEqual(code, EXIT_REFUSED)                       # failed step blocks moving on
        code, out = self.publish(rid)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["commit"], g["commit"])               # no second commit
        self.assertTrue(out["pull_request"]["created"])
        self.assertEqual(self.rec(rid)["step"]["attempt"], 2)

    def test_publishing_twice_reuses_the_pull_request(self):
        rid = self.ready()
        code, first = self.publish(rid)
        code, second = self.publish(rid)
        self.assertEqual(code, 0, second)
        self.assertEqual(first["commit"], second["commit"])
        self.assertFalse(second["pull_request"]["created"])
        self.assertEqual(len(self.gh_calls()["prs"]), 1)

    def test_gh_missing_after_push_is_partial_not_success(self):
        rid = self.ready()
        os.unlink(os.path.join(self.bin, "gh"))
        code, out = self.cli(GITHUB_CLI, "publish", rid, "--message", "Add storage", env=self.only_fakes)
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertIn("gh_missing", out["message"])
        self.assertIn("iac/" + rid, self.remote_branches())
        self.assertNotIn("pr_url", self.rec(rid)["git"])

    def test_no_git_identity_commits_nothing(self):
        rid = self.ready()
        env = {k: v for k, v in self.env.items() if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}
        env.update(GIT_AUTHOR_NAME="", GIT_AUTHOR_EMAIL="", GIT_COMMITTER_NAME="", GIT_COMMITTER_EMAIL="",
                   EMAIL="", HOME=self.tmp)
        before = self.ws().head()
        code, out = self.cli(GITHUB_CLI, "publish", rid, "--message", "Add storage", env=env)
        self.assertEqual(code, EXIT_REFUSED, out)
        self.assertIn("author identity", out["message"])
        self.assertEqual(before, self.ws().head())
        self.assertNotIn("iac/" + rid, self.remote_branches())

    def test_verify_reads_github_again(self):
        rid = self.ready()
        code, out = self.cli(GITHUB_CLI, "verify", rid, env=self.env)
        self.assertEqual(code, EXIT_REFUSED)                       # nothing published yet
        self.publish(rid)
        code, out = self.cli(GITHUB_CLI, "verify", rid, env=self.env)
        self.assertTrue(out["remote_matches"])
        self.assertEqual(out["pull_request"]["state"], "OPEN")
        git(["update-ref", "-d", "refs/heads/iac/" + rid], self.bare, self.full_env)
        code, out = self.cli(GITHUB_CLI, "verify", rid, env=self.env)
        self.assertFalse(out["remote_matches"])
        self.assertIsNone(out["remote_commit"])

    def test_deployment_approval_covers_the_published_commit(self):
        rid = self.ready()
        code, out = self.publish(rid)
        store = StateStore(self.project)
        self.assertEqual(M.deployment_content(store.load(rid))["git_commit"], out["commit"])


class AcceptedFindings(PublishCase):
    def test_accepted_finding_is_a_warning_with_its_reason_never_a_pass(self):
        self.ws().clone()
        self.write("infra/modules/storage.bicep", STORAGE + "// PUBLIC\n")
        code, out = self.cli(VALIDATE_CLI, "run", env=self.env)
        self.assertEqual({r["check"]: r["result"] for r in out["results"]}["security-scan"], "failed")
        code, out = self.cli(CONFIG_CLI, "set", "accepted_findings.CKV_AZURE_35", "short")
        self.assertEqual(code, EXIT_INVALID)                       # a real reason is required
        code, out = self.cli(CONFIG_CLI, "set", "accepted_findings.CKV_AZURE_35",
                             "Public endpoint agreed for the dev sandbox only")
        self.assertEqual(code, 0, out)
        code, out = self.cli(VALIDATE_CLI, "run", env=self.env)
        sec = {r["check"]: r for r in out["results"]}["security-scan"]
        self.assertEqual(sec["result"], "warning")
        self.assertIn("dev sandbox", sec["findings"][0]["accepted"])
        self.assertEqual(out["verdict"], "passed_with_warnings")
        self.cli(CONFIG_CLI, "unset", "accepted_findings.CKV_AZURE_35")
        code, out = self.cli(VALIDATE_CLI, "run", env=self.env)
        self.assertEqual({r["check"]: r["result"] for r in out["results"]}["security-scan"], "failed")


class ReadBack(PublishCase):
    """A push that returns success is not believed until the remote shows the commit."""

    class LyingWorkspace:
        repo = {"slug": "example-org/infra"}
        default_branch = "main"

        def __init__(self, remote_sha):
            self.remote_sha = remote_sha

        def _git(self, *args):
            if args[0] == "push":
                return 0, "", ""
            if args[0] == "ls-remote":
                return 0, ("%s\trefs/heads/iac/x\n" % self.remote_sha) if self.remote_sha else "", ""
            raise AssertionError(args)

    def test_push_success_without_the_commit_on_the_remote_is_an_error(self):
        from github.publisher import Publisher
        from lib.errors import ExternalUnavailable
        mine = "a" * 40
        for remote in ("b" * 40, None):
            with self.assertRaises(ExternalUnavailable) as cm:
                Publisher(self.LyingWorkspace(remote)).push("iac/x", mine)
            self.assertIn("Do not treat this as published", str(cm.exception))
        self.assertEqual(Publisher(self.LyingWorkspace(mine)).push("iac/x", mine), mine)

    def test_pull_request_that_cannot_be_found_after_creation_is_an_error(self):
        from github.publisher import Publisher
        from lib.errors import ExternalUnavailable
        calls = []

        def fake_gh(args, cwd=None, stdin=None):
            calls.append(args[:2])
            return (0, "[]", "") if args[:2] == ["pr", "list"] else (0, "https://github.com/example-org/infra/pull/9\n", "")
        with self.assertRaises(ExternalUnavailable) as cm:
            Publisher(self.LyingWorkspace(None), run_gh=fake_gh).open_pr("iac/x", "t", "b")
        self.assertIn("do not treat it as created", str(cm.exception))
        self.assertEqual(calls, [["pr", "list"], ["pr", "create"], ["pr", "list"]])
