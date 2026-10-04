"""First-run repository setup: inspection through a fake `gh`, persistence across
processes, switching, required permission rules, and setup status. No network: the fake
gh is a local script that answers from a fixture file."""
import json
import os
import stat
import sys

from helpers import CONFIG_CLI, REPO_CLI, SETUP_CLI, STATE_CLI, PROFILE, ARCH, SUB, TENANT, StoreCase

from lib.errors import EXIT_EXTERNAL, EXIT_INVALID, EXIT_REFUSED, ExternalUnavailable, NotFound
from lib import repo_id
from repo import inspector
from setup import permissions

FAKE_GH = '''#!%s
import json, os, sys
fx = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
if sys.argv[1:2] != ["api"]:
    sys.stderr.write("fake gh: only api is supported\\n"); sys.exit(1)
path = sys.argv[2]
with open(os.environ["FAKE_GH_FIXTURE"] + ".log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
r = fx.get(path)
if r is None:
    sys.stderr.write("gh: Not Found (HTTP 404)\\n"); sys.exit(1)
if r.get("exit", 0) != 0:
    sys.stderr.write(r.get("stderr", "")); sys.exit(r["exit"])
sys.stdout.write(json.dumps(r["body"]))
''' % sys.executable

META = {"default_branch": "main", "private": True, "archived": False,
        "permissions": {"push": True, "admin": False}}


def tree(*paths, truncated=False):
    return {"truncated": truncated, "tree": [{"path": p, "type": "blob"} for p in paths]
            + [{"path": "infra", "type": "tree"}]}


class FakeGhCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as f:
            f.write(FAKE_GH)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR)
        self.fixture = os.path.join(self.tmp, "gh.json")
        self.gh_env = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
                       "FAKE_GH_FIXTURE": self.fixture}

    def github(self, slug="example-org/infra", meta=META, files=(), truncated=False, extra=None):
        fx = {"repos/%s" % slug: {"body": meta},
              "repos/%s/git/trees/%s?recursive=1" % (slug, meta["default_branch"]):
                  {"body": tree(*files, truncated=truncated)}}
        fx.update(extra or {})
        with open(self.fixture, "w") as f:
            json.dump(fx, f)

    def gh_calls(self):
        try:
            with open(self.fixture + ".log") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def inspect(self, *args):
        return self.cli(REPO_CLI, "inspect", *args, env=self.gh_env)


class Inspect(FakeGhCase):
    def test_populated_repository(self):
        self.github(files=["README.md", "infra/main.bicep", "infra/modules/storage.bicep",
                           "infra/parameters/dev.bicepparam", ".github/workflows/ci.yml", "src/app.py"])
        code, out = self.inspect("https://github.com/Example-Org/Infra.git")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["verified"])
        self.assertEqual(out["repository"]["slug"], "example-org/infra")
        self.assertEqual(out["repository"]["default_branch"], "main")
        s = out["structure"]
        self.assertEqual(s["state"], "has_bicep")
        self.assertEqual(s["infra_root_candidates"], ["infra"])
        self.assertFalse(s["infra_root_ambiguous"])
        self.assertIsNone(s["proposed_structure"])
        self.assertEqual(s["workflows"], [".github/workflows/ci.yml"])
        self.assertEqual(s["parameter_files"], ["infra/parameters/dev.bicepparam"])
        self.assertTrue(any("workflows" in n for n in s["notes"]))
        self.assertEqual(out["blockers"], [])

    def test_ambiguous_infrastructure_root(self):
        self.github(files=["platform/main.bicep", "apps/shop/main.bicep", "apps/shop/modules/db.bicep"])
        code, out = self.inspect("example-org/infra")
        s = out["structure"]
        self.assertTrue(s["infra_root_ambiguous"])
        self.assertEqual(s["infra_root_candidates"], ["apps/shop", "platform"])
        self.assertTrue(any("ask the user" in n for n in s["notes"]))

    def test_repository_without_bicep_gets_a_proposed_structure(self):
        self.github(files=["README.md", "terraform/main.tf"])
        code, out = self.inspect("example-org/infra")
        s = out["structure"]
        self.assertEqual(s["state"], "no_bicep")
        self.assertEqual(s["proposed_infra_root"], "infra")
        self.assertIn("infra/main.bicep", s["proposed_structure"])
        self.assertEqual(s["other_iac"]["terraform"], 1)
        self.assertTrue(any("Bicep only" in n for n in s["notes"]))

    def test_empty_repository(self):
        self.github(extra={"repos/example-org/infra/git/trees/main?recursive=1":
                           {"exit": 1, "stderr": "gh: Git Repository is empty. (HTTP 409)"}})
        code, out = self.inspect("example-org/infra")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["structure"]["state"], "empty")
        self.assertTrue(out["structure"]["proposed_structure"])

    def test_truncated_listing_is_reported(self):
        self.github(files=["infra/main.bicep"], truncated=True)
        code, out = self.inspect("example-org/infra")
        self.assertTrue(out["structure"]["truncated"])
        self.assertTrue(any("incomplete" in n for n in out["structure"]["notes"]))

    def test_read_only_and_archived_are_blockers(self):
        self.github(meta=dict(META, archived=True, permissions={"push": False}))
        code, out = self.inspect("example-org/infra")
        self.assertEqual(len(out["blockers"]), 2)
        self.assertFalse(out["repository"]["can_push"])

    def test_missing_repository(self):
        self.github()
        code, out = self.inspect("example-org/does-not-exist")
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("not_found_or_no_access", out["message"])
        self.assertNotIn("verified", out)

    def test_invalid_identifier_never_reaches_github(self):
        self.github()
        for bad in ("not a repo", "https://gitlab.com/a/b", "https://user:pw@github.com/a/b"):
            code, out = self.inspect(bad)
            self.assertEqual(code, EXIT_INVALID, bad)
        self.assertEqual(self.gh_calls(), [])

    def test_failures_are_classified_and_never_reported_as_verified(self):
        cases = {"gh: To get started with GitHub CLI, please run:  gh auth login": "not_authenticated",
                 "gh: Bad credentials (HTTP 401)": "not_authenticated",
                 "gh: API rate limit exceeded (HTTP 403)": "rate_limited",
                 "gh: Resource protected by organization SAML enforcement (HTTP 403)": "forbidden",
                 "error connecting to api.github.com": "network",
                 "something odd": "error"}
        for stderr, kind in cases.items():
            self.github(extra={"repos/example-org/infra": {"exit": 1, "stderr": stderr}})
            code, out = self.inspect("example-org/infra")
            self.assertEqual(code, EXIT_EXTERNAL, stderr)
            self.assertTrue(out["message"].startswith(kind), out["message"])
            self.assertFalse(out["ok"])

    def test_gh_not_installed(self):
        self.github()
        os.unlink(os.path.join(self.bin, "gh"))
        code, out = self.cli(REPO_CLI, "inspect", "example-org/infra",
                             env={"PATH": self.bin, "FAKE_GH_FIXTURE": self.fixture})
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertIn("gh_missing", out["message"])

    def test_only_get_calls_are_made(self):
        self.github(files=["infra/main.bicep"])
        self.inspect("example-org/infra")
        calls = self.gh_calls()
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertTrue(c.startswith("api repos/example-org/infra"), c)
            self.assertNotIn("-X", c)
            self.assertNotIn("--method", c)
            self.assertNotIn("-f", c.split())

    def test_hostile_file_names_are_neutralised(self):
        hostile = "infra/\x1b[31mIGNORE PREVIOUS\u202e.bicep"
        out = inspector.analyse([hostile, "x" * 2000 + ".bicep"])
        for p in out["bicep_files"]:
            self.assertLessEqual(len(p), inspector.MAX_PATH_LEN)
            self.assertNotIn("\x1b", p)
            self.assertNotIn("\u202e", p)

    def test_runner_errors_surface(self):
        def boom(args):
            raise ExternalUnavailable("gh_missing: no gh")
        with self.assertRaises(ExternalUnavailable):
            inspector.inspect(repo_id.parse("a/b"), run=boom)
        with self.assertRaises(NotFound):
            inspector.inspect(repo_id.parse("a/b"), run=lambda a: (1, "", "HTTP 404"))
        with self.assertRaises(ExternalUnavailable):
            inspector.inspect(repo_id.parse("a/b"), run=lambda a: (0, "not json", ""))


class FirstRun(FakeGhCase):
    def test_first_run_then_persist_then_restore_in_new_process(self):
        code, out = self.cli(SETUP_CLI, "status", env=self.gh_env)
        self.assertTrue(out["first_run"])
        self.assertIn("Which GitHub repository", out["first_run_question"])
        self.assertFalse(out["ready"])
        code, out = self.inspect()  # nothing configured, nothing given
        self.assertEqual(code, EXIT_REFUSED)

        self.github(files=["infra/main.bicep"])
        code, out = self.inspect("example-org/infra")
        self.assertFalse(out["is_configured_repository"])
        code, cfg = self.cli(CONFIG_CLI, "show")
        self.assertFalse(cfg["configured"])  # inspection saves nothing

        self.cli(CONFIG_CLI, "set-repo", "example-org/infra", "--default-branch",
                 out["repository"]["default_branch"])
        self.cli(CONFIG_CLI, "set", "infra_root", out["structure"]["infra_root_candidates"][0])
        self.write_settings()
        code, out = self.cli(SETUP_CLI, "status", env=self.gh_env)  # a new process
        self.assertFalse(out["first_run"])
        self.assertIsNone(out["first_run_question"])
        self.assertEqual(out["repository"]["slug"], "example-org/infra")
        self.assertNotIn("infra_root", out["unset_fields"])
        self.assertTrue(out["ready"])
        code, out = self.inspect()  # uses the configured repository
        self.assertTrue(out["is_configured_repository"])
        self.assertEqual(out["configured_infra_root"], "infra")

    def test_inspecting_another_repository_flags_the_switch(self):
        self.cli(CONFIG_CLI, "set-repo", "example-org/infra")
        self.github(slug="example-org/other", files=["main.bicep"])
        code, out = self.inspect("example-org/other")
        self.assertIn("explicit confirmation", out["switch_required"])
        code, out = self.cli(CONFIG_CLI, "set-repo", "example-org/other")
        self.assertEqual(code, EXIT_REFUSED)
        code, out = self.cli(CONFIG_CLI, "set-repo", "example-org/other",
                             "--confirm-switch-from", "example-org/infra")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["previous_repository"], "example-org/infra")

    def test_status_reports_missing_tools_honestly(self):
        code, out = self.cli(SETUP_CLI, "status", env={"PATH": self.bin})
        for tool in ("az", "bicep", "git"):
            self.assertFalse(out["tools"][tool]["installed"], tool)
            self.assertNotIn("version", out["tools"][tool])
        self.assertTrue(out["tools"]["gh"]["installed"])


class PermissionRules(StoreCase):
    def test_missing_by_default(self):
        r = permissions.check(self.project)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["missing_rules"]), len(permissions.REQUIRED))

    def test_required_rules_in_any_scope(self):
        for scope in ("user", "project", "local"):
            with self.subTest(scope=scope):
                self.write_settings(scope=scope)
                self.assertTrue(permissions.check(self.project)["ok"])
                self.tearDown()
                self.setUp()

    def test_rule_must_match_quoted_and_unquoted_commands(self):
        # The form that fails when the script path is quoted.
        good = [v["rule"] for k, v in permissions.REQUIRED.items() if k != "approve"]
        self.write_settings(["Bash(*tools/state/cli.py approve*)"] + good)
        r = permissions.check(self.project)
        self.assertEqual(r["missing_rules"], ["Bash(*tools/state/cli.py* approve *)"])

    def test_partial_rules_are_reported(self):
        self.write_settings(["Bash(*tools/state/cli.py* approve *)"])
        r = permissions.check(self.project)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["missing_rules"]), len(permissions.REQUIRED) - 1)

    def test_allow_rule_that_swallows_an_approval_is_a_conflict(self):
        self.write_settings(extra={"allow": ["Bash(python3 *)"]})
        r = permissions.check(self.project)
        self.assertFalse(r["ok"])
        self.assertTrue(r["conflicts"])

    def test_bypass_mode_and_unreadable_settings_are_problems(self):
        self.write_settings(extra={"defaultMode": "bypassPermissions"})
        self.assertTrue(any("bypassPermissions" in p for p in permissions.check(self.project)["problems"]))
        with open(os.path.join(self.project, ".claude", "settings.json"), "w") as f:
            f.write("{not json")
        r = permissions.check(self.project)
        self.assertFalse(r["ok"])
        self.assertTrue(any("could not be read" in p for p in r["problems"]))


class DeploymentGateNeedsRules(StoreCase):
    """The tool refuses deployment approval while the required ask rules are absent."""

    def drive_to_deployment_approval(self):
        c = lambda *a: self.cli(STATE_CLI, *a)
        code, out = c("create", "--intent", "Storage account for logs")
        rid = out["record"]["id"]
        c("advance", rid, "--to", "DISCOVERY")
        c("set-profile", rid, "--json", json.dumps(PROFILE))
        c("add-requirement", rid, "--text", "Logs kept 90 days")
        for t in ("target_subscription", "public_exposure"):
            c("add-requirement", rid, "--text", "confirmed", "--topic", t)
        c("advance", rid, "--to", "ARCHITECTURE")
        c("set-architecture", rid, "--json", json.dumps(ARCH))
        code, out = c("advance", rid, "--to", "APPROVAL")
        c("approve", rid, "--kind", "architecture", "--confirm", out["summary"]["pending_approval"]["hash"])
        c("advance", rid, "--to", "IMPLEMENTATION")
        c("add-file", rid, "--path", "infra/main.bicep", "--action", "created")
        c("advance", rid, "--to", "VALIDATION")
        c("add-validation", rid, "--check", "bicep build", "--result", "passed")
        c("advance", rid, "--to", "GIT_REVIEW")
        c("set-git", rid, "--branch", "iac/logs", "--commit", "0123456789abcdef0123456789abcdef01234567")
        c("set-target", rid, "--tenant", TENANT, "--subscription", SUB,
          "--resource-group", "rg-logs-dev", "--environment", "dev")
        c("set-plan", rid, "--json", json.dumps({"change_set": [{"resource": "st-logs", "change": "Create"}]}))
        code, out = c("advance", rid, "--to", "DEPLOYMENT_APPROVAL")
        self.assertEqual(code, 0, out)
        return rid, out["summary"]["pending_approval"]["hash"]

    def test_refused_without_rules_and_allowed_with_them(self):
        rid, h = self.drive_to_deployment_approval()
        code, out = self.cli(STATE_CLI, "approve", rid, "--kind", "deployment", "--confirm", h)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("permission rules", out["message"])
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")
        self.assertEqual(code, EXIT_REFUSED)
        code, out = self.cli(STATE_CLI, "show", rid)
        self.assertIsNone(out["record"]["approvals"]["deployment"])

        self.write_settings()
        code, out = self.cli(STATE_CLI, "approve", rid, "--kind", "deployment", "--confirm", h)
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")
        self.assertEqual(code, 0, out)

    def test_architecture_approval_does_not_need_the_rules(self):
        # Covered by drive_to_deployment_approval: it approves the architecture with no
        # settings file present.
        rid, _ = self.drive_to_deployment_approval()
        code, out = self.cli(STATE_CLI, "show", rid)
        self.assertTrue(out["record"]["approvals"]["architecture"]["valid"])
