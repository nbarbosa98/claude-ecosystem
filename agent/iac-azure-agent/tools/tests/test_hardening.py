"""Milestone 6: the validation workflow installer, the merged-first rule for production,
and the production path end to end. GitHub is a local bare repository and a fake `gh`;
Azure is a fake `az`. Nothing here touches the network."""
import json
import os
import re
import stat

from helpers import CONFIG_CLI, PLUGIN, STATE_CLI
from test_deploy import DEPLOY_CLI, DeployCase
from test_publish import GITHUB_CLI, PublishCase
from test_workspace_validate import PARAMS, git

from config.store import ConfigStore
from github import workflow
from lib.errors import EXIT_EXTERNAL, EXIT_REFUSED
from setup import permissions

WF = ".github/workflows/iac-validate.yml"


class Template(PublishCase):
    def test_template_is_read_only_pinned_and_needs_no_secret(self):
        text = workflow.render("infra")
        self.assertNotIn(workflow.PLACEHOLDER, text)
        self.assertIn('- "infra/**"', text)
        self.assertIn("permissions:\n  contents: read\n", text)
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("azure/login", text.lower())
        self.assertNotIn("deployment", text.replace("deploys nothing", ""))
        uses = re.findall(r"uses:\s*(\S+)", text)
        self.assertTrue(uses)
        for u in uses:                                   # every action pinned to a commit
            self.assertRegex(u, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", text)

    def test_infrastructure_root_is_the_only_input_and_is_validated(self):
        self.assertIn('INFRA_ROOT: "platform/bicep"', workflow.render("platform/bicep"))
        for bad in ('infra"\n  evil: x', "../x", "a b", "$(id)", "infra/**"):
            with self.assertRaises(Exception):
                workflow.render(bad)


class InstallWorkflow(PublishCase):
    def install_wf(self, env=None):
        return self.cli(GITHUB_CLI, "install-workflow", env=dict(self.env, **(env or {})))

    def status(self):
        return self.cli(GITHUB_CLI, "workflow-status", env=self.env)[1]["state"]

    def show(self, ref, path=WF):
        return git(["show", "%s:%s" % (ref, path)], self.bare, self.full_env)

    def test_install_pushes_one_file_on_its_own_branch_and_opens_a_pull_request(self):
        self.ws().clone()
        main_before = self.remote_branches()["main"]
        self.assertEqual(self.status(), "absent")
        code, out = self.install_wf()
        self.assertEqual(code, 0, out)
        remote = self.remote_branches()
        self.assertEqual(remote["main"], main_before)              # default branch untouched
        self.assertEqual(remote[out["branch"]], out["commit"])     # independently read back
        self.assertTrue(out["branch"].startswith("iac/validation-workflow-"))
        changed = git(["diff", "--name-only", "main", out["branch"]], self.bare, self.full_env)
        self.assertEqual(changed.split(), [WF])                    # nothing else rides along
        self.assertEqual(self.show(out["branch"]) + "\n", workflow.render("infra"))
        self.assertTrue(out["pull_request"]["created"])
        self.assertEqual(out["pull_request"]["head"], out["commit"])
        self.assertEqual(len(out["verified"]), 2)
        self.assertIn("merge", " ".join(out["not_done"]))
        self.assertEqual(self.ws().branch(), "main")               # working copy handed back
        self.assertEqual(self.ws().dirty(), [])
        self.assertEqual(self.status(), "absent")                  # not on main until merged
        pr = self.gh_calls()["prs"][0]
        self.assertEqual((pr["baseRefName"], pr["head"]), ("main", out["branch"]))
        self.assertIn("not by the model", pr["body"])

    def test_rerun_reuses_the_branch_and_pull_request(self):
        self.ws().clone()
        first = self.install_wf()[1]
        code, second = self.install_wf()
        self.assertEqual(code, 0, second)
        self.assertEqual(second["commit"], first["commit"])
        self.assertFalse(second["pull_request"]["created"])
        self.assertEqual(len(self.gh_calls()["prs"]), 1)

    def test_once_merged_nothing_is_done(self):
        self.ws().clone()
        first = self.install_wf()[1]
        git(["update-ref", "refs/heads/main", first["commit"]], self.bare, self.full_env)   # "merge"
        self.assertEqual(self.status(), "current")
        code, out = self.install_wf()
        self.assertEqual(code, 0, out)
        self.assertFalse(out["changed"])
        self.assertNotIn("commit", out)
        self.assertEqual(len(self.gh_calls()["prs"]), 1)

    def test_changed_infrastructure_root_is_a_new_pull_request(self):
        self.ws().clone()
        first = self.install_wf()[1]
        git(["update-ref", "refs/heads/main", first["commit"]], self.bare, self.full_env)
        ConfigStore(self.project).set_value("infra_root", "src")
        self.assertEqual(self.status(), "differs")
        code, out = self.install_wf()
        self.assertEqual(code, 0, out)
        self.assertNotEqual(out["branch"], first["branch"])
        self.assertIn('- "src/**"', self.show(out["branch"]))

    def test_request_in_progress_is_not_disturbed(self):
        rid = self.request_in("IMPLEMENTATION")
        self.write("infra/modules/storage.bicep", "// half-written\n")
        code, out = self.install_wf()
        self.assertEqual(code, EXIT_REFUSED)                       # uncommitted work: refused
        self.assertEqual(self.ws().branch(), "iac/" + rid)
        self.assertNotIn("iac/validation-workflow", " ".join(self.remote_branches()))
        git(["checkout", "--", "."], self.ws().dir, self.full_env)
        code, out = self.install_wf()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ws().branch(), "iac/" + rid)         # back on the request's branch

    def test_missing_permission_rule_or_working_copy_refuses(self):
        code, out = self.install_wf()
        self.assertNotEqual(code, 0)                               # no working copy yet
        self.ws().clone()
        self.write_settings([v["rule"] for k, v in permissions.REQUIRED.items() if k != "install-workflow"])
        code, out = self.install_wf()
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("install-workflow", out["message"])
        self.assertEqual(list(self.remote_branches()), ["main"])

    def test_credential_without_the_workflow_scope_is_explained(self):
        self.ws().clone()
        hook = os.path.join(self.bare, "hooks", "pre-receive")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho 'refusing to allow an OAuth App to create or update workflow "
                    "`.github/workflows/iac-validate.yml` without `workflow` scope' >&2\nexit 1\n")
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        code, out = self.install_wf()
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("gh auth refresh -s workflow", out["message"])
        self.assertIn("verified before the failure: nothing", out["message"])
        self.assertEqual(list(self.remote_branches()), ["main"])
        self.assertEqual(self.ws().branch(), "main")

    def test_pull_request_failure_after_the_push_is_partial_and_a_rerun_finishes(self):
        self.ws().clone()
        code, out = self.install_wf(env={"FAKE_GH_FAIL": "create"})
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertIn("remote branch iac/validation-workflow-", out["message"])
        code, out = self.install_wf()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["pull_request"]["created"])


class ProdCase(DeployCase):
    FILES = dict(DeployCase.FILES, **{"infra/parameters/prod.bicepparam": PARAMS.replace("dev", "prod")})

    def setUp(self):
        super().setUp()
        ConfigStore(self.project).set_value("environments", "dev,prod")
        ConfigStore(self.project).set_value("production_environments", "prod")

    def deploy(self, rid, env=None):
        return self.run_cli(DEPLOY_CLI, "deploy", rid, env=env)

    def set_pr(self, **kw):
        state = self.gh_calls()
        state["prs"][0].update(kw)
        with open(self.gh_state, "w") as f:
            json.dump(state, f)

    def merge(self, rid, **kw):
        self.set_pr(**dict({"state": "MERGED", "mergedAt": "2026-10-10T10:00:00Z",
                            "headRefOid": self.rec(rid)["git"]["commit"]}, **kw))

    def views(self):
        return [c for c in self.gh_calls()["calls"] if c[:2] == ["pr", "view"]]


class MergedFirstForProduction(ProdCase):
    def test_production_is_not_deployed_from_an_unmerged_pull_request(self):
        rid = self.approved("prod")
        self.assertTrue(self.rec(rid)["approvals"]["high_risk"]["valid"])   # approved, confirmed
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("merged first", out["message"])
        self.assertEqual(self.az_calls("create"), [])
        self.assertIsNone(self.rec(rid)["deployment"])
        self.assertEqual(self.rec(rid)["state"], "DEPLOYMENT")     # the approval is not lost
        self.merge(rid)
        code, out = self.deploy(rid)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["status"], "succeeded")
        self.assertTrue(out["merged_pull_request"]["merged"])
        self.assertEqual(len(self.az_calls("create")), 1)

    def test_plan_says_a_merge_will_be_required(self):
        rid = self.published()
        self.assertTrue(self.plan(rid, environment="prod")[1]["requires_merged_pull_request"])
        self.assertFalse(self.plan(rid)[1]["requires_merged_pull_request"])

    def test_closed_or_merged_elsewhere_or_with_later_commits_is_refused(self):
        rid = self.approved("prod")
        for change, text in (({"state": "CLOSED"}, "is closed"),
                             ({"state": "MERGED", "mergedAt": None}, "merged first"),
                             ({"baseRefName": "release"}, "not the default branch"),
                             ({"headRefOid": "f" * 40}, "commits were added after publishing")):
            self.merge(rid, **change)
            code, out = self.deploy(rid)
            self.assertEqual(code, EXIT_REFUSED, change)
            self.assertIn(text, out["message"])
            self.set_pr(baseRefName="main")
        self.assertEqual(self.az_calls("create"), [])

    def test_github_unreachable_means_unknown_and_nothing_is_deployed(self):
        rid = self.approved("prod")
        self.merge(rid)
        code, out = self.deploy(rid, env={"FAKE_GH_FAIL": "view"})
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertIn("unknown", out["message"])
        self.assertEqual(self.az_calls("create"), [])

    def test_development_deploys_from_the_branch_without_asking_github(self):
        rid = self.approved("dev")
        before = len(self.views())
        code, out = self.deploy(rid)
        self.assertEqual(code, 0, out)
        self.assertIsNone(out["merged_pull_request"])
        self.assertEqual(len(self.views()), before)

    def test_built_in_production_names_count_without_configuration(self):
        ConfigStore(self.project).unset_value("production_environments")
        rid = self.approved("prod")
        self.assertEqual(self.deploy(rid)[0], EXIT_REFUSED)
        self.assertEqual(self.az_calls("create"), [])


class ProductionEndToEnd(ProdCase):
    """Published code to a completed production request, through the CLIs: what-if, both
    approvals, the merge the user performs, deployment, verification."""

    def test_production_path(self):
        c = lambda script, *a: self.run_cli(script, *a)
        ok = lambda r: (self.assertEqual(r[0], 0, r[1]), r[1])[1]
        self.ws().clone()
        wf = ok(c(GITHUB_CLI, "install-workflow"))
        self.assertTrue(wf["pull_request"]["created"])
        rid = self.published()
        plan = ok(self.plan(rid, environment="prod"))
        self.assertIn("production_target", plan["risk_flags"])
        pending = ok(c(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT_APPROVAL"))["summary"]["pending_approval"]
        ok(c(STATE_CLI, "approve", rid, "--kind", "deployment", "--confirm", pending["hash"]))
        self.assertEqual(c(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")[0], EXIT_REFUSED)  # no phrase yet
        ok(c(STATE_CLI, "confirm-risk", rid, "--phrase", pending["high_risk_phrase"]))
        ok(c(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT"))
        self.assertEqual(c(DEPLOY_CLI, "deploy", rid)[0], EXIT_REFUSED)                        # not merged
        state = self.gh_calls()
        pr = [p for p in state["prs"] if p["head"] == "iac/" + rid][0]
        pr.update(state="MERGED", mergedAt="2026-10-10T10:00:00Z", headRefOid=self.rec(rid)["git"]["commit"])
        with open(self.gh_state, "w") as f:
            json.dump(state, f)
        dep = ok(c(DEPLOY_CLI, "deploy", rid))
        self.assertEqual(dep["status"], "succeeded")
        ok(c(STATE_CLI, "advance", rid, "--to", "VERIFICATION"))
        self.assertEqual(ok(c(DEPLOY_CLI, "verify", rid))["verdict"], "passed")
        done = ok(c(STATE_CLI, "complete", rid))
        self.assertEqual(done["record"]["status"], "completed")
        self.assertEqual(len(self.az_calls("create")), 1)
        self.assertEqual(self.remote_branches()["main"], self.main_at_start)

    def setUp(self):
        super().setUp()
        self.main_at_start = self.remote_branches()["main"]
