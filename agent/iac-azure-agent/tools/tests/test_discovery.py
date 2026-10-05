"""Discovery: profile, adaptive question batches, must-confirm topics, assumption review,
proposal completeness and rendering."""
import json

from helpers import ARCH, DISCOVERY_CLI, PROFILE, STATE_CLI, StoreCase

from config.store import ConfigStore
from discovery import catalog, planner, proposal
from lib.canonical import short
from lib.errors import EXIT_REFUSED, InvalidInput, Refused
from state import machine as M
from state.store import StateStore

LANDING_ZONE = {"kind": "new", "categories": ["networking", "security", "monitoring", "identity"],
                "environments": ["prod"], "integrates_existing": True,
                "restatement": "A production hub-and-spoke network with firewall and private DNS."}


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore(self.project)
        self.id = self.store.create("Storage account for application logs")["id"]
        self.do_env(M.advance, "DISCOVERY", False)

    def do(self, fn, *args):
        return self.store.mutate(self.id, lambda r, e: fn(r, *args))[0]

    def do_env(self, fn, *args):
        return self.store.mutate(self.id, lambda r, e: fn(r, *(args + (e,))))[0]

    def rec(self):
        return self.store.load(self.id)

    def plan(self):
        return planner.plan(self.rec(), ConfigStore(self.project).load(),
                            self.store.production_envs())

    def topics(self, plan=None):
        return [a["topic"] for a in (plan or self.plan())["ask"]]


class Profile(Base):
    def test_no_questions_before_profile(self):
        p = self.plan()
        self.assertEqual(p["round"], 1)
        self.assertEqual(p["ask"], [])
        self.assertIn("set-profile", p["next"])

    def test_profile_validation(self):
        for bad in ({}, dict(PROFILE, kind="destroy"), dict(PROFILE, categories=[]),
                    dict(PROFILE, categories=["quantum"]), dict(PROFILE, environments=["Prod!"]),
                    dict(PROFILE, restatement=""), dict(PROFILE, extra=1),
                    dict(PROFILE, integrates_existing="yes")):
            with self.assertRaises(InvalidInput, msg=bad):
                self.do(M.set_profile, bad)
        rec = self.do(M.set_profile, PROFILE)
        self.assertEqual(rec["profile"]["kind"], "new")
        self.assertFalse(rec["profile"]["integrates_existing"])

    def test_profile_is_part_of_the_approved_content(self):
        self.do(M.set_profile, PROFILE)
        before = M.architecture_content(self.rec())
        self.do(M.set_profile, dict(PROFILE, kind="update"))
        self.assertNotEqual(before, M.architecture_content(self.rec()))


class Adaptive(Base):
    def test_batches_are_small_and_round_ordered(self):
        self.do(M.set_profile, LANDING_ZONE)
        p = self.plan()
        self.assertLessEqual(len(p["ask"]), catalog.MAX_BATCH)
        self.assertEqual({a["round"] for a in p["ask"]}, {2})
        self.assertGreater(p["remaining_after_batch"], 0)
        self.assertTrue(p["ask"][0]["must_confirm"])  # must-confirm topics first

    def test_simple_request_asks_far_less_than_a_landing_zone(self):
        def total(profile):
            self.do(M.set_profile, profile)
            p = self.plan()
            return len(p["ask"]) + p["remaining_after_batch"], p
        simple, p_simple = total(PROFILE)
        full, p_full = total(LANDING_ZONE)
        self.assertEqual(p_simple["depth"], "simple")
        self.assertEqual(p_full["depth"], "full")
        self.assertLess(simple * 2, full)
        # Optional topics become suggested defaults, not questions, for a simple request.
        self.assertTrue(p_simple["suggested_assumptions"])
        self.assertEqual(p_full["suggested_assumptions"], [])

    def test_only_relevant_categories_are_asked(self):
        self.do(M.set_profile, PROFILE)  # storage only
        asked = set()
        for _ in range(10):
            p = self.plan()
            if not p["ask"]:
                break
            for a in p["ask"]:
                asked.add(a["topic"])
                self.do(M.add_requirement, "answer", a["topic"])
        self.assertIn("data_classification", asked)
        # A simple request gets the conventional default as a suggestion, not a question.
        self.assertIn("storage_redundancy", [x["topic"] for x in self.plan()["suggested_assumptions"]])
        for other in ("address_space", "compute_os", "database_engine", "hosting_platform"):
            self.assertNotIn(other, asked)

    def test_config_answers_are_skipped(self):
        self.do(M.set_profile, PROFILE)
        self.assertIn("region", [a["topic"] for a in self.all_asks()])
        ConfigStore(self.project).set_value("region", "westeurope")
        ConfigStore(self.project).set_value("tagging.owner", "platform team")
        asks = [a["topic"] for a in self.all_asks()]
        self.assertNotIn("region", asks)
        self.assertNotIn("tagging", asks)
        reasons = {s["topic"]: s["reason"] for s in self.plan()["skipped"]}
        self.assertIn("project config (region)", reasons["region"])

    def all_asks(self):
        """Every topic that would be asked, ignoring batching."""
        rec, cfg, envs = self.rec(), ConfigStore(self.project).load(), self.store.production_envs()
        out, seen = [], set()
        import copy
        rec = copy.deepcopy(rec)
        while True:
            batch = planner.plan(rec, cfg, envs)["ask"]
            batch = [b for b in batch if b["topic"] not in seen]
            if not batch:
                return out
            for b in batch:
                seen.add(b["topic"])
                out.append(b)
                rec["requirements"].append({"id": "x", "text": "x", "topic": b["topic"]})

    def test_already_recorded_topics_are_not_asked_again(self):
        self.do(M.set_profile, PROFILE)
        self.do(M.add_requirement, "Subscription confirmed by the user", "target_subscription")
        self.do(M.add_question, "Which region?", "region")
        topics = self.topics()
        self.assertNotIn("target_subscription", topics)
        self.assertNotIn("region", topics)
        with self.assertRaises(Refused):  # one open question per topic
            self.do(M.add_question, "Region again?", "region")

    def test_production_adds_sizing_and_criticality(self):
        self.do(M.set_profile, dict(PROFILE, categories=["compute"], environments=["prod"]))
        asks = [a["topic"] for a in self.all_asks()]
        self.assertIn("criticality", asks)
        self.assertIn("compute_sizing_production", asks)
        self.assertNotIn("compute_sizing", asks)
        self.assertIn("compute_sizing_production", M.unconfirmed_topics(self.rec()))

    def test_configured_production_environment_counts(self):
        ConfigStore(self.project).set_value("environments", "dev,live")
        ConfigStore(self.project).set_value("production_environments", "live")
        self.do(M.set_profile, dict(PROFILE, categories=["compute"], environments=["live"]))
        self.assertIn("compute_sizing_production",
                      M.unconfirmed_topics(self.rec(), self.store.production_envs()))

    def test_removal_requires_destructive_scope(self):
        self.do(M.set_profile, dict(PROFILE, kind="remove"))
        self.assertIn("destructive_scope", M.unconfirmed_topics(self.rec()))
        self.do(M.set_profile, PROFILE)
        self.assertNotIn("destructive_scope", M.unconfirmed_topics(self.rec()))

    def test_unknown_topic_rejected(self):
        self.do(M.set_profile, PROFILE)
        with self.assertRaises(InvalidInput):
            self.do(M.add_requirement, "x", "no_such_topic")

    def test_catalog_is_well_formed(self):
        for tid, t in catalog.TOPICS.items():
            self.assertIn(t["round"], (2, 3), tid)
            self.assertTrue(t["text"] and t["why"], tid)
            if t["applies"] != "*":
                self.assertTrue(set(t["applies"]) <= set(catalog.CATEGORIES), tid)
            if t.get("optional"):
                self.assertTrue(t.get("default"), "%s: optional topics need a default" % tid)
                self.assertFalse(t.get("must_confirm"), tid)
            if t.get("must_confirm"):
                self.assertNotIn("default", t, "%s: must-confirm topics have no default" % tid)


class NeverGuess(Base):
    def setUp(self):
        super().setUp()
        self.do(M.set_profile, dict(PROFILE, categories=["networking"]))

    def test_a_vm_with_its_network_is_still_a_simple_request(self):
        self.do(M.set_profile, dict(PROFILE, categories=["compute", "networking"]))
        p = planner.plan(self.rec(), None, self.store.production_envs())
        self.assertEqual(p["depth"], "simple")
        self.assertLessEqual(len(p["ask"]) + p["remaining_after_batch"], 8)
        self.assertIn("address_space", M.unconfirmed_topics(self.rec()))   # still never guessed
        self.do(M.set_profile, dict(PROFILE, categories=["compute", "networking", "database"]))
        self.assertEqual(planner.plan(self.rec(), None, self.store.production_envs())["depth"], "full")

    def test_must_confirm_question_cannot_be_deferred(self):
        self.do(M.add_question, "Which address space?", "address_space")
        q = self.rec()["questions"][0]
        self.assertTrue(q["must_confirm"])
        with self.assertRaises(Refused):
            self.do(M.defer_question, "Q1", "10.0.0.0/16 is probably free")
        self.assertEqual(self.rec()["assumptions"], [])

    def test_must_confirm_topic_cannot_be_assumed(self):
        for topic in ("address_space", "target_subscription", "public_exposure"):
            with self.assertRaises(Refused):
                self.do(M.add_assumption, "a guess", topic)

    def test_ad_hoc_question_can_be_marked_must_confirm(self):
        self.do(M.add_question, "May the old gateway be replaced?", None, True)
        with self.assertRaises(Refused):
            self.do(M.defer_question, "Q1", "yes")

    def test_architecture_blocked_until_user_answers(self):
        self.do(M.add_requirement, "Hub network for shared services")
        with self.assertRaises(Refused) as cm:
            self.do_env(M.advance, "ARCHITECTURE", False)
        for topic in ("address_space", "public_exposure", "target_subscription"):
            self.assertIn(topic, str(cm.exception))
        self.do(M.add_question, "Which address space?", "address_space")
        self.do(M.answer_question, "Q1", "10.20.0.0/16, confirmed free by the network team")
        self.do(M.add_requirement, "No public exposure", "public_exposure")
        self.do(M.add_requirement, "Subscription confirmed", "target_subscription")
        self.assertEqual(M.unconfirmed_topics(self.rec()), [])
        self.assertEqual(self.do_env(M.advance, "ARCHITECTURE", False)["state"], "ARCHITECTURE")


class Assumptions(Base):
    def setUp(self):
        super().setUp()
        self.do(M.set_profile, PROFILE)
        self.do(M.add_requirement, "Logs kept 90 days")
        for t in M.unconfirmed_topics(self.rec()):
            self.do(M.add_requirement, "confirmed", t)

    def test_no_assumptions_needs_no_review(self):
        self.assertTrue(M.assumptions_reviewed(self.rec()))
        with self.assertRaises(Refused):
            self.do(M.review_assumptions, "000000000000")

    def test_review_required_and_bound_to_the_list(self):
        self.do(M.add_assumption, "LRS redundancy", "storage_redundancy")
        with self.assertRaises(Refused):
            self.do_env(M.advance, "ARCHITECTURE", False)
        with self.assertRaises(Refused):
            self.do(M.review_assumptions, "not-the-hash")
        self.do(M.review_assumptions, short(M.assumptions_hash(self.rec())))
        self.assertTrue(M.assumptions_reviewed(self.rec()))
        self.do(M.add_assumption, "Soft delete 7 days")  # list changed: review is stale
        self.assertFalse(M.assumptions_reviewed(self.rec()))
        with self.assertRaises(Refused):
            self.do_env(M.advance, "ARCHITECTURE", False)

    def test_assumptions_stay_separate_from_requirements(self):
        self.do(M.add_assumption, "LRS redundancy", "storage_redundancy")
        rec = self.rec()
        self.assertNotIn("LRS redundancy", [r["text"] for r in rec["requirements"]])
        self.do(M.resolve_assumption, "A1", True)
        rec = self.rec()
        self.assertIn("LRS redundancy", [r["text"] for r in rec["requirements"]])
        self.assertEqual(M.open_assumptions(rec), [])

    def test_suggested_default_disappears_once_recorded(self):
        before = [s["topic"] for s in self.plan()["suggested_assumptions"]]
        self.assertIn("budget", before)
        self.do(M.add_assumption, "Lowest-cost SKUs", "budget")
        after = [s["topic"] for s in self.plan()["suggested_assumptions"]]
        self.assertNotIn("budget", after)

    def test_ready_only_when_everything_is_resolved(self):
        self.assertFalse(self.plan()["ready_for_architecture"])
        for _ in range(10):
            for a in self.plan()["ask"]:
                self.do(M.add_requirement, "answer", a["topic"])
        self.do(M.add_assumption, "Lowest-cost SKUs", "budget")
        self.assertFalse(self.plan()["ready_for_architecture"])
        self.assertIn("review-assumptions", self.plan()["next"])
        self.do(M.review_assumptions, short(M.assumptions_hash(self.rec())))
        self.assertTrue(self.plan()["ready_for_architecture"])


class Proposal(Base):
    def test_complete_proposal_has_no_problems(self):
        self.assertEqual(proposal.problems(ARCH), [])

    def test_each_missing_section_is_named(self):
        for key in ("objective", "scope", "resources", "overview", "dependencies",
                    "naming_tagging_region", "identity_access", "network_security",
                    "monitoring", "cost", "repository_changes", "deployment_strategy",
                    "risks", "unresolved"):
            broken = {k: v for k, v in ARCH.items() if k != key}
            self.assertTrue(any(key in p for p in proposal.problems(broken)), key)

    def test_cost_needs_estimate_or_limitations(self):
        self.assertTrue(proposal.problems(dict(ARCH, cost={})))
        self.assertTrue(proposal.problems(dict(ARCH, cost={"estimate": " "})))
        self.assertEqual(proposal.problems(dict(ARCH, cost={"limitations": "No usage data yet."})), [])

    def test_resources_need_name_type_purpose(self):
        self.assertTrue(proposal.problems(dict(ARCH, resources=[{"name": "x"}])))
        self.assertEqual(proposal.problems(dict(ARCH, resources=[])), [])

    def test_template_is_incomplete_until_filled(self):
        self.assertTrue(proposal.problems(proposal.template()))

    def test_render_separates_confirmed_from_assumed(self):
        rec = self.rec()
        rec["profile"] = M.set_profile(dict(rec), PROFILE)["profile"]
        rec["requirements"] = [{"id": "R1", "text": "Logs kept 90 days"}]
        rec["assumptions"] = [{"id": "A1", "text": "LRS redundancy", "status": "assumed"},
                              {"id": "A2", "text": "Rejected idea", "status": "rejected"}]
        rec["architecture"] = dict(ARCH, diagram="graph TD; app-->st")
        md = proposal.render(rec, "abc123abc123")
        confirmed = md.index("## Confirmed by you")
        assumed = md.index("## Assumptions (not confirmed)")
        self.assertLess(confirmed, md.index("Logs kept 90 days"))
        self.assertLess(assumed, md.index("LRS redundancy"))
        self.assertLess(md.index("Logs kept 90 days"), assumed)
        self.assertNotIn("Rejected idea", md)
        self.assertIn("```mermaid", md)
        self.assertIn("abc123abc123", md)
        self.assertIn("| st-logs | Microsoft.Storage/storageAccounts |", md)

    def test_render_flags_incomplete_proposal(self):
        rec = self.rec()
        rec["architecture"] = {"overview": "x"}
        self.assertIn("INCOMPLETE", proposal.render(rec))


class Cli(Base):
    def test_next_and_proposal_through_the_cli(self):
        code, out = self.cli(DISCOVERY_CLI, "next", self.id)
        self.assertEqual((code, out["round"]), (0, 1))
        self.cli(STATE_CLI, "set-profile", self.id, "--json", json.dumps(PROFILE))
        code, out = self.cli(DISCOVERY_CLI, "next", self.id)
        self.assertEqual(code, 0)
        self.assertTrue(out["ask"])
        self.assertEqual(out["depth"], "simple")
        code, out = self.cli(STATE_CLI, "add-question", self.id, "--text", "Public?",
                             "--topic", "public_exposure")
        code, out = self.cli(STATE_CLI, "defer", self.id, "--question", "Q1", "--assumption", "no")
        self.assertEqual(code, EXIT_REFUSED)
        self.cli(STATE_CLI, "answer", self.id, "--question", "Q1", "--answer", "Nothing public")
        self.cli(STATE_CLI, "add-requirement", self.id, "--text", "Sub confirmed",
                 "--topic", "target_subscription")
        self.cli(STATE_CLI, "add-assumption", self.id, "--text", "LRS", "--topic", "storage_redundancy")
        code, out = self.cli(STATE_CLI, "advance", self.id, "--to", "ARCHITECTURE")
        self.assertEqual(code, EXIT_REFUSED)
        code, out = self.cli(DISCOVERY_CLI, "next", self.id)
        h = out["assumptions_hash"]
        self.assertEqual([a["text"] for a in out["assumptions"]], ["LRS"])
        code, out = self.cli(STATE_CLI, "review-assumptions", self.id, "--confirm", h)
        self.assertEqual(code, 0, out)
        code, out = self.cli(STATE_CLI, "advance", self.id, "--to", "ARCHITECTURE")
        self.assertEqual(code, 0, out)
        code, out = self.cli(DISCOVERY_CLI, "proposal", self.id)
        self.assertEqual(out["problems"], ["no proposal recorded"])
        self.cli(STATE_CLI, "set-architecture", self.id, "--json", json.dumps({"overview": "x"}))
        code, out = self.cli(STATE_CLI, "advance", self.id, "--to", "APPROVAL")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("incomplete", out["message"])
        self.cli(STATE_CLI, "set-architecture", self.id, "--json", json.dumps(ARCH))
        code, out = self.cli(STATE_CLI, "advance", self.id, "--to", "APPROVAL")
        self.assertEqual(code, 0, out)
        code, out = self.cli(DISCOVERY_CLI, "proposal", self.id)
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["approval_hash"], M.pending_approval(self.rec())["hash"])
        self.assertIn(out["approval_hash"], out["markdown"])
        self.assertIn("LRS", out["markdown"])

    def test_template_and_topics(self):
        code, out = self.cli(DISCOVERY_CLI, "template")
        self.assertEqual(code, 0)
        self.assertIn("cost", out["required"])
        code, out = self.cli(DISCOVERY_CLI, "topics")
        self.assertIn("address_space", out["topics"])

    def test_discovery_cli_changes_nothing(self):
        before = self.rec()
        self.cli(DISCOVERY_CLI, "next", self.id)
        self.cli(DISCOVERY_CLI, "proposal", self.id)
        self.assertEqual(before, self.rec())
