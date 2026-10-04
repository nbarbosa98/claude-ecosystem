"""Milestone 3: working copy, Bicep inventory, recorded files, validation pipeline.

GitHub is a local bare repository reached through git's url.<base>.insteadOf, so the
tools run their real `git clone https://github.com/...` without any network. The Bicep
CLI and Checkov are fake scripts that answer from the file contents; tests marked "real"
run only when the real tools are installed.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

from helpers import (ARCH, CONFIG_CLI, PLUGIN, PROFILE, STATE_CLI, TOOLS, StoreCase)

from config.store import ConfigStore
from lib.errors import (EXIT_EXTERNAL, EXIT_INVALID, EXIT_REFUSED, ExternalUnavailable, NotFound,
                        Refused)
from state import machine as M
from state.store import StateStore
from validate import checks
from workspace import bicep_scan
from workspace.manager import Workspace

WORKSPACE_CLI = os.path.join(TOOLS, "workspace", "cli.py")
VALIDATE_CLI = os.path.join(TOOLS, "validate", "cli.py")

MAIN = """targetScope = 'resourceGroup'
@description('Environment name')
param environment string
param location string = resourceGroup().location
@secure()
param adminLogin string
module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: { location: location, name: 'stlogs${environment}' }
}
output storageId string = storage.outputs.id
"""
STORAGE = """param location string
param name string
resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: 'kv-shared' }
output id string = sa.id
"""
PARAMS = "using '../main.bicep'\nparam environment = 'dev'\n"

FAKE_BICEP = '''#!%s
import os, sys
sub, path = sys.argv[1], sys.argv[2]
if sub == "--version":
    print("Bicep CLI version 0.0.0 (fake)"); sys.exit(0)
text = open(path).read()
full = os.path.abspath(path)
code = 0
if "SYNTAX_ERROR" in text:
    sys.stderr.write("%%s(1,1) : Error BCP018: Expected the \\"}\\" character at this location. [https://aka.ms/x]\\n" %% full); code = 1
if "UNUSED" in text:
    sys.stderr.write("%%s(2,7) : Warning no-unused-params: Parameter \\"x\\" is declared but never used. [https://aka.ms/y]\\n" %% full)
if "CRASH" in text:
    sys.stderr.write("Unhandled exception\\n"); code = 3
if sub in ("build", "build-params") and code == 0:
    sys.stdout.write("{}")
sys.exit(code)
''' % sys.executable

FAKE_CHECKOV = '''#!%s
import json, os, sys
if "--version" in sys.argv:
    print("0.0.0-fake"); sys.exit(0)
d = sys.argv[sys.argv.index("-d") + 1]
failed, skipped, passed = [], [], 0
for folder, _, files in os.walk(d):
    for n in files:
        if not n.endswith(".bicep"):
            continue
        t = open(os.path.join(folder, n)).read()
        rel = "/" + os.path.relpath(os.path.join(folder, n), d)
        item = {"check_id": "CKV_AZURE_35", "check_name": "Default network access rule set to deny",
                "file_path": rel, "resource": "Microsoft.Storage/storageAccounts.sa",
                "file_line_range": [3, 8], "guideline": "https://example.invalid/g"}
        if "GARBAGE" in t:
            sys.stdout.write("not json"); sys.exit(2)
        if "checkov:skip=CKV_AZURE_35" in t:
            skipped.append(dict(item, check_result={"result": "SKIPPED", "suppress_comment": "agreed with user"}))
        elif "PUBLIC" in t:
            failed.append(item)
        elif "resource " in t:
            passed += 1
out = {"check_type": "bicep", "results": {"failed_checks": failed, "skipped_checks": skipped},
       "summary": {"passed": passed, "failed": len(failed), "skipped": len(skipped), "parsing_errors": 0}}
sys.stdout.write(json.dumps(out)); sys.exit(1 if failed else 0)
''' % sys.executable


def git(args, cwd, env):
    p = subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True, text=True)
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout.strip()


class WorkCase(StoreCase):
    """A project configured for example-org/infra, whose 'GitHub' is a local bare repo."""

    FILES = {"README.md": "# infra\n", "infra/main.bicep": MAIN,
             "infra/modules/storage.bicep": STORAGE, "infra/parameters/dev.bicepparam": PARAMS,
             "infra/README.md": "# Infrastructure\n", "src/app.py": "print('hi')\n"}

    def setUp(self):
        super().setUp()
        self.remotes = os.path.join(self.tmp, "remotes")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.env = {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "url.file://%s/.insteadOf" % self.remotes,
            "GIT_CONFIG_VALUE_0": "https://github.com/",
            "GIT_CONFIG_KEY_1": "init.defaultBranch", "GIT_CONFIG_VALUE_1": "main",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
        }
        self._genv = mock.patch.dict(os.environ, self.env)
        self._genv.start()
        self.full_env = dict(os.environ)
        self.seed("example-org/infra", self.FILES)
        store = ConfigStore(self.project)
        store.set_repo("example-org/infra", default_branch="main")
        store.set_value("infra_root", "infra")
        self.install("bicep", FAKE_BICEP)
        self.install("checkov", FAKE_CHECKOV)
        # A PATH holding only the fakes and git, so "tool missing" tests cannot pick up a
        # real bicep or checkov installed on the machine.
        os.symlink(shutil.which("git"), os.path.join(self.bin, "git"))
        self.only_fakes = dict(self.env, PATH=self.bin)

    def tearDown(self):
        self._genv.stop()
        super().tearDown()

    def install(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def seed(self, slug, files, empty=False):
        bare = os.path.join(self.remotes, slug + ".git")
        os.makedirs(bare)
        git(["init", "--quiet", "--bare", bare], self.tmp, self.full_env)
        if empty:
            return bare
        work = os.path.join(self.tmp, "seed-" + slug.replace("/", "-"))
        git(["clone", "--quiet", bare, work], self.tmp, self.full_env)
        self.commit(work, files, "initial")
        git(["push", "--quiet", "origin", "HEAD:main"], work, self.full_env)
        return bare

    def commit(self, work, files, message):
        for rel, text in files.items():
            full = os.path.join(work, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        git(["add", "-A"], work, self.full_env)
        git(["commit", "--quiet", "-m", message], work, self.full_env)

    def ws(self):
        return Workspace(self.project, ConfigStore(self.project).load())

    def write(self, rel, text):
        full = os.path.join(self.ws().dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def request_in(self, state):
        """A request driven to IMPLEMENTATION or VALIDATION, on its work branch."""
        store = StateStore(self.project)
        rid = store.create("Storage account for logs")["id"]
        do = lambda fn, *a: store.mutate(rid, lambda r, e: fn(r, *a))
        do(M.advance, "DISCOVERY")
        do(M.set_profile, PROFILE)
        do(M.add_requirement, "Logs kept 90 days")
        for t in M.unconfirmed_topics(store.load(rid)):
            do(M.add_requirement, "confirmed", t)
        do(M.advance, "ARCHITECTURE")
        do(M.set_architecture, ARCH)
        do(M.advance, "APPROVAL")
        do(M.approve, "architecture", M.pending_approval(store.load(rid))["hash"])
        do(M.advance, "IMPLEMENTATION")
        ws = self.ws()
        if not ws.exists():
            ws.clone()
        ws.begin(rid)
        if state == "VALIDATION":
            self.write("infra/modules/storage.bicep", STORAGE + "// change\n")
            code, out = self.cli(WORKSPACE_CLI, "record-files", rid, env=self.env)
            assert code == 0, out
            do(M.advance, "VALIDATION")
        return rid


class WorkingCopy(WorkCase):
    def test_clone_lands_under_the_project_and_is_ignored_by_it(self):
        ws = self.ws()
        self.assertEqual(ws.status(), {"exists": False, "path": ws.dir, "repository": "example-org/infra"})
        st = ws.clone()
        self.assertTrue(ws.dir.startswith(os.path.join(self.project, ".iac-azure-agent", "workspace")))
        self.assertTrue(st["clean"])
        self.assertEqual(st["branch"], "main")
        self.assertTrue(os.path.isfile(os.path.join(ws.dir, "infra", "main.bicep")))
        with open(os.path.join(self.project, ".iac-azure-agent", ".gitignore")) as f:
            self.assertIn("*", f.read().split())
        with self.assertRaises(Refused):
            ws.clone()  # already there

    def test_no_repository_configured(self):
        ConfigStore(self.project).clear(self.project)
        with self.assertRaises(Refused):
            self.ws()
        code, out = self.cli(WORKSPACE_CLI, "status", env=self.env)
        self.assertEqual(code, EXIT_REFUSED)

    def test_missing_repository_and_missing_git(self):
        ConfigStore(self.project).set_repo("example-org/gone", "example-org/infra", "main")
        with self.assertRaises(ExternalUnavailable) as cm:
            self.ws().clone()
        self.assertIn("Nothing was changed", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.ws().dir, ".git")))
        os.unlink(os.path.join(self.bin, "git"))
        code, out = self.cli(WORKSPACE_CLI, "clone", env={"PATH": self.bin})
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertIn("git_missing", out["message"])

    def test_foreign_clone_is_refused_and_left_alone(self):
        ws = self.ws()
        ws.clone()
        git(["remote", "set-url", "origin", "https://github.com/example-org/other.git"], ws.dir, self.full_env)
        for action in (ws.status, ws.sync, lambda: ws.begin("req-20260101-000000-abcdef")):
            with self.assertRaises(Refused) as cm:
                action()
            self.assertIn("left untouched", str(cm.exception))

    def test_sync_fast_forwards_a_clean_default_branch(self):
        ws = self.ws()
        ws.clone()
        before = ws.head()
        seed = os.path.join(self.tmp, "seed-example-org-infra")
        self.commit(seed, {"infra/modules/network.bicep": "param location string\n"}, "upstream")
        git(["push", "--quiet", "origin", "HEAD:main"], seed, self.full_env)
        out = ws.sync()
        self.assertTrue(out["fast_forwarded"])
        self.assertNotEqual(before, ws.head())

    def test_sync_never_discards_local_changes(self):
        ws = self.ws()
        ws.clone()
        self.write("infra/main.bicep", MAIN + "// my edit\n")
        with self.assertRaises(Refused):
            ws.sync()
        with open(os.path.join(ws.dir, "infra", "main.bicep")) as f:
            self.assertIn("// my edit", f.read())

    def test_diverged_default_branch_is_refused(self):
        ws = self.ws()
        ws.clone()
        self.commit(ws.dir, {"infra/local.bicep": "param a string\n"}, "local only")
        seed = os.path.join(self.tmp, "seed-example-org-infra")
        self.commit(seed, {"infra/remote.bicep": "param b string\n"}, "upstream")
        git(["push", "--quiet", "origin", "HEAD:main"], seed, self.full_env)
        head = ws.head()
        with self.assertRaises(Refused) as cm:
            ws.sync()
        self.assertIn("diverged", str(cm.exception))
        self.assertEqual(head, ws.head())

    def test_begin_creates_a_local_branch_and_pushes_nothing(self):
        rid = self.request_in("IMPLEMENTATION")
        ws = self.ws()
        self.assertEqual(ws.branch(), "iac/" + rid)
        bare = os.path.join(self.remotes, "example-org/infra.git")
        self.assertEqual(git(["branch", "--list"], bare, self.full_env).replace("*", "").split(), ["main"])
        self.assertFalse(ws.begin(rid)["created"])  # returning to it is not an error

    def test_begin_needs_an_approved_request_and_a_clean_tree(self):
        store = StateStore(self.project)
        rid = store.create("Not approved yet")["id"]
        self.ws().clone()
        code, out = self.cli(WORKSPACE_CLI, "begin", rid, env=self.env)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("after the architecture is approved", out["message"])
        self.write("infra/main.bicep", MAIN + "// stray\n")
        with self.assertRaises(Refused):
            self.ws().begin(rid)
        self.assertEqual(self.ws().branch(), "main")

    def test_empty_repository_can_be_cloned_and_started(self):
        self.seed("example-org/fresh", {}, empty=True)
        ConfigStore(self.project).set_repo("example-org/fresh", "example-org/infra", "main")
        ws = self.ws()
        st = ws.clone()
        self.assertIsNone(st["head"])
        self.assertTrue(ws.begin("req-20260101-000000-abcdef")["created"])
        self.assertEqual(ws.branch(), "iac/req-20260101-000000-abcdef")


class Inventory(WorkCase):
    def test_existing_bicep_is_described(self):
        self.ws().clone()
        code, out = self.cli(WORKSPACE_CLI, "inventory", env=self.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["entry_points"], ["infra/main.bicep"])
        self.assertEqual(out["modules"], ["infra/modules/storage.bicep"])
        self.assertEqual(out["parameter_files"], ["infra/parameters/dev.bicepparam"])
        self.assertEqual(out["resource_types"]["Microsoft.Storage/storageAccounts"], ["2023-05-01"])
        main = out["files"]["infra/main.bicep"]
        self.assertEqual(main["target_scope"], "resourceGroup")
        params = {p["name"]: p for p in main["params"]}
        self.assertTrue(params["adminLogin"]["secure"])
        self.assertFalse(params["environment"]["secure"])
        res = out["files"]["infra/modules/storage.bicep"]["resources"]
        self.assertEqual([r["existing"] for r in res], [False, True])
        self.assertEqual(out["files"]["infra/parameters/dev.bicepparam"]["using"], "../main.bicep")
        self.assertNotIn("src/app.py", json.dumps(out))  # only the infrastructure root is read

    def test_scan_neutralises_hostile_names(self):
        info = bicep_scan.scan_text("param evil\u202ename string\nresource r 'A.B/c@2020-01-01' = {}\n")
        self.assertEqual(info["resources"][0]["type"], "A.B/c")
        self.assertNotIn("\u202e", json.dumps(info, ensure_ascii=False))

    def test_inventory_before_clone(self):
        code, out = self.cli(WORKSPACE_CLI, "inventory", env=self.env)
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("no working copy", out["message"])


class RecordFiles(WorkCase):
    def test_files_come_from_git_not_from_the_caller(self):
        rid = self.request_in("IMPLEMENTATION")
        self.write("infra/modules/network.bicep", "param location string\n")
        self.write("infra/main.bicep", MAIN + "// changed\n")
        os.unlink(os.path.join(self.ws().dir, "infra", "parameters", "dev.bicepparam"))
        code, out = self.cli(WORKSPACE_CLI, "record-files", rid, env=self.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["recorded"], [
            {"path": "infra/main.bicep", "action": "modified"},
            {"path": "infra/modules/network.bicep", "action": "created"},
            {"path": "infra/parameters/dev.bicepparam", "action": "deleted"}])
        self.assertEqual(StateStore(self.project).load(rid)["files"], out["recorded"])

    def test_change_outside_the_infrastructure_root_is_refused(self):
        rid = self.request_in("IMPLEMENTATION")
        self.write("infra/main.bicep", MAIN + "// changed\n")
        self.write("src/app.py", "print('tampered')\n")
        self.write(".github/workflows/deploy.yml", "on: push\n")
        code, out = self.cli(WORKSPACE_CLI, "record-files", rid, env=self.env)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("src/app.py", out["message"])
        self.assertIn(".github/workflows/deploy.yml", out["message"])
        self.assertEqual(StateStore(self.project).load(rid)["files"], [])

    def test_nothing_changed_and_wrong_branch(self):
        rid = self.request_in("IMPLEMENTATION")
        code, out = self.cli(WORKSPACE_CLI, "record-files", rid, env=self.env)
        self.assertEqual(code, EXIT_REFUSED)
        git(["checkout", "--quiet", "main"], self.ws().dir, self.full_env)
        code, out = self.cli(WORKSPACE_CLI, "record-files", rid, env=self.env)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("not on this request's branch", out["message"])

    def test_tree_hash_follows_content(self):
        self.ws().clone()
        a = self.ws().tree_hash()
        self.write("src/app.py", "x = 1\n")           # outside the infrastructure root
        self.assertEqual(a, self.ws().tree_hash())
        self.write("infra/main.bicep", MAIN + " ")
        self.assertNotEqual(a, self.ws().tree_hash())


class Validation(WorkCase):
    def run_checks(self, rid=None):
        args = ["run"] + ([rid] if rid else [])
        code, out = self.cli(VALIDATE_CLI, *args, env=self.env)
        self.assertEqual(code, 0, out)
        return out, {r["check"]: r for r in out["results"]}

    def test_clean_tree_passes_every_check(self):
        self.ws().clone()
        out, by = self.run_checks()
        self.assertEqual({k: v["result"] for k, v in by.items()}, {
            "structure": "passed", "bicep-only": "passed", "secret-scan": "passed",
            "bicep-build": "passed", "bicep-build-params": "passed", "bicep-lint": "passed",
            "security-scan": "passed"})
        self.assertEqual(out["verdict"], "passed")
        self.assertFalse(out["recorded"])
        self.assertTrue(out["not_run"])  # Azure validation and what-if are named as not run

    def test_build_error_fails_with_location(self):
        self.ws().clone()
        self.write("infra/main.bicep", MAIN + "// SYNTAX_ERROR\n")
        out, by = self.run_checks()
        self.assertEqual(by["bicep-build"]["result"], "failed")
        f = by["bicep-build"]["findings"][0]
        self.assertEqual((f["file"], f["line"], f["code"]), ("infra/main.bicep", 1, "BCP018"))
        self.assertEqual(out["verdict"], "failed")

    def test_lint_warning_is_a_warning_not_a_pass(self):
        self.ws().clone()
        self.write("infra/modules/storage.bicep", STORAGE + "// UNUSED\n")
        out, by = self.run_checks()
        self.assertEqual(by["bicep-lint"]["result"], "warning")
        self.assertEqual(out["verdict"], "passed_with_warnings")

    def test_tool_crash_without_diagnostics_is_a_failure(self):
        self.ws().clone()
        self.write("infra/main.bicep", MAIN + "// CRASH\n")
        out, by = self.run_checks()
        self.assertEqual(by["bicep-build"]["result"], "failed")
        self.assertEqual(by["bicep-build"]["findings"][0]["code"], "exit-3")

    def test_missing_tools_are_unavailable_never_passed(self):
        self.ws().clone()
        os.unlink(os.path.join(self.bin, "bicep"))
        os.unlink(os.path.join(self.bin, "checkov"))
        code, out = self.cli(VALIDATE_CLI, "run", env=self.only_fakes)
        by = {r["check"]: r["result"] for r in out["results"]}
        for check in ("bicep-build", "bicep-build-params", "bicep-lint", "security-scan"):
            self.assertEqual(by[check], "unavailable", check)
        self.assertEqual(out["verdict"], "incomplete")
        self.assertIn("not a pass", out["meaning"])
        self.assertIsNone(out["tools"]["bicep"])

    def test_unreadable_scanner_report_is_unavailable(self):
        self.ws().clone()
        self.write("infra/modules/storage.bicep", STORAGE + "// GARBAGE\n")
        out, by = self.run_checks()
        self.assertEqual(by["security-scan"]["result"], "unavailable")

    def test_security_finding_fails_and_suppression_is_surfaced(self):
        self.ws().clone()
        self.write("infra/modules/storage.bicep", STORAGE + "// PUBLIC\n")
        out, by = self.run_checks()
        self.assertEqual(by["security-scan"]["result"], "failed")
        f = by["security-scan"]["findings"][0]
        self.assertEqual((f["code"], f["file"]), ("CKV_AZURE_35", "infra/modules/storage.bicep"))
        self.write("infra/modules/storage.bicep", STORAGE + "// checkov:skip=CKV_AZURE_35: agreed\n")
        out, by = self.run_checks()
        self.assertEqual(by["security-scan"]["result"], "warning")  # suppressed is not passed
        self.assertIn("suppressed", by["security-scan"]["findings"][0])

    def test_secret_in_code_fails_without_echoing_it(self):
        self.ws().clone()
        secret = "ghp_" + "A1b2C3d4" * 5
        self.write("infra/parameters/dev.bicepparam", PARAMS + "param pat = '%s'\n" % secret)
        code, raw = self.cli(VALIDATE_CLI, "run", env=self.env)
        by = {r["check"]: r for r in raw["results"]}
        self.assertEqual(by["secret-scan"]["result"], "failed")
        self.assertEqual(by["secret-scan"]["findings"][0]["file"], "infra/parameters/dev.bicepparam")
        self.assertNotIn(secret, json.dumps(raw))

    def test_hard_coded_subscription_is_a_warning(self):
        self.ws().clone()
        self.write("infra/main.bicep", MAIN + "var s = '/subscriptions/00000000-0000-0000-0000-00000000000b/x'\n")
        out, by = self.run_checks()
        self.assertEqual(by["secret-scan"]["result"], "warning")

    def test_structure_and_bicep_only(self):
        self.ws().clone()
        os.unlink(os.path.join(self.ws().dir, "infra", "README.md"))
        os.unlink(os.path.join(self.ws().dir, "infra", "modules", "storage.bicep"))
        self.write("infra/legacy/main.tf", "resource {}\n")
        self.write("infra/parameters/test.bicepparam", "using '../missing.bicep'\n")
        out, by = self.run_checks()
        msgs = " ".join(f["message"] for f in by["structure"]["findings"])
        self.assertEqual(by["structure"]["result"], "failed")
        for expected in ("README.md is missing", "references module", "missing.bicep"):
            self.assertIn(expected, msgs)
        self.assertEqual(by["bicep-only"]["result"], "failed")

    def test_no_parameter_files_is_skipped_not_passed(self):
        self.ws().clone()
        os.unlink(os.path.join(self.ws().dir, "infra", "parameters", "dev.bicepparam"))
        out, by = self.run_checks()
        self.assertEqual(by["bicep-build-params"]["result"], "skipped")
        self.assertEqual(out["verdict"], "incomplete")

    def test_results_are_recorded_and_bound_to_the_files(self):
        rid = self.request_in("VALIDATION")
        out, by = self.run_checks(rid)
        self.assertTrue(out["recorded"])
        rec = StateStore(self.project).load(rid)
        self.assertEqual(rec["validation_run"]["tree_hash"], self.ws().tree_hash())
        self.assertEqual(M.summarise_checks(rec["validation"])["verdict"], "passed")
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW")
        self.assertEqual(code, 0, out)

    def test_failed_validation_blocks_and_rerun_replaces_results(self):
        rid = self.request_in("VALIDATION")
        self.write("infra/main.bicep", MAIN + "// SYNTAX_ERROR\n")
        self.run_checks(rid)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("bicep-build", out["message"])
        self.write("infra/main.bicep", MAIN)
        self.run_checks(rid)
        rec = StateStore(self.project).load(rid)
        self.assertEqual(M.summarise_checks(rec["validation"])["verdict"], "passed")
        self.assertTrue(any(c.get("stale") for c in rec["validation"]))  # history kept

    def test_incomplete_validation_needs_explicit_acceptance(self):
        rid = self.request_in("VALIDATION")
        os.unlink(os.path.join(self.bin, "checkov"))
        code, out = self.cli(VALIDATE_CLI, "run", rid, env=self.only_fakes)
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("security-scan", out["message"])
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW", "--accept-incomplete")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["record"]["accepted_incomplete"][0]["checks"], ["security-scan"])

    def test_recording_needs_validation_state_and_the_request_branch(self):
        rid = self.request_in("IMPLEMENTATION")
        code, out = self.cli(VALIDATE_CLI, "run", rid, env=self.env)
        self.assertEqual(code, EXIT_REFUSED)
        rid2 = self.request_in("VALIDATION")
        git(["checkout", "--quiet", "main"], self.ws().dir, self.full_env)
        code, out = self.cli(VALIDATE_CLI, "run", rid2, env=self.env)
        self.assertEqual(code, EXIT_REFUSED)

    def test_moving_back_clears_the_validation_run(self):
        rid = self.request_in("VALIDATION")
        self.run_checks(rid)
        store = StateStore(self.project)
        store.mutate(rid, lambda r, e: M.back(r, "IMPLEMENTATION", "rework"))
        rec = store.load(rid)
        self.assertIsNone(rec["validation_run"])
        self.assertEqual(M.summarise_checks(rec["validation"])["verdict"], "none")

    def test_diagnostic_parser(self):
        text = ('/w/infra/a.bicep(4,7) : Warning no-unused-params: Parameter "x" is unused. [u]\n'
                "noise\n/w/infra/b.bicep(2,1) : Error BCP018: Expected. [u]\n")
        d = checks.parse_diagnostics(text, "/w")
        self.assertEqual([(x["file"], x["line"], x["level"], x["code"]) for x in d],
                         [("infra/a.bicep", 4, "warning", "no-unused-params"),
                          ("infra/b.bicep", 2, "error", "BCP018")])


REAL_BICEP = shutil.which("bicep")
REAL_CHECKOV = shutil.which("checkov")


class RealTools(WorkCase):
    """The same pipeline against the real Bicep CLI and Checkov, when they are installed."""

    def setUp(self):
        super().setUp()
        os.unlink(os.path.join(self.bin, "bicep"))
        os.unlink(os.path.join(self.bin, "checkov"))

    @unittest.skipUnless(REAL_BICEP, "the Bicep CLI is not installed")
    def test_real_bicep_build_and_lint(self):
        self.ws().clone()
        self.write("infra/main.bicep", MAIN.replace("@secure()\nparam adminLogin string\n", ""))
        code, out = self.cli(VALIDATE_CLI, "run", env=self.env)
        by = {r["check"]: r for r in out["results"]}
        self.assertIn(by["bicep-build"]["result"], ("passed", "warning"), by["bicep-build"])
        self.assertIn(by["bicep-build-params"]["result"], ("passed", "warning"), by["bicep-build-params"])
        self.write("infra/main.bicep", "resource x 'Microsoft.Storage/storageAccounts@2023-05-01' = { nam: 1\n")
        code, out = self.cli(VALIDATE_CLI, "run", env=self.env)
        by = {r["check"]: r for r in out["results"]}
        self.assertEqual(by["bicep-build"]["result"], "failed")
        self.assertTrue(any(f["code"].startswith("BCP") for f in by["bicep-build"]["findings"]))

    @unittest.skipUnless(REAL_CHECKOV, "Checkov is not installed")
    def test_real_checkov_finds_an_open_storage_account(self):
        self.ws().clone()
        code, out = self.cli(VALIDATE_CLI, "run", env=self.env)
        sec = {r["check"]: r for r in out["results"]}["security-scan"]
        self.assertEqual(sec["result"], "failed", sec)
        self.assertTrue(all(f["code"].startswith("CKV") for f in sec["findings"]))
        self.assertTrue(all(f["file"].startswith("infra/") for f in sec["findings"]))
