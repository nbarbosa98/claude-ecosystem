"""Milestone 5: Azure planning, approved deployment, recovery and verification.

Azure is a fake `az` script that keeps its state in a JSON file. No test contacts Azure.
The last test drives the whole workflow, request to completion, through the CLIs.
"""
import json
import os
import sys

from helpers import ARCH, CONFIG_CLI, DISCOVERY_CLI, PROFILE, STATE_CLI, SUB, TENANT, TOOLS
from test_publish import GITHUB_CLI, PublishCase
from test_workspace_validate import MAIN, STORAGE, VALIDATE_CLI, WORKSPACE_CLI

from config.store import ConfigStore
from deploy import whatif
from deploy.azrun import ADVICE, Az, AzFailure, classify
from lib.errors import EXIT_EXTERNAL, EXIT_INVALID, EXIT_REFUSED
from state import machine as M
from state.store import StateStore

DEPLOY_CLI = os.path.join(TOOLS, "deploy", "cli.py")
OTHER_SUB = "00000000-0000-0000-0000-00000000000c"
P = "/subscriptions/%s" % SUB
RG = P + "/resourceGroups/rg-logs-dev"
ST = RG + "/providers/Microsoft.Storage/storageAccounts/stlogsdev"

FAKE_AZ = '''#!%s
import json, os, sys
path = os.environ["FAKE_AZ_STATE"]
s = json.load(open(path))
args = [a for a in sys.argv[1:] if a not in ("--output", "json")]
s.setdefault("calls", []).append(args)
def done(obj=None, code=0, err=""):
    json.dump(s, open(path, "w"))
    if err: sys.stderr.write(err + "\\n")
    if obj is not None: sys.stdout.write(json.dumps(obj))
    sys.exit(code)
def fail(key):
    f = s.get("fail", {}).get(key)
    if isinstance(f, list):
        if f:
            msg = f.pop(0); done(code=1, err=msg)
    elif f:
        done(code=1, err=f)
head = " ".join(args[:3])
if args[:2] == ["account", "show"]:
    fail("account"); done(s["account"])
if args[0] == "deployment" and args[1] == "operation":
    done(s.get("ops", []))
if args[0] == "deployment":
    verb = args[2]
    if verb == "validate":
        fail("validate"); done({"properties": {"provisioningState": "Succeeded"}})
    if verb == "what-if":
        fail("what-if"); done(s["whatif"])
    if verb == "create":
        s["create_env_key"] = os.environ.get("IAC_TEST_INPUT", "")
        if s.get("fail", {}).get("create"):
            s["exists"] = True; s["provisioning"] = "Failed"
            fail("create")
        s["exists"] = True; s["provisioning"] = "Succeeded"
        done({"properties": {"provisioningState": "Succeeded"}})
    if verb == "show":
        if not s.get("exists"):
            done(code=3, err="ERROR: (DeploymentNotFound) Deployment could not be found.")
        done({"properties": {"provisioningState": s.get("provisioning", "Succeeded"),
                             "correlationId": "00000000-0000-0000-0000-0000000000cc",
                             "timestamp": "2026-10-05T00:00:00Z",
                             "outputs": s.get("outputs", {}),
                             "outputResources": [{"id": c["resourceId"]} for c in s["whatif"]["changes"]
                                                 if c["changeType"] in ("Create", "Modify")]}})
if args[:2] == ["group", "show"]:
    if not s.get("exists"): done(code=3, err="ERROR: (ResourceGroupNotFound) not found")
    done({"properties": {"provisioningState": "Succeeded"}})
if args[:2] == ["resource", "show"]:
    rid = args[args.index("--ids") + 1]
    if not s.get("exists") or rid in s.get("missing", []):
        done(code=3, err="ERROR: (ResourceNotFound) The Resource was not found.")
    done({"properties": {"provisioningState": s.get("resource_state", "Succeeded")}})
if args[:2] == ["resource", "list"]:
    done(s.get("rg_resources", []))
if args[:2] == ["vm", "get-instance-view"]:
    done(["ProvisioningState/succeeded", s.get("power", "PowerState/running")])
done(code=1, err="fake az: unsupported " + " ".join(args))
''' % sys.executable


def change(kind, rid, after=None, delta=None, reason=None):
    return {"changeType": kind, "resourceId": rid, "before": None, "after": after, "delta": delta,
            "unsupportedReason": reason}


CREATE_STORAGE = {"status": "Succeeded", "error": None, "potentialChanges": [],
                  "changes": [change("Create", RG), change("Create", ST, {"properties": {"publicNetworkAccess": "Disabled"}})]}


class DeployCase(PublishCase):
    FILES = dict(PublishCase.FILES, **{
        "infra/main.bicep": MAIN.replace("targetScope = 'resourceGroup'", "targetScope = 'subscription'")})

    def setUp(self):
        super().setUp()
        self.install("az", FAKE_AZ)
        # build-params output depends on an environment variable, like a real parameter file
        # that reads one, so a changed input changes the inputs hash.
        from test_workspace_validate import FAKE_BICEP
        self.install("bicep", FAKE_BICEP.replace('sys.stdout.write("{}")',
                                                 'sys.stdout.write("{%s}" % os.environ.get("IAC_TEST_INPUT", ""))'))
        self.az_state = os.path.join(self.tmp, "az-state.json")
        self.set_az(account={"id": SUB, "tenantId": TENANT, "name": "lab", "state": "Enabled",
                             "user": {"name": "tester@example.invalid", "type": "user"}},
                    whatif=CREATE_STORAGE)
        self.env = dict(self.env, FAKE_AZ_STATE=self.az_state)

    def set_az(self, **kw):
        try:
            with open(self.az_state) as f:
                s = json.load(f)
        except OSError:
            s = {}
        s.update(kw)
        with open(self.az_state, "w") as f:
            json.dump(s, f)

    def az(self):
        with open(self.az_state) as f:
            return json.load(f)

    def az_calls(self, verb):
        return [c for c in self.az().get("calls", []) if c[:1] == ["deployment"] and c[2:3] == [verb]]

    def run_cli(self, script, *args, env=None):
        return self.cli(script, *args, env=dict(self.env, **(env or {})))

    def published(self):
        rid = self.ready()
        code, out = self.publish(rid)
        self.assertEqual(code, 0, out)
        return rid

    def plan(self, rid, *extra, environment="dev", sub=SUB, env=None):
        return self.run_cli(DEPLOY_CLI, "plan", rid, "--environment", environment, "--tenant", TENANT,
                            "--subscription", sub, "--location", "westeurope", *extra, env=env)

    def approved(self, environment="dev"):
        """A request in DEPLOYMENT with a valid approval."""
        rid = self.published()
        code, out = self.plan(rid, environment=environment)
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT_APPROVAL")
        self.assertEqual(code, 0, out)
        p = out["summary"]["pending_approval"]
        code, out = self.cli(STATE_CLI, "approve", rid, "--kind", "deployment", "--confirm", p["hash"])
        self.assertEqual(code, 0, out)
        if p.get("high_risk_phrase"):
            self.cli(STATE_CLI, "confirm-risk", rid, "--phrase", p["high_risk_phrase"])
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")
        self.assertEqual(code, 0, out)
        return rid

    def rec(self, rid):
        return StateStore(self.project).load(rid)


class Plan(DeployCase):
    def test_plan_records_target_change_set_and_changes_nothing(self):
        rid = self.published()
        code, out = self.plan(rid)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["counts"], {"Create": 2})
        self.assertEqual(out["azure_validation"], "passed")
        self.assertEqual(out["deployment"]["scope"], "subscription")
        self.assertEqual(out["deployment"]["name"], "iac-" + rid)
        rec = self.rec(rid)
        self.assertEqual(rec["target"], {"tenant_id": TENANT, "subscription_id": SUB,
                                         "resource_group": "rg-logs-dev", "environment": "dev"})
        ids = [c["id"] for c in rec["plan"]["change_set"]]
        self.assertEqual(ids, ["/resourceGroups/rg-logs-dev",
                               "/resourceGroups/rg-logs-dev/providers/Microsoft.Storage/storageAccounts/stlogsdev"])
        self.assertNotIn(SUB, json.dumps(rec["plan"]["change_set"]))   # no subscription id in approved content
        self.assertEqual(self.az_calls("create"), [])
        self.assertEqual(len(self.az_calls("validate")), 1)
        self.assertEqual(rec["state"], "GIT_REVIEW")

    def test_only_published_code_is_planned(self):
        rid = self.ready()
        code, out = self.plan(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("not been published", out["message"])
        self.publish(rid)
        self.write("infra/modules/storage.bicep", STORAGE + "// edited after publishing\n")
        code, out = self.plan(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(self.az_calls("what-if"), [])

    def test_wrong_subscription_or_not_signed_in(self):
        rid = self.published()
        code, out = self.plan(rid, sub=OTHER_SUB)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("wrong_context", out["message"])
        self.assertIn("does not switch context", out["message"])
        self.set_az(fail={"account": "ERROR: Please run 'az login' to setup account."})
        code, out = self.plan(rid)
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertTrue(out["message"].startswith("not_signed_in"), out["message"])
        self.assertIsNone(self.rec(rid)["plan"])

    def test_azure_rejections_are_classified_with_a_way_forward(self):
        rid = self.published()
        cases = {
            "ERROR: (RequestDisallowedByPolicy) Resource was disallowed by policy.": ("policy", "not bypassed"),
            "ERROR: (AuthorizationFailed) The client does not have authorization to perform action": ("authorization", "will not do it"),
            "ERROR: (InvalidTemplateDeployment) ... QuotaExceeded: Operation could not be completed as it results in exceeding approved Total Regional Cores quota": ("quota", "quota"),
            "ERROR: (SkuNotAvailable) The requested VM size is currently not available in location": ("sku_unavailable", "another size"),
            "ERROR: (InvalidTemplate) Deployment template validation failed": ("invalid_template", "IMPLEMENTATION"),
        }
        for stderr, (kind, hint) in cases.items():
            self.set_az(fail={"validate": stderr})
            code, out = self.plan(rid)
            self.assertEqual(code, EXIT_EXTERNAL, stderr)
            self.assertTrue(out["message"].startswith(kind + ":"), out["message"])
            self.assertIn(hint, out["message"])
        self.assertIsNone(self.rec(rid)["plan"])
        self.assertEqual(self.az_calls("create"), [])

    def test_transient_read_failure_is_retried_a_bounded_number_of_times(self):
        calls = []

        def flaky(args, timeout):
            calls.append(args)
            return (1, "", "ERROR: (TooManyRequests) 429") if len(calls) < 3 else (0, '{"ok": true}', "")
        self.assertEqual(Az(run=flaky, sleep=lambda s: None).read(["account", "show"]), {"ok": True})
        self.assertEqual(len(calls), 3)
        calls.clear()

        def always(args, timeout):
            calls.append(args)
            return 1, "", "ERROR: ServiceUnavailable 503"
        with self.assertRaises(AzFailure) as cm:
            Az(run=always, sleep=lambda s: None).read(["account", "show"])
        self.assertEqual((cm.exception.kind, len(calls)), ("transient", 3))
        calls.clear()
        with self.assertRaises(AzFailure):
            Az(run=always, sleep=lambda s: None).change(["deployment", "sub", "create"])
        self.assertEqual(len(calls), 1)                      # a change is never retried

        def denied(args, timeout):
            calls.append(args)
            return 1, "", "ERROR: (AuthorizationFailed) no"
        calls.clear()
        with self.assertRaises(AzFailure):
            Az(run=denied, sleep=lambda s: None).read(["x"])
        self.assertEqual(len(calls), 1)                      # only transient failures are retried

    def test_missing_parameter_file(self):
        rid = self.published()
        code, out = self.plan(rid, environment="test")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("infra/parameters/test.bicepparam", out["message"])

    def test_every_classification_has_advice(self):
        for kind in ("az_missing", "not_signed_in", "authorization", "policy", "quota",
                     "sku_unavailable", "invalid_template", "conflict", "transient", "error"):
            self.assertTrue(ADVICE[kind])
        self.assertEqual(classify("ERROR: AADSTS700082: The refresh token has expired"), "not_signed_in")
        self.assertEqual(classify("(Conflict) AnotherOperationInProgress"), "conflict")
        self.assertEqual(classify("something odd"), "error")


class RiskFlags(DeployCase):
    def flags(self, changes):
        return whatif.parse({"changes": changes})["risk_flags"]

    def test_flags_from_what_if(self):
        nsg = RG + "/providers/Microsoft.Network/networkSecurityGroups/nsg"
        role_rg = RG + "/providers/Microsoft.Authorization/roleAssignments/11111111-1111-1111-1111-111111111111"
        role_res = ST + "/providers/Microsoft.Authorization/roleAssignments/11111111-1111-1111-1111-111111111111"
        self.assertEqual(self.flags([change("Create", ST)]), [])
        self.assertEqual(self.flags([change("Delete", RG + "/providers/Microsoft.Network/virtualNetworks/v")]), ["deletion"])
        self.assertEqual(self.flags([change("Delete", ST)]), ["data_loss", "deletion"])
        self.assertEqual(self.flags([change("Modify", ST, delta=[{"path": "sku.name"}])]), ["stateful_replacement"])
        self.assertEqual(self.flags([change("Modify", ST, delta=[{"path": "tags.owner"}])]), [])
        self.assertEqual(self.flags([change("Create", RG + "/providers/Microsoft.Network/publicIPAddresses/pip")]), ["public_exposure"])
        self.assertEqual(self.flags([change("Create", ST, {"properties": {"publicNetworkAccess": "Enabled"}})]), ["public_exposure"])
        open_rule = {"properties": {"securityRules": [{"properties": {"direction": "Inbound", "access": "Allow", "sourceAddressPrefix": "*"}}]}}
        closed = {"properties": {"securityRules": [{"properties": {"direction": "Inbound", "access": "Allow", "sourceAddressPrefix": "10.0.0.0/8"}}]}}
        self.assertEqual(self.flags([change("Create", nsg, open_rule)]), ["public_exposure"])
        self.assertEqual(self.flags([change("Create", nsg, closed)]), [])
        self.assertEqual(self.flags([change("Create", role_rg, {"properties": {"roleDefinitionId": "/x/reader"}})]), ["broad_rbac"])
        owner = {"properties": {"roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"}}
        self.assertEqual(self.flags([change("Create", role_res, owner)]), ["broad_rbac"])
        self.assertEqual(self.flags([change("Create", role_res, {"properties": {"roleDefinitionId": "/x/blob-reader"}})]), [])

    def test_unevaluated_changes_are_uncertain_not_no_change(self):
        out = whatif.parse({"changes": [change("Unsupported", ST, reason="nested deployment could not be evaluated"),
                                        change("Ignore", RG + "/providers/Microsoft.Network/virtualNetworks/v"),
                                        change("NoChange", RG)],
                            "potentialChanges": [change("Modify", ST)]})
        self.assertEqual(len(out["uncertain"]), 3)
        self.assertIn("could not be evaluated", whatif.summarise(out))
        self.assertEqual([c["change"] for c in out["change_set"]], ["NoChange", "Unsupported"])

    def test_deletion_needs_the_high_risk_phrase_before_deployment(self):
        self.set_az(whatif={"status": "Succeeded", "changes": [change("Create", RG), change("Delete", ST)]})
        rid = self.published()
        code, out = self.plan(rid)
        self.assertEqual(out["risk_flags"], ["data_loss", "deletion"])
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT_APPROVAL")
        p = out["summary"]["pending_approval"]
        self.cli(STATE_CLI, "approve", rid, "--kind", "deployment", "--confirm", p["hash"])
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("high-risk", out["message"])
        code, out = self.cli(STATE_CLI, "confirm-risk", rid, "--phrase", "yes go ahead")
        self.assertEqual(code, EXIT_REFUSED)
        self.cli(STATE_CLI, "confirm-risk", rid, "--phrase", p["high_risk_phrase"])
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")
        self.assertEqual(code, 0, out)

    def test_production_environment_is_flagged(self):
        ConfigStore(self.project).set_value("environments", "dev,prod")
        rid = self.published()
        self.write_file_in_remote = None
        ws = self.ws()
        # A prod parameter file must exist in the published tree for prod to be planned.
        code, out = self.plan(rid, environment="prod")
        self.assertEqual(code, EXIT_REFUSED)       # no prod parameter file was published
        self.assertIsNone(self.rec(rid)["plan"])
        self.assertTrue(ws.exists())


class Deploy(DeployCase):
    def deploy(self, rid, env=None):
        return self.run_cli(DEPLOY_CLI, "deploy", rid, env=env)

    def test_no_deployment_without_approval(self):
        rid = self.published()
        self.plan(rid)
        for state in ("GIT_REVIEW", "DEPLOYMENT_APPROVAL"):
            code, out = self.deploy(rid)
            self.assertEqual(code, EXIT_REFUSED, state)
            self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT_APPROVAL")
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(self.az_calls("create"), [])

    def test_a_forged_state_still_cannot_deploy(self):
        rid = self.published()
        self.plan(rid)
        StateStore(self.project).mutate(rid, lambda r, e: r.update(state="DEPLOYMENT"))
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("no valid deployment approval", out["message"])
        self.assertEqual(self.az_calls("create"), [])

    def test_forged_state_with_risk_flags_and_no_confirmation_cannot_deploy(self):
        self.set_az(whatif={"status": "Succeeded", "changes": [change("Create", RG), change("Delete", ST)]})
        rid = self.published()
        self.plan(rid)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT_APPROVAL")
        self.cli(STATE_CLI, "approve", rid, "--kind", "deployment",
                 "--confirm", out["summary"]["pending_approval"]["hash"])
        StateStore(self.project).mutate(rid, lambda r, e: r.update(state="DEPLOYMENT"))
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("high-risk", out["message"])
        self.assertEqual(self.az_calls("create"), [])

    def test_approved_deployment_runs_once_and_reports_what_azure_said(self):
        rid = self.approved()
        self.set_az(outputs={"storageId": {"type": "String", "value": "stlogsdev"},
                             "connection": {"type": "SecureString"}})
        code, out = self.deploy(rid)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(len(self.az_calls("create")), 1)
        create = self.az_calls("create")[0]
        self.assertIn("--template-file", create)
        self.assertNotIn("Complete", create)
        self.assertEqual(out["independently_verified"], [])          # nothing claimed beyond Azure's report
        self.assertEqual(len(out["reported_by_azure"]), 2)
        self.assertEqual(out["deployment"]["outputs"], {"storageId": "stlogsdev"})   # secure outputs dropped
        rec = self.rec(rid)
        self.assertEqual(rec["deployment"]["status"], "succeeded")
        self.assertEqual(rec["deployment"]["info"]["name"], "iac-" + rid)
        self.assertEqual(rec["step"]["status"], "succeeded")
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)                         # not deployed twice
        self.assertEqual(len(self.az_calls("create")), 1)

    def test_drift_between_approval_and_deployment_stops_it(self):
        rid = self.approved()
        vnet = RG + "/providers/Microsoft.Network/virtualNetworks/extra"
        self.set_az(whatif={"status": "Succeeded", "changes": CREATE_STORAGE["changes"] + [change("Delete", vnet)]})
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("differs from the approved change set", out["message"])
        self.assertEqual(self.az_calls("create"), [])
        self.assertIsNone(self.rec(rid)["deployment"])

    def test_replanning_with_a_different_result_needs_a_new_approval(self):
        rid = self.approved()
        self.set_az(whatif={"status": "Succeeded", "changes": CREATE_STORAGE["changes"][:1]})
        code, out = self.cli(STATE_CLI, "back", rid, "--to", "DEPLOYMENT_APPROVAL", "--reason", "replan")
        code, out = self.plan(rid)
        self.assertEqual(code, 0, out)
        rec = self.rec(rid)
        self.assertFalse(rec["approvals"]["deployment"]["valid"])
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")
        self.assertEqual(code, EXIT_REFUSED)

    def test_changed_inputs_stop_it(self):
        rid = self.approved()
        code, out = self.deploy(rid, env={"IAC_TEST_INPUT": "a-different-key"})
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("differ from what was approved", out["message"])
        self.assertEqual(self.az_calls("create"), [])

    def test_split_id(self):
        role = ST + "/providers/Microsoft.Authorization/roleAssignments/abc"
        self.assertEqual(whatif.split_id(role), ("rg-logs-dev", "Microsoft.Authorization/roleAssignments", "abc"))
        self.assertEqual(whatif.split_id(RG), ("rg-logs-dev", "Microsoft.Resources/resourceGroups", "rg-logs-dev"))
        sub = RG + "/providers/Microsoft.Network/virtualNetworks/v/subnets/s"
        self.assertEqual(whatif.split_id(sub), ("rg-logs-dev", "Microsoft.Network/virtualNetworks/subnets", "v/s"))

    def test_changed_files_context_or_rules_stop_it(self):
        rid = self.approved()
        self.write("infra/modules/storage.bicep", STORAGE + "// edited after approval\n")
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.write("infra/modules/storage.bicep", STORAGE + "// change\n")
        self.set_az(account=dict(self.az()["account"], id=OTHER_SUB))
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("wrong_context", out["message"])
        self.set_az(account=dict(self.az()["account"], id=SUB))
        os.unlink(os.path.join(self.project, ".claude", "settings.json"))
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("permission rules", out["message"])
        self.assertEqual(self.az_calls("create"), [])

    def test_partial_failure_is_recorded_as_partial_and_never_retried(self):
        rid = self.approved()
        self.set_az(fail={"create": "ERROR: (DeploymentFailed) At least one resource deployment operation failed. (QuotaExceeded) cores"},
                    ops=[{"properties": {"provisioningState": "Succeeded", "targetResource": {"id": RG, "resourceType": "Microsoft.Resources/resourceGroups"}}},
                         {"properties": {"provisioningState": "Failed", "targetResource": {"id": ST, "resourceType": "Microsoft.Storage/storageAccounts"},
                                         "statusMessage": {"error": {"message": "quota exceeded"}}}}])
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertTrue(out["message"].startswith("quota"), out["message"])
        self.assertIn("/resourceGroups/rg-logs-dev", out["message"])
        self.assertIn("Nothing was rolled back or deleted", out["message"])
        self.assertEqual(len(self.az_calls("create")), 1)
        rec = self.rec(rid)
        self.assertEqual(rec["deployment"]["status"], "partial")
        self.assertEqual(rec["step"]["status"], "failed")
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "VERIFICATION")
        self.assertEqual(code, EXIT_REFUSED)                         # a failed deployment is not verified as done
        calls = self.az().get("calls", [])
        self.assertFalse([c for c in calls if "delete" in c])        # nothing deleted to recover

    def test_total_failure_is_recorded_as_failed(self):
        rid = self.approved()
        self.set_az(fail={"create": "ERROR: (RequestDisallowedByPolicy) disallowed by policy"}, ops=[])
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_EXTERNAL)
        self.assertTrue(out["message"].startswith("policy"))
        self.assertEqual(self.rec(rid)["deployment"]["status"], "failed")

    def test_interrupted_deployment_is_resolved_from_azure_not_rerun(self):
        rid = self.approved()
        store = StateStore(self.project)
        store.mutate(rid, lambda r, e: M.step_start(r, "deploy"))      # the process died here
        code, out = self.deploy(rid)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("outcome is unknown", out["message"])
        self.assertEqual(self.az_calls("create"), [])
        code, out = self.run_cli(DEPLOY_CLI, "status", rid)             # read-only look
        self.assertFalse(out["exists_in_azure"])
        self.assertEqual(self.rec(rid)["step"]["status"], "in_progress")
        code, out = self.run_cli(DEPLOY_CLI, "status", rid, "--record")
        self.assertIn("safe to deploy", out["resolved"])
        code, out = self.deploy(rid)                                    # now a real attempt
        self.assertEqual(code, 0, out)

    def test_interrupted_deployment_that_succeeded_is_recorded_from_azure(self):
        rid = self.approved()
        StateStore(self.project).mutate(rid, lambda r, e: M.step_start(r, "deploy"))
        self.set_az(exists=True, provisioning="Succeeded")
        code, out = self.run_cli(DEPLOY_CLI, "status", rid, "--record")
        self.assertIn("succeeded", out["resolved"])
        rec = self.rec(rid)
        self.assertEqual(rec["deployment"]["status"], "succeeded")
        self.assertEqual(self.az_calls("create"), [])
        self.set_az(exists=True, provisioning="Running")
        StateStore(self.project).mutate(rid, lambda r, e: M.step_start(r, "deploy"))
        code, out = self.run_cli(DEPLOY_CLI, "status", rid, "--record")
        self.assertFalse(out["resolved"])
        self.assertIn("still running", out["note"])


class Verify(DeployCase):
    def deployed(self):
        rid = self.approved()
        code, out = self.run_cli(DEPLOY_CLI, "deploy", rid)
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "VERIFICATION")
        self.assertEqual(code, 0, out)
        return rid

    def test_verified_from_the_resources_themselves(self):
        rid = self.deployed()
        code, out = self.run_cli(DEPLOY_CLI, "verify", rid)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["verdict"], "passed")
        self.assertEqual(len(out["planned"]), 2)
        self.assertEqual(len(out["independently_verified"]), 2)
        self.assertEqual(out["problems"], [])
        code, out = self.cli(STATE_CLI, "complete", rid)
        self.assertEqual(out["record"]["status"], "completed")

    def test_missing_resource_fails_verification_whatever_the_deployment_says(self):
        rid = self.deployed()
        self.set_az(missing=[ST])
        code, out = self.run_cli(DEPLOY_CLI, "verify", rid)
        by = {c["check"]: c["result"] for c in out["checks"]}
        self.assertEqual(by["deployment-state"], "passed")
        self.assertEqual(by["resources-exist"], "failed")
        self.assertEqual(out["verdict"], "failed")
        code, out = self.cli(STATE_CLI, "complete", rid)
        self.assertEqual(code, EXIT_REFUSED)

    def test_unplanned_resources_and_stopped_vm(self):
        vm = RG + "/providers/Microsoft.Compute/virtualMachines/vm1"
        self.set_az(whatif={"status": "Succeeded", "changes": [change("Create", RG), change("Create", vm)]})
        rid = self.deployed()
        self.set_az(power="PowerState/deallocated",
                    rg_resources=[{"id": vm, "type": "Microsoft.Compute/virtualMachines"},
                                  {"id": RG + "/providers/Microsoft.Compute/disks/osdisk", "type": "Microsoft.Compute/disks"},
                                  {"id": RG + "/providers/Microsoft.Network/publicIPAddresses/surprise", "type": "Microsoft.Network/publicIPAddresses"}])
        code, out = self.run_cli(DEPLOY_CLI, "verify", rid)
        by = {c["check"]: c for c in out["checks"]}
        self.assertEqual(by["vm-power-state"]["result"], "failed")
        self.assertEqual(by["unplanned-resources"]["result"], "warning")
        self.assertIn("surprise", by["unplanned-resources"]["detail"])
        self.assertNotIn("osdisk", by["unplanned-resources"]["detail"])
        self.set_az(power="PowerState/running", rg_resources=[{"id": RG + "/providers/Microsoft.Compute/disks/osdisk", "type": "Microsoft.Compute/disks"}])
        code, out = self.run_cli(DEPLOY_CLI, "verify", rid)
        self.assertEqual(out["verdict"], "passed")


class EndToEnd(DeployCase):
    """The whole workflow through the CLIs with mocked GitHub and Azure."""

    def test_request_to_completion(self):
        c = lambda script, *a: self.run_cli(script, *a)
        ok = lambda r: (self.assertEqual(r[0], 0, r[1]), r[1])[1]
        rid = ok(c(STATE_CLI, "create", "--intent", "Storage account for application logs"))["record"]["id"]
        ok(c(STATE_CLI, "advance", rid, "--to", "DISCOVERY"))
        ok(c(STATE_CLI, "set-profile", rid, "--json", json.dumps(PROFILE)))
        for _ in range(6):
            nxt = ok(c(DISCOVERY_CLI, "next", rid))
            if not nxt["ask"]:
                break
            for q in nxt["ask"]:
                ok(c(STATE_CLI, "add-requirement", rid, "--text", "answer for %s" % q["topic"], "--topic", q["topic"]))
        for s in ok(c(DISCOVERY_CLI, "next", rid))["suggested_assumptions"]:
            ok(c(STATE_CLI, "add-assumption", rid, "--text", s["assumption"], "--topic", s["topic"]))
        nxt = ok(c(DISCOVERY_CLI, "next", rid))
        ok(c(STATE_CLI, "review-assumptions", rid, "--confirm", nxt["assumptions_hash"]))
        self.assertTrue(ok(c(DISCOVERY_CLI, "next", rid))["ready_for_architecture"])
        ok(c(STATE_CLI, "advance", rid, "--to", "ARCHITECTURE"))
        ok(c(STATE_CLI, "set-architecture", rid, "--json", json.dumps(ARCH)))
        ok(c(STATE_CLI, "advance", rid, "--to", "APPROVAL"))
        h = ok(c(DISCOVERY_CLI, "proposal", rid))["approval_hash"]
        self.assertEqual(c(STATE_CLI, "advance", rid, "--to", "IMPLEMENTATION")[0], EXIT_REFUSED)
        ok(c(STATE_CLI, "approve", rid, "--kind", "architecture", "--confirm", h))
        ok(c(STATE_CLI, "advance", rid, "--to", "IMPLEMENTATION"))
        ok(c(WORKSPACE_CLI, "clone"))
        ok(c(WORKSPACE_CLI, "begin", rid))
        self.write("infra/modules/storage.bicep", STORAGE + "// retention\n")
        ok(c(WORKSPACE_CLI, "record-files", rid))
        ok(c(STATE_CLI, "advance", rid, "--to", "VALIDATION"))
        self.assertEqual(ok(c(VALIDATE_CLI, "run", rid))["verdict"], "passed")
        ok(c(STATE_CLI, "advance", rid, "--to", "GIT_REVIEW"))
        pub = ok(c(GITHUB_CLI, "publish", rid, "--message", "Add log storage retention"))
        self.assertEqual(self.remote_branches()["iac/" + rid], pub["commit"])
        plan = ok(self.plan(rid))
        self.assertEqual(plan["counts"], {"Create": 2})
        pending = ok(c(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT_APPROVAL"))["summary"]["pending_approval"]
        self.assertEqual(pending["content"]["git_commit"], pub["commit"])
        self.assertEqual(c(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT")[0], EXIT_REFUSED)
        ok(c(STATE_CLI, "approve", rid, "--kind", "deployment", "--confirm", pending["hash"]))
        ok(c(STATE_CLI, "advance", rid, "--to", "DEPLOYMENT"))
        dep = ok(c(DEPLOY_CLI, "deploy", rid))
        self.assertEqual(dep["status"], "succeeded")
        ok(c(STATE_CLI, "advance", rid, "--to", "VERIFICATION"))
        self.assertEqual(ok(c(DEPLOY_CLI, "verify", rid))["verdict"], "passed")
        done = ok(c(STATE_CLI, "complete", rid))
        self.assertEqual(done["record"]["status"], "completed")
        events = [e["event"] for e in done["record"]["history"]]
        for e in ("approved", "validation_run", "git_set", "plan_set", "deployment_recorded", "completed"):
            self.assertIn(e, events)
        self.assertEqual(len(self.az_calls("create")), 1)
        raw = json.dumps(done["record"])
        self.assertNotIn("ghp_", raw)
