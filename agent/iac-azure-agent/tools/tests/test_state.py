"""Workflow state machine: transitions, approval gates, invalidation, high risk, resume."""
import json
import os

from helpers import STATE_CLI, SUB, TENANT, StoreCase

from config.store import ConfigStore
from lib.canonical import content_hash, short
from lib.errors import (EXIT_CORRUPT, EXIT_REFUSED, CorruptRecord, InvalidInput, Refused,
                        StorageUnavailable)
from state import machine as M
from state.store import StateStore

ARCH = {"resources": [{"type": "Microsoft.Storage/storageAccounts", "name": "st-logs"}],
        "network": "private endpoints"}
PLAN = {"change_set": [{"resource": "st-logs", "change": "Create"}], "risk_flags": []}
COMMIT = "0123456789abcdef0123456789abcdef01234567"


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore(self.project)
        self.id = self.store.create("Storage account for application logs")["id"]

    def do(self, fn, *args, **kw):
        return self.store.mutate(self.id, lambda r, e: fn(r, *args, **kw))[0]

    def do_env(self, fn, *args):
        """For machine functions that take production_envs as their last argument."""
        return self.store.mutate(self.id, lambda r, e: fn(r, *(args + (e,))))[0]

    def rec(self):
        return self.store.load(self.id)

    def pending_hash(self):
        r = self.rec()
        return M.pending_approval(r, self.store.production_envs())["hash"]

    def drive(self, target, plan=PLAN, environment="dev"):
        """Satisfies each guard and advances until the record is in `target`."""
        while self.rec()["state"] != target:
            s = self.rec()["state"]
            if s == "DISCOVERY":
                self.do(M.add_requirement, "Store application logs for 90 days")
            elif s == "ARCHITECTURE":
                self.do(M.set_architecture, ARCH)
            elif s == "APPROVAL":
                self.do_env(M.approve, "architecture", self.pending_hash())
            elif s == "IMPLEMENTATION":
                self.do(M.add_file, "infra/main.bicep", "created")
            elif s == "VALIDATION":
                self.do(M.add_validation, "bicep build", "passed")
            elif s == "GIT_REVIEW":
                self.do(M.set_git, "iac/logs", COMMIT, "https://github.com/example-org/infra/pull/1")
                self.do(M.set_target, TENANT, SUB, "rg-logs-%s" % environment, environment)
                self.do(M.set_plan, plan)
            elif s == "DEPLOYMENT_APPROVAL":
                p = M.pending_approval(self.rec(), self.store.production_envs())
                self.do_env(M.approve, "deployment", p["hash"])
                if p.get("high_risk_phrase"):
                    self.do_env(M.confirm_high_risk, p["high_risk_phrase"])
            elif s == "DEPLOYMENT":
                self.do(M.set_deployment, "succeeded")
            nxt = M.STATES[M.IDX[s] + 1]
            self.do_env(M.advance, nxt, False)
        return self.rec()


class Transitions(Base):
    def test_full_forward_path_and_complete(self):
        seen = [self.rec()["state"]]
        for target in M.STATES[1:]:
            self.drive(target)
            seen.append(self.rec()["state"])
        self.assertEqual(tuple(seen), M.STATES)
        self.do(M.add_verification, "resource exists", "passed")
        rec = self.do(M.complete)
        self.assertEqual(rec["status"], "completed")
        with self.assertRaises(Refused):
            self.do_env(M.back, "DISCOVERY", "late change")

    def test_every_legal_backward_move(self):
        for i in range(M.IDX["ARCHITECTURE"], len(M.STATES)):
            src = M.STATES[i]
            for dst in M.STATES[M.IDX["DISCOVERY"]:i]:
                self.id = self.store.create("back %s -> %s" % (src, dst))["id"]
                self.drive(src)
                rec = self.do_env(M.back, dst, "rework")
                self.assertEqual(rec["state"], dst, (src, dst))
                self.assertEqual(rec["history"][-1]["event"], "moved_back")
                self.assertTrue(rec["requirements"], "context must be kept on back moves")

    def test_illegal_moves(self):
        cases = [("advance", "ARCHITECTURE"), ("advance", "REQUEST"), ("advance", "VERIFICATION"),
                 ("back", "DISCOVERY")]
        for kind, to in cases:
            with self.assertRaises(Refused, msg=(kind, to)):
                if kind == "advance":
                    self.do_env(M.advance, to, False)
                else:
                    self.do_env(M.back, to, "x")
        self.drive("ARCHITECTURE")
        for to in ("ARCHITECTURE", "IMPLEMENTATION", "DISCOVERY"):
            with self.assertRaises(Refused, msg=to):
                self.do_env(M.advance, to, False)
        with self.assertRaises(Refused):
            self.do_env(M.back, "REQUEST", "x")
        with self.assertRaises(Refused):
            self.do_env(M.back, "APPROVAL", "x")
        with self.assertRaises(InvalidInput):
            self.do_env(M.advance, "DONE", False)
        self.drive("VERIFICATION")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "VERIFICATION", False)

    def test_guards_on_forward_moves(self):
        self.drive("DISCOVERY")
        with self.assertRaises(Refused):  # no requirements
            self.do_env(M.advance, "ARCHITECTURE", False)
        self.do(M.add_question, "Which region?")
        self.do(M.add_requirement, "Logs kept 90 days")
        with self.assertRaises(Refused):  # open question
            self.do_env(M.advance, "ARCHITECTURE", False)
        self.do(M.defer_question, "Q1", "Region is westeurope")
        self.do_env(M.advance, "ARCHITECTURE", False)
        with self.assertRaises(Refused):  # no architecture
            self.do_env(M.advance, "APPROVAL", False)

    def test_cancel_is_terminal(self):
        rec = self.do(M.cancel, "not needed")
        self.assertEqual(rec["status"], "cancelled")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "DISCOVERY", False)
        with self.assertRaises(Refused):
            self.do(M.step_start, "x")


class Approvals(Base):
    def test_approval_required_before_implementation(self):
        self.drive("APPROVAL")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "IMPLEMENTATION", False)

    def test_wrong_hash_or_wrong_state_refused(self):
        self.drive("ARCHITECTURE")
        self.do(M.set_architecture, ARCH)
        with self.assertRaises(Refused):  # not in APPROVAL yet
            self.do_env(M.approve, "architecture", short(content_hash(M.architecture_content(self.rec()))))
        self.do_env(M.advance, "APPROVAL", False)
        for bad in ("", "0" * 12, content_hash(ARCH)[:12], None):
            with self.assertRaises(Refused, msg=bad):
                self.do_env(M.approve, "architecture", bad)
        with self.assertRaises(Refused):
            self.do_env(M.approve, "deployment", self.pending_hash())

    def test_hash_is_computed_by_tool_over_canonical_json(self):
        self.drive("APPROVAL")
        h = self.pending_hash()
        reordered = json.loads(json.dumps(M.architecture_content(self.rec())), object_pairs_hook=lambda kv: dict(reversed(kv)))
        self.assertEqual(short(content_hash(reordered)), h)
        rec = self.do_env(M.approve, "architecture", h.upper())
        self.assertEqual(len(rec["approvals"]["architecture"]["hash"]), 64)

    def test_architecture_change_invalidates_and_returns_to_approval(self):
        self.drive("VALIDATION")
        rec = self.do(M.set_architecture, dict(ARCH, network="public"))
        self.assertEqual(rec["state"], "APPROVAL")
        self.assertFalse(rec["approvals"]["architecture"]["valid"])
        self.assertIn("returned_to_approval", [h["event"] for h in rec["history"]])
        with self.assertRaises(Refused):
            self.do_env(M.advance, "IMPLEMENTATION", False)
        self.do_env(M.approve, "architecture", self.pending_hash())
        self.do_env(M.advance, "IMPLEMENTATION", False)

    def test_new_requirement_after_approval_invalidates(self):
        self.drive("IMPLEMENTATION")
        rec = self.do(M.add_requirement, "Must be zone redundant")
        self.assertEqual(rec["state"], "APPROVAL")

    def test_unchanged_content_keeps_approval_across_back_moves(self):
        self.drive("IMPLEMENTATION")
        self.do_env(M.back, "ARCHITECTURE", "review proposal again")
        self.do(M.set_architecture, ARCH)  # same content
        self.do_env(M.advance, "APPROVAL", False)
        rec = self.do_env(M.advance, "IMPLEMENTATION", False)
        self.assertEqual(rec["state"], "IMPLEMENTATION")

    def test_tampered_record_on_disk_invalidates(self):
        self.drive("IMPLEMENTATION")
        path = self.store.path(self.id)
        with open(path) as f:
            data = json.load(f)
        data["architecture"]["network"] = "public"
        with open(path, "w") as f:
            json.dump(data, f)
        rec = self.do(M.add_file, "infra/main.bicep", "created")
        self.assertEqual(rec["state"], "APPROVAL")

    def test_deployment_approval_required(self):
        self.drive("DEPLOYMENT_APPROVAL")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "DEPLOYMENT", False)
        self.do_env(M.approve, "deployment", self.pending_hash())
        self.assertEqual(self.do_env(M.advance, "DEPLOYMENT", False)["state"], "DEPLOYMENT")

    def test_plan_or_target_change_invalidates_deployment_approval(self):
        self.drive("DEPLOYMENT")
        rec = self.do(M.set_plan, {"change_set": [{"resource": "st-logs", "change": "Modify"}]})
        self.assertEqual(rec["state"], "DEPLOYMENT_APPROVAL")
        self.assertFalse(rec["approvals"]["deployment"]["valid"])
        self.do_env(M.approve, "deployment", self.pending_hash())
        self.do_env(M.advance, "DEPLOYMENT", False)
        rec = self.do(M.set_target, TENANT, "00000000-0000-0000-0000-00000000000c", "rg-logs-dev", "dev")
        self.assertEqual(rec["state"], "DEPLOYMENT_APPROVAL")

    def test_architecture_change_also_invalidates_deployment_approval(self):
        self.drive("DEPLOYMENT")
        rec = self.do(M.set_architecture, dict(ARCH, sku="Standard_ZRS"))
        self.assertEqual(rec["state"], "APPROVAL")
        self.assertFalse(rec["approvals"]["deployment"]["valid"])

    def test_deployment_inputs_required_for_approval(self):
        self.drive("GIT_REVIEW")
        self.do(M.set_git, "iac/logs", COMMIT)
        self.do_env(M.advance, "DEPLOYMENT_APPROVAL", False)
        with self.assertRaises(Refused):
            self.do_env(M.approve, "deployment", "whatever")


class HighRisk(Base):
    def test_risk_flag_needs_distinct_confirmation(self):
        plan = dict(PLAN, risk_flags=["deletion"])
        self.drive("GIT_REVIEW")
        self.do(M.set_git, "iac/logs", COMMIT)
        self.do(M.set_target, TENANT, SUB, "rg-logs-dev", "dev")
        self.do(M.set_plan, plan)
        self.do_env(M.advance, "DEPLOYMENT_APPROVAL", False)
        p = M.pending_approval(self.rec(), self.store.production_envs())
        self.assertEqual(p["high_risk_flags"], ["deletion"])
        self.do_env(M.approve, "deployment", p["hash"])
        with self.assertRaises(Refused):
            self.do_env(M.advance, "DEPLOYMENT", False)
        for bad in (p["hash"], "yes", "ACCEPT-RISK %s" % p["hash"],
                    "ACCEPT-RISK 000000000000 deletion"):
            with self.assertRaises(Refused, msg=bad):
                self.do_env(M.confirm_high_risk, bad)
        self.do_env(M.confirm_high_risk, p["high_risk_phrase"])
        self.assertEqual(self.do_env(M.advance, "DEPLOYMENT", False)["state"], "DEPLOYMENT")

    def test_no_flags_nothing_to_confirm(self):
        self.drive("DEPLOYMENT_APPROVAL")
        with self.assertRaises(Refused):
            self.do_env(M.confirm_high_risk, "ACCEPT-RISK x")

    def test_production_target_flag_is_automatic(self):
        self.drive("DEPLOYMENT_APPROVAL", environment="prod")
        p = M.pending_approval(self.rec(), self.store.production_envs())
        self.assertIn("production_target", p["high_risk_flags"])

    def test_configured_production_environment_is_flagged(self):
        cfg = ConfigStore(self.project)
        cfg.set_value("environments", "dev,live")
        cfg.set_value("production_environments", "live")
        self.drive("DEPLOYMENT_APPROVAL", environment="live")
        p = M.pending_approval(self.rec(), self.store.production_envs())
        self.assertEqual(p["high_risk_flags"], ["production_target"])

    def test_new_flag_after_confirmation_invalidates(self):
        self.drive("DEPLOYMENT", plan=dict(PLAN, risk_flags=["public_exposure"]))
        rec = self.do(M.set_plan, dict(PLAN, risk_flags=["public_exposure", "broad_rbac"]))
        self.assertEqual(rec["state"], "DEPLOYMENT_APPROVAL")
        self.assertFalse(rec["approvals"]["high_risk"]["valid"])

    def test_unknown_flag_rejected(self):
        self.drive("GIT_REVIEW")
        with self.assertRaises(InvalidInput):
            self.do(M.set_plan, dict(PLAN, risk_flags=["minor"]))


class ChecksAndSteps(Base):
    def test_skipped_and_unavailable_are_not_passes(self):
        self.drive("VALIDATION")
        self.do(M.add_validation, "bicep build", "passed")
        self.do(M.add_validation, "psrule", "unavailable")
        self.do(M.add_validation, "bicep lint", "skipped")
        s = M.summarise_checks(self.rec()["validation"])
        self.assertEqual(s["verdict"], "incomplete")
        self.assertEqual(s["counts"]["passed"], 1)
        with self.assertRaises(Refused):
            self.do_env(M.advance, "GIT_REVIEW", False)
        rec = self.do_env(M.advance, "GIT_REVIEW", True)
        self.assertEqual(rec["accepted_incomplete"][-1]["checks"], ["bicep lint", "psrule"])

    def test_failed_validation_blocks_and_back_marks_stale(self):
        self.drive("VALIDATION")
        self.do(M.add_validation, "bicep build", "failed", "BCP035 missing property")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "GIT_REVIEW", True)
        rec = self.do_env(M.back, "IMPLEMENTATION", "fix build error")
        self.assertTrue(all(c["stale"] for c in rec["validation"]))
        self.do_env(M.advance, "VALIDATION", False)
        self.assertEqual(M.summarise_checks(self.rec()["validation"])["verdict"], "none")

    def test_failed_deployment_cannot_be_verified(self):
        self.drive("DEPLOYMENT")
        self.do(M.set_deployment, "failed", "conflict")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "VERIFICATION", False)
        rec = self.do_env(M.back, "IMPLEMENTATION", "deployment failed")
        self.assertTrue(rec["deployment"]["stale"])

    def test_failed_step_blocks_until_retried(self):
        self.drive("IMPLEMENTATION")
        self.do(M.add_file, "infra/main.bicep", "created")
        self.do(M.step_start, "generate")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "VALIDATION", False)
        self.do(M.step_end, "failed", "template error")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "VALIDATION", False)
        self.do(M.step_start, "generate")
        rec = self.do(M.step_end, "succeeded")
        self.assertEqual(rec["step"]["attempt"], 2)
        self.do_env(M.advance, "VALIDATION", False)

    def test_completion_requires_passed_verification(self):
        self.drive("VERIFICATION")
        with self.assertRaises(Refused):
            self.do(M.complete)
        self.do(M.add_verification, "endpoint reachable", "unavailable")
        with self.assertRaises(Refused):
            self.do(M.complete)

    def test_data_recorded_only_in_matching_state(self):
        with self.assertRaises(Refused):
            self.do(M.add_requirement, "x")  # REQUEST
        self.drive("DISCOVERY")
        for fn, args in ((M.set_architecture, (ARCH,)), (M.add_file, ("a.bicep", "created")),
                         (M.add_validation, ("c", "passed")), (M.set_plan, (PLAN,)),
                         (M.set_deployment, ("succeeded",)), (M.add_verification, ("c", "passed"))):
            with self.assertRaises(Refused, msg=fn.__name__):
                self.do(fn, *args)

    def test_input_validation(self):
        self.drive("IMPLEMENTATION")
        for p in ("/etc/passwd", "../x.bicep", "C:/x.bicep"):
            with self.assertRaises(InvalidInput):
                self.do(M.add_file, p, "created")
        self.drive("GIT_REVIEW")
        with self.assertRaises(InvalidInput):
            self.do(M.set_target, "not-a-guid", SUB, "rg", "dev")
        with self.assertRaises(InvalidInput):
            self.do(M.set_git, commit="NOTHEX")


class Secrets(Base):
    def test_secrets_rejected_in_records(self):
        self.drive("DISCOVERY")
        with self.assertRaises(InvalidInput):
            self.do(M.add_requirement, "use key DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abc123==")
        self.drive("ARCHITECTURE")
        with self.assertRaises(InvalidInput):
            self.do(M.set_architecture, {"admin_password": "x"})
        self.assertNotIn("AccountKey", json.dumps(self.rec()))

    def test_guids_in_target_accepted(self):
        self.drive("GIT_REVIEW")
        rec = self.do(M.set_target, TENANT.upper(), SUB, "rg-x", "dev")
        self.assertEqual(rec["target"]["tenant_id"], TENANT)


class StorageAndCli(Base):
    def test_resume_after_interrupted_step_in_new_process(self):
        self.drive("IMPLEMENTATION")
        code, _ = self.cli(STATE_CLI, "add-file", self.id, "--path", "infra/main.bicep", "--action", "created")
        self.assertEqual(code, 0)
        code, _ = self.cli(STATE_CLI, "step-start", self.id, "--name", "generate modules")
        self.assertEqual(code, 0)
        # The process that started the step is gone. A new process resumes.
        code, out = self.cli(STATE_CLI, "resume", self.id)
        self.assertEqual(code, 0)
        self.assertTrue(out["interrupted_step_found"])
        self.assertEqual(out["summary"]["step"]["status"], "interrupted")
        self.assertEqual(out["summary"]["state"], "IMPLEMENTATION")
        self.assertTrue(out["summary"]["next_action"])
        code, _ = self.cli(STATE_CLI, "advance", self.id, "--to", "VALIDATION")
        self.assertEqual(code, EXIT_REFUSED)
        self.cli(STATE_CLI, "step-start", self.id, "--name", "generate modules")
        self.cli(STATE_CLI, "step-end", self.id, "--result", "succeeded")
        code, out = self.cli(STATE_CLI, "advance", self.id, "--to", "VALIDATION")
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "resume", self.id)
        self.assertFalse(out["interrupted_step_found"])

    def test_cli_approval_flow(self):
        code, out = self.cli(STATE_CLI, "create", "--intent", "Key Vault for app config")
        rid = out["record"]["id"]
        self.cli(STATE_CLI, "advance", rid, "--to", "DISCOVERY")
        self.cli(STATE_CLI, "add-requirement", rid, "--text", "RBAC authorization")
        self.cli(STATE_CLI, "advance", rid, "--to", "ARCHITECTURE")
        self.cli(STATE_CLI, "set-architecture", rid, "--json", json.dumps(ARCH))
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "APPROVAL")
        h = out["summary"]["pending_approval"]["hash"]
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "IMPLEMENTATION")
        self.assertEqual(code, EXIT_REFUSED)
        code, out = self.cli(STATE_CLI, "approve", rid, "--kind", "architecture", "--confirm", h)
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "advance", rid, "--to", "IMPLEMENTATION")
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "list")
        self.assertEqual(len(out["requests"]), 2)

    def test_corrupt_record_reported(self):
        with open(self.store.path(self.id), "w") as f:
            f.write("{oops")
        code, out = self.cli(STATE_CLI, "show", self.id)
        self.assertEqual(code, EXIT_CORRUPT)
        with self.assertRaises(CorruptRecord):
            self.do(M.cancel, "x")
        listing = self.store.list()
        self.assertIn("error", listing[0])
        with open(self.store.path(self.id)) as f:
            self.assertEqual(f.read(), "{oops")

    def test_concurrent_change_detected(self):
        def racing(rec, envs):
            other = StateStore(self.project)
            other.mutate(self.id, lambda r, e: M.add_question(r, "racing question") if r["state"] != "REQUEST" else M.advance(r, "DISCOVERY"))
            M.advance(rec, "DISCOVERY")
        with self.assertRaises(StorageUnavailable):
            self.store.mutate(self.id, racing)

    def test_unknown_id_and_path_traversal(self):
        for bad in ("../config", "req-1", "req-20260101-000000-zzzzzz"):
            code, out = self.cli(STATE_CLI, "show", bad)
            self.assertNotEqual(code, 0)

    def test_record_file_mode(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions only")
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path(self.id)).st_mode), 0o600)
