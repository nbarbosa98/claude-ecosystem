"""Workflow rules for one request record. Pure functions over a dict; no I/O.

States, in order:
  REQUEST -> DISCOVERY -> ARCHITECTURE -> APPROVAL -> IMPLEMENTATION -> VALIDATION
  -> GIT_REVIEW -> DEPLOYMENT_APPROVAL -> DEPLOYMENT -> VERIFICATION

Rules enforced here (docs/decisions.md ADR-007 to ADR-009):
  - Forward moves go one state at a time and each has a guard (GUARDS below).
  - Leaving APPROVAL needs a valid architecture approval; leaving DEPLOYMENT_APPROVAL needs
    a valid deployment approval, plus a high-risk confirmation when the plan has risk flags.
  - Approvals are bound to a hash of canonical JSON computed here, never supplied by the
    caller. The caller must echo the current short hash to approve, so an approval always
    refers to the content as it is now.
  - When approved content changes, the approval becomes invalid and, if the workflow is
    past the matching approval state, it returns to that state.
  - Backward moves to any earlier state from DISCOVERY on are allowed with a reason; data
    is kept, results that rework makes stale are marked stale, not deleted.
  - A step that is in progress, failed or interrupted blocks moves until it is resolved.
  - Discovery (ADR-015 to ADR-017): leaving DISCOVERY needs a request profile, every
    must-confirm topic for that profile answered by the user (never assumed), and the
    current list of assumptions reviewed by the user. Leaving ARCHITECTURE needs a proposal
    with every required section.
"""
import datetime
import re

from discovery import catalog, proposal
from lib import secret_guard
from lib.canonical import content_hash, short
from lib.errors import InvalidInput, Refused

SCHEMA_VERSION = 1

STATES = ("REQUEST", "DISCOVERY", "ARCHITECTURE", "APPROVAL", "IMPLEMENTATION", "VALIDATION",
          "GIT_REVIEW", "DEPLOYMENT_APPROVAL", "DEPLOYMENT", "VERIFICATION")
IDX = {s: i for i, s in enumerate(STATES)}
CHECK_RESULTS = ("passed", "failed", "warning", "skipped", "unavailable")
RISK_FLAGS = ("deletion", "stateful_replacement", "data_loss", "public_exposure",
              "broad_rbac", "production_target")
DEPLOYMENT_STATUSES = ("succeeded", "failed", "partial")
FILE_ACTIONS = ("created", "modified", "deleted")
DEFAULT_PRODUCTION_ENVS = ("prod", "production", "prd")

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RG_NAME = re.compile(r"^[-\w._()]{1,90}$")
ENV_NAME = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
SHA = re.compile(r"^[0-9a-f]{7,64}$")
MAX_TEXT = 4000
REQUEST_KINDS = ("new", "update", "troubleshoot", "optimize", "remove")


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _text(value, what, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput("%s must be non-empty text" % what)
    if len(value) > limit:
        raise InvalidInput("%s is longer than %d characters" % (what, limit))
    secret_guard.check(value, what)
    return value.strip()


def _event(rec, event, **detail):
    entry = {"at": now(), "event": event, "state": rec["state"]}
    entry.update({k: v for k, v in detail.items() if v is not None})
    rec["history"].append(entry)


def _next_id(items, prefix):
    return "%s%d" % (prefix, len(items) + 1)


def new_record(rec_id, project_root, intent):
    rec = {
        "schema_version": SCHEMA_VERSION, "id": rec_id, "project_root": project_root,
        "created_at": now(), "updated_at": now(), "revision": 0,
        "state": "REQUEST", "status": "active",
        "intent": _text(intent, "intent"),
        "profile": None, "assumptions_review": None,
        "requirements": [], "questions": [], "assumptions": [],
        "architecture": None, "target": None, "files": [], "validation": [],
        "git": None, "plan": None,
        "approvals": {"architecture": None, "deployment": None, "high_risk": None},
        "deployment": None, "verification": [], "step": None,
        "accepted_incomplete": [], "history": [],
    }
    _event(rec, "created")
    return rec


# ---------------------------------------------------------------- approved content and hashes

def architecture_content(rec):
    """What an architecture approval covers: the proposal and the facts it rests on."""
    return {
        "architecture": rec["architecture"],
        "profile": rec.get("profile"),
        "requirements": [r["text"] for r in rec["requirements"]],
        "assumptions": [a["text"] for a in rec["assumptions"] if a["status"] == "assumed"],
    }


def open_assumptions(rec):
    return [a["text"] for a in rec["assumptions"] if a["status"] == "assumed"]


def assumptions_hash(rec):
    return content_hash(open_assumptions(rec))


def assumptions_reviewed(rec):
    """True when there is nothing assumed, or the user reviewed exactly the current list."""
    if not open_assumptions(rec):
        return True
    review = rec.get("assumptions_review")
    return bool(review) and review["hash"] == assumptions_hash(rec)


def topic_status(rec):
    """Maps each catalog topic touched by this request to answered, deferred or open."""
    out = {}
    for x in rec["assumptions"]:
        if x.get("topic") and x["status"] != "rejected":
            out[x["topic"]] = "deferred"
    for q in rec["questions"]:
        if q.get("topic"):
            out[q["topic"]] = q["status"]
    for r in rec["requirements"]:
        if r.get("topic"):
            out[r["topic"]] = "answered"
    return out


def unconfirmed_topics(rec, production_envs=DEFAULT_PRODUCTION_ENVS):
    """Must-confirm topics for this profile that the user has not answered."""
    profile = rec.get("profile")
    if not profile:
        return []
    status = topic_status(rec)
    return [t for t in catalog.must_confirm(profile, production_envs)
            if status.get(t) != "answered"]


def risk_flags(rec, production_envs=DEFAULT_PRODUCTION_ENVS):
    flags = set((rec.get("plan") or {}).get("risk_flags", []))
    env = (rec.get("target") or {}).get("environment")
    if env and (env in production_envs or env in DEFAULT_PRODUCTION_ENVS):
        flags.add("production_target")
    return sorted(flags)


def deployment_content(rec, production_envs=DEFAULT_PRODUCTION_ENVS):
    """What a deployment approval covers: target plus change set, and what produced it."""
    plan = rec.get("plan") or {}
    return {
        "target": rec["target"],
        "deployment": plan.get("deployment"),
        "change_set": plan.get("change_set"),
        "risk_flags": risk_flags(rec, production_envs),
        "git_commit": (rec.get("git") or {}).get("commit"),
        "files": rec["files"],
        "architecture_hash": content_hash(architecture_content(rec)),
    }


def high_risk_phrase(rec, production_envs=DEFAULT_PRODUCTION_ENVS):
    h = short(content_hash(deployment_content(rec, production_envs)))
    return "ACCEPT-RISK %s %s" % (h, ",".join(risk_flags(rec, production_envs)))


def _approval_valid(rec, kind, production_envs):
    a = rec["approvals"].get(kind)
    if not a or not a.get("valid"):
        return False
    if kind == "architecture":
        return a["hash"] == content_hash(architecture_content(rec))
    current = content_hash(deployment_content(rec, production_envs))
    if kind == "high_risk":
        return a["hash"] == current and a.get("flags") == risk_flags(rec, production_envs)
    return a["hash"] == current


def reconcile(rec, production_envs=DEFAULT_PRODUCTION_ENVS):
    """Invalidates approvals whose content changed and returns the workflow to the matching
    approval state. Called after every change to a record."""
    for kind, gate in (("architecture", "APPROVAL"), ("deployment", "DEPLOYMENT_APPROVAL"),
                       ("high_risk", "DEPLOYMENT_APPROVAL")):
        a = rec["approvals"].get(kind)
        if a and a.get("valid") and not _approval_valid(rec, kind, production_envs):
            a["valid"] = False
            a["invalidated_at"] = now()
            _event(rec, "approval_invalidated", kind=kind,
                   detail="approved content changed after approval")
            if IDX[rec["state"]] > IDX[gate]:
                frm = rec["state"]
                rec["state"] = gate
                _event(rec, "returned_to_approval", **{"from": frm, "to": gate})
    return rec


# ---------------------------------------------------------------- guards

def _require_step_clear(rec):
    st = rec.get("step")
    if st and st["state"] == rec["state"] and st["status"] in ("in_progress", "failed", "interrupted"):
        raise Refused("step '%s' in %s is %s; finish it (step-end), retry it (step-start) "
                      "or move back first" % (st["name"], st["state"], st["status"]))


def _require_active(rec):
    if rec["status"] != "active":
        raise Refused("request %s is %s; no further changes are allowed" % (rec["id"], rec["status"]))


def summarise_checks(checks):
    current = [c for c in checks if not c.get("stale")]
    by = {r: sorted(c["check"] for c in current if c["result"] == r) for r in CHECK_RESULTS}
    if not current:
        verdict = "none"
    elif by["failed"]:
        verdict = "failed"
    elif by["skipped"] or by["unavailable"]:
        verdict = "incomplete"
    elif by["warning"]:
        verdict = "passed_with_warnings"
    else:
        verdict = "passed"
    return {"verdict": verdict, "counts": {r: len(by[r]) for r in CHECK_RESULTS}, "checks": by}


def _require_checks(rec, checks, what, accept_incomplete):
    s = summarise_checks(checks)
    if s["verdict"] in ("none",):
        raise Refused("no %s results recorded" % what)
    if s["verdict"] == "failed":
        raise Refused("%s failed: %s" % (what, ", ".join(s["checks"]["failed"])))
    if s["verdict"] == "incomplete":
        missing = s["checks"]["skipped"] + s["checks"]["unavailable"]
        if not accept_incomplete:
            raise Refused("%s incomplete (skipped or unavailable: %s). These are not passes. "
                          "Pass --accept-incomplete only if the user explicitly accepts "
                          "proceeding without them" % (what, ", ".join(missing)))
        rec["accepted_incomplete"].append({"at": now(), "state": rec["state"],
                                           "what": what, "checks": missing})


def _require_deploy_inputs(rec):
    if not rec.get("plan") or rec["plan"].get("change_set") is None:
        raise Refused("no deployment plan (change set) recorded")
    t = rec.get("target") or {}
    if not all(t.get(k) for k in ("tenant_id", "subscription_id", "resource_group", "environment")):
        raise Refused("deployment target incomplete (tenant, subscription, resource group, "
                      "environment)")
    if not (rec.get("git") or {}).get("commit"):
        raise Refused("git commit not recorded")


def _guard(rec, frm, to, accept_incomplete, production_envs):
    if frm == "REQUEST":
        return
    if frm == "DISCOVERY":
        open_q = [q["id"] for q in rec["questions"] if q["status"] == "open"]
        if open_q:
            raise Refused("outstanding questions: %s. Answer them, or defer them as recorded "
                          "assumptions, before ARCHITECTURE" % ", ".join(open_q))
        if not rec["requirements"]:
            raise Refused("no confirmed requirements recorded")
        if not rec.get("profile"):
            raise Refused("no request profile recorded (set-profile): restate the request and "
                          "record its kind, resource categories and environments first")
        missing = unconfirmed_topics(rec, production_envs)
        if missing:
            raise Refused("these must be confirmed by the user and cannot be assumed: %s"
                          % ", ".join(missing))
        if not assumptions_reviewed(rec):
            raise Refused("the user has not reviewed the current assumptions; show them and "
                          "record the review (review-assumptions) before ARCHITECTURE")
    elif frm == "ARCHITECTURE":
        if not rec["architecture"]:
            raise Refused("no architecture proposal recorded")
        problems = proposal.problems(rec["architecture"])
        if problems:
            raise Refused("architecture proposal incomplete: %s" % "; ".join(problems))
    elif frm == "APPROVAL":
        if not _approval_valid(rec, "architecture", production_envs):
            raise Refused("architecture not approved, or approval no longer matches the "
                          "proposal; approve the current hash first")
    elif frm == "IMPLEMENTATION":
        if not rec["files"]:
            raise Refused("no files recorded as created or changed")
    elif frm == "VALIDATION":
        _require_checks(rec, rec["validation"], "validation", accept_incomplete)
    elif frm == "GIT_REVIEW":
        g = rec.get("git") or {}
        if not g.get("branch") or not g.get("commit"):
            raise Refused("git branch and commit not recorded")
    elif frm == "DEPLOYMENT_APPROVAL":
        _require_deploy_inputs(rec)
        if not _approval_valid(rec, "deployment", production_envs):
            raise Refused("deployment not approved, or approval no longer matches the target "
                          "and change set; approve the current hash first")
        if risk_flags(rec, production_envs) and not _approval_valid(rec, "high_risk", production_envs):
            raise Refused("plan carries high-risk flags (%s); a separate high-risk "
                          "confirmation is required" % ", ".join(risk_flags(rec, production_envs)))
    elif frm == "DEPLOYMENT":
        d = rec.get("deployment")
        if not d or d.get("stale"):
            raise Refused("deployment result not recorded")
        if d["status"] != "succeeded":
            raise Refused("deployment status is %s; move back to rework, do not verify a "
                          "failed deployment as done" % d["status"])
    if IDX[to] > IDX["APPROVAL"] and not _approval_valid(rec, "architecture", production_envs):
        raise Refused("architecture approval is not valid")


# ---------------------------------------------------------------- transitions

def advance(rec, to, accept_incomplete=False, production_envs=DEFAULT_PRODUCTION_ENVS):
    _require_active(rec)
    reconcile(rec, production_envs)
    frm = rec["state"]
    if to not in IDX:
        raise InvalidInput("unknown state %s" % to)
    if IDX[to] != IDX[frm] + 1:
        raise Refused("illegal transition %s -> %s; forward moves go one state at a time "
                      "(next is %s)" % (frm, to, STATES[IDX[frm] + 1] if IDX[frm] + 1 < len(STATES) else "complete"))
    _require_step_clear(rec)
    _guard(rec, frm, to, accept_incomplete, production_envs)
    rec["state"] = to
    _event(rec, "advanced", **{"from": frm, "to": to})
    return rec


def back(rec, to, reason, production_envs=DEFAULT_PRODUCTION_ENVS):
    _require_active(rec)
    frm = rec["state"]
    if to not in IDX:
        raise InvalidInput("unknown state %s" % to)
    if not IDX["DISCOVERY"] <= IDX[to] < IDX[frm]:
        raise Refused("illegal backward move %s -> %s; back moves go to an earlier state "
                      "from DISCOVERY on" % (frm, to))
    st = rec.get("step")
    if st and st["status"] == "in_progress":
        raise Refused("step '%s' is in progress; end it or resume first" % st["name"])
    reason = _text(reason, "reason", 1000)
    if IDX[to] <= IDX["IMPLEMENTATION"]:
        for c in rec["validation"]:
            c["stale"] = True
        rec["validation_run"] = None
    if IDX[to] < IDX["DEPLOYMENT"] and rec.get("deployment"):
        rec["deployment"]["stale"] = True
    if IDX[to] < IDX["VERIFICATION"]:
        for c in rec["verification"]:
            c["stale"] = True
    rec["state"] = to
    _event(rec, "moved_back", reason=reason, **{"from": frm, "to": to})
    reconcile(rec, production_envs)
    return rec


def complete(rec, accept_incomplete=False):
    _require_active(rec)
    if rec["state"] != "VERIFICATION":
        raise Refused("only a request in VERIFICATION can be completed")
    _require_step_clear(rec)
    _require_checks(rec, rec["verification"], "verification", accept_incomplete)
    rec["status"] = "completed"
    _event(rec, "completed")
    return rec


def cancel(rec, reason):
    _require_active(rec)
    rec["status"] = "cancelled"
    _event(rec, "cancelled", reason=_text(reason, "reason", 1000))
    return rec


# ---------------------------------------------------------------- approvals

def pending_approval(rec, production_envs=DEFAULT_PRODUCTION_ENVS):
    """What the user would approve right now, with the short hash to echo back."""
    if rec["state"] == "APPROVAL" and rec["architecture"]:
        return {"kind": "architecture",
                "hash": short(content_hash(architecture_content(rec))),
                "content": architecture_content(rec)}
    if rec["state"] == "DEPLOYMENT_APPROVAL" and rec.get("plan") and rec.get("target"):
        content = deployment_content(rec, production_envs)
        out = {"kind": "deployment", "hash": short(content_hash(content)), "content": content}
        if content["risk_flags"]:
            out["high_risk_flags"] = content["risk_flags"]
            out["high_risk_phrase"] = high_risk_phrase(rec, production_envs)
        return out
    return None


def approve(rec, kind, confirm, production_envs=DEFAULT_PRODUCTION_ENVS):
    _require_active(rec)
    reconcile(rec, production_envs)
    _require_step_clear(rec)
    if kind == "architecture":
        if rec["state"] != "APPROVAL":
            raise Refused("architecture can be approved only in APPROVAL (now %s)" % rec["state"])
        if not rec["architecture"]:
            raise Refused("no architecture proposal recorded")
        h = content_hash(architecture_content(rec))
    elif kind == "deployment":
        if rec["state"] != "DEPLOYMENT_APPROVAL":
            raise Refused("deployment can be approved only in DEPLOYMENT_APPROVAL (now %s)" % rec["state"])
        _require_deploy_inputs(rec)
        h = content_hash(deployment_content(rec, production_envs))
    else:
        raise InvalidInput("kind must be architecture or deployment")
    if not isinstance(confirm, str) or confirm.strip().lower() != short(h):
        raise Refused("confirmation %r does not match the current %s hash; show the current "
                      "content to the user and ask again" % (confirm, kind))
    rec["approvals"][kind] = {"hash": h, "approved_at": now(), "valid": True}
    _event(rec, "approved", kind=kind, hash=short(h))
    return rec


def confirm_high_risk(rec, phrase, production_envs=DEFAULT_PRODUCTION_ENVS):
    _require_active(rec)
    reconcile(rec, production_envs)
    if rec["state"] != "DEPLOYMENT_APPROVAL":
        raise Refused("high-risk confirmation is given only in DEPLOYMENT_APPROVAL")
    _require_deploy_inputs(rec)
    flags = risk_flags(rec, production_envs)
    if not flags:
        raise Refused("the plan carries no high-risk flags; nothing to confirm")
    expected = high_risk_phrase(rec, production_envs)
    if not isinstance(phrase, str) or " ".join(phrase.split()) != expected:
        raise Refused("high-risk phrase does not match. The user must type exactly the phrase "
                      "shown by 'show' (pending_approval.high_risk_phrase)")
    h = content_hash(deployment_content(rec, production_envs))
    rec["approvals"]["high_risk"] = {"hash": h, "flags": flags, "approved_at": now(), "valid": True}
    _event(rec, "high_risk_confirmed", flags=",".join(flags), hash=short(h))
    return rec


# ---------------------------------------------------------------- steps and resume

def step_start(rec, name):
    _require_active(rec)
    st = rec.get("step")
    if st and st["status"] == "in_progress":
        raise Refused("step '%s' is already in progress; end it or resume first" % st["name"])
    name = _text(name, "step name", 200)
    retry = st and st["name"] == name and st["state"] == rec["state"]
    rec["step"] = {"name": name, "state": rec["state"], "status": "in_progress",
                   "started_at": now(), "attempt": st["attempt"] + 1 if retry else 1}
    _event(rec, "step_started", step=rec["step"]["name"])
    return rec


def step_end(rec, result, detail=None):
    _require_active(rec)
    st = rec.get("step")
    if not st or st["status"] != "in_progress":
        raise Refused("no step in progress")
    if result not in ("succeeded", "failed"):
        raise InvalidInput("result must be succeeded or failed")
    st["status"] = result
    st["ended_at"] = now()
    if detail:
        st["detail"] = _text(detail, "detail")
    _event(rec, "step_ended", step=st["name"], result=result)
    return rec


NEXT_ACTION = {
    "REQUEST": "Start discovery: advance to DISCOVERY.",
    "DISCOVERY": "Record the profile, run discovery/cli.py next for the next questions, "
                 "resolve them, and have the user review the assumptions.",
    "ARCHITECTURE": "Record a complete architecture proposal (discovery/cli.py template, "
                    "then set-architecture).",
    "APPROVAL": "Show the proposal and its hash to the user; record approval only on their explicit yes.",
    "IMPLEMENTATION": "Write Bicep in the working copy (/iac-implement), then record the "
                      "changed files (workspace/cli.py record-files).",
    "VALIDATION": "Run validate/cli.py run ID; fix failures by moving back to IMPLEMENTATION.",
    "GIT_REVIEW": "Publish with github/cli.py publish ID, then plan the deployment with "
                  "deploy/cli.py plan ID (Azure validation and what-if).",
    "DEPLOYMENT_APPROVAL": "Show target, change set and risk flags; record approval only on the user's explicit yes.",
    "DEPLOYMENT": "Run deploy/cli.py deploy ID. After an interruption run deploy/cli.py status ID "
                  "to read the real deployment state from Azure before anything else.",
    "VERIFICATION": "Run deploy/cli.py verify ID, then complete.",
}


def resume(rec):
    """Reports where the request stands. An in-progress step found on resume is marked
    interrupted: the process that ran it is gone and its outcome is unknown."""
    st = rec.get("step")
    interrupted = False
    if rec["status"] == "active" and st and st["status"] == "in_progress":
        st["status"] = "interrupted"
        st["ended_at"] = now()
        interrupted = True
        _event(rec, "step_interrupted", step=st["name"])
    return rec, interrupted


# ---------------------------------------------------------------- record data

def _require_state(rec, allowed, what):
    if rec["state"] not in allowed:
        raise Refused("%s can be recorded only in %s (now %s)" % (what, ", ".join(allowed), rec["state"]))


AFTER_DISCOVERY = STATES[IDX["DISCOVERY"]:]
AFTER_ARCH = STATES[IDX["ARCHITECTURE"]:]


def _topic(topic):
    if topic is not None and topic not in catalog.TOPICS:
        raise InvalidInput("unknown topic %r (see discovery/cli.py topics)" % topic)
    return topic


def set_profile(rec, profile):
    """Round 1 result: what kind of request this is and what it touches."""
    _require_active(rec)
    _require_state(rec, AFTER_DISCOVERY, "the request profile")
    if not isinstance(profile, dict):
        raise InvalidInput("profile must be a JSON object")
    extra = set(profile) - {"kind", "categories", "environments", "restatement",
                            "integrates_existing"}
    if extra:
        raise InvalidInput("unknown profile fields: %s" % ", ".join(sorted(extra)))
    if profile.get("kind") not in REQUEST_KINDS:
        raise InvalidInput("kind must be one of %s" % ", ".join(REQUEST_KINDS))
    cats = profile.get("categories")
    if not isinstance(cats, list) or not cats or any(c not in catalog.CATEGORIES for c in cats):
        raise InvalidInput("categories must be a non-empty list drawn from %s"
                           % ", ".join(catalog.CATEGORIES))
    envs = profile.get("environments")
    if not isinstance(envs, list) or not envs or any(
            not isinstance(e, str) or not ENV_NAME.match(e) for e in envs):
        raise InvalidInput("environments must be a non-empty list of environment names")
    if not isinstance(profile.get("integrates_existing", False), bool):
        raise InvalidInput("integrates_existing must be true or false")
    rec["profile"] = {"kind": profile["kind"], "categories": sorted(set(cats)),
                      "environments": sorted(set(envs)),
                      "integrates_existing": profile.get("integrates_existing", False),
                      "restatement": _text(profile.get("restatement"), "restatement")}
    _event(rec, "profile_set", kind=profile["kind"])
    return rec


def review_assumptions(rec, confirm):
    """Records that the user saw the current assumptions and accepts proceeding on them.
    `confirm` is the short hash of the list, so the review refers to the list as it is now."""
    _require_active(rec)
    _require_state(rec, AFTER_DISCOVERY, "an assumptions review")
    if not open_assumptions(rec):
        raise Refused("there are no open assumptions to review")
    h = assumptions_hash(rec)
    if not isinstance(confirm, str) or confirm.strip().lower() != short(h):
        raise Refused("confirmation does not match the current assumptions; show the current "
                      "list to the user and ask again")
    rec["assumptions_review"] = {"hash": h, "at": now(), "count": len(open_assumptions(rec))}
    _event(rec, "assumptions_reviewed", hash=short(h))
    return rec


def add_requirement(rec, text, topic=None):
    _require_active(rec)
    _require_state(rec, AFTER_DISCOVERY, "requirements")
    item = {"id": _next_id(rec["requirements"], "R"), "text": _text(text, "requirement"), "at": now()}
    if _topic(topic):
        item["topic"] = topic
    rec["requirements"].append(item)
    _event(rec, "requirement_added", item=item["id"])
    return item


def add_question(rec, text, topic=None, must_confirm=False):
    _require_active(rec)
    _require_state(rec, AFTER_DISCOVERY, "questions")
    item = {"id": _next_id(rec["questions"], "Q"), "text": _text(text, "question"),
            "status": "open", "at": now()}
    if _topic(topic):
        if any(q.get("topic") == topic and q["status"] == "open" for q in rec["questions"]):
            raise Refused("topic %s already has an open question" % topic)
        item["topic"] = topic
    if must_confirm or (topic and catalog.TOPICS[topic].get("must_confirm")):
        item["must_confirm"] = True
    rec["questions"].append(item)
    _event(rec, "question_added", item=item["id"])
    return item


def _find(items, item_id, what):
    for it in items:
        if it["id"] == item_id:
            return it
    raise InvalidInput("no %s with id %s" % (what, item_id))


def answer_question(rec, qid, answer):
    _require_active(rec)
    q = _find(rec["questions"], qid, "question")
    if q["status"] != "open":
        raise Refused("question %s is already %s" % (qid, q["status"]))
    q["status"] = "answered"
    q["answer"] = _text(answer, "answer")
    req = add_requirement(rec, "%s -> %s" % (q["text"], q["answer"]), q.get("topic"))
    req["source"] = qid
    return q


def defer_question(rec, qid, assumption):
    _require_active(rec)
    q = _find(rec["questions"], qid, "question")
    if q["status"] != "open":
        raise Refused("question %s is already %s" % (qid, q["status"]))
    if q.get("must_confirm"):
        raise Refused("question %s must be answered by the user; it cannot be deferred as an "
                      "assumption (subscription, address ranges, production sizing, public "
                      "exposure and destructive scope are never guessed)" % qid)
    a = add_assumption(rec, assumption, q.get("topic"))
    a["source"] = qid
    q["status"] = "deferred"
    q["assumption"] = a["id"]
    return q


def add_assumption(rec, text, topic=None):
    _require_active(rec)
    _require_state(rec, AFTER_DISCOVERY, "assumptions")
    item = {"id": _next_id(rec["assumptions"], "A"), "text": _text(text, "assumption"),
            "status": "assumed", "at": now()}
    if _topic(topic):
        if catalog.TOPICS[topic].get("must_confirm"):
            raise Refused("topic %s must be confirmed by the user; it cannot be assumed" % topic)
        item["topic"] = topic
    rec["assumptions"].append(item)
    _event(rec, "assumption_added", item=item["id"])
    return item


def resolve_assumption(rec, aid, confirmed):
    """A confirmed assumption becomes a requirement; a rejected one stays on record."""
    _require_active(rec)
    a = _find(rec["assumptions"], aid, "assumption")
    if a["status"] != "assumed":
        raise Refused("assumption %s is already %s" % (aid, a["status"]))
    a["status"] = "confirmed" if confirmed else "rejected"
    if confirmed:
        req = add_requirement(rec, a["text"])
        req["source"] = aid
    _event(rec, "assumption_" + a["status"], item=aid)
    return a


def set_architecture(rec, arch):
    _require_active(rec)
    _require_state(rec, AFTER_ARCH, "the architecture")
    if not isinstance(arch, dict) or not arch:
        raise InvalidInput("architecture must be a non-empty JSON object")
    secret_guard.check(arch, "architecture")
    rec["architecture"] = arch
    _event(rec, "architecture_set", hash=short(content_hash(arch)))
    return rec


def set_target(rec, tenant_id, subscription_id, resource_group, environment):
    _require_active(rec)
    _require_state(rec, AFTER_DISCOVERY, "the target")
    for name, v in (("tenant_id", tenant_id), ("subscription_id", subscription_id)):
        if not isinstance(v, str) or not GUID.match(v):
            raise InvalidInput("%s must be a GUID" % name)
    if not isinstance(resource_group, str) or not RG_NAME.match(resource_group) or resource_group.endswith("."):
        raise InvalidInput("invalid resource group name")
    if not isinstance(environment, str) or not ENV_NAME.match(environment):
        raise InvalidInput("invalid environment name")
    rec["target"] = {"tenant_id": tenant_id.lower(), "subscription_id": subscription_id.lower(),
                     "resource_group": resource_group, "environment": environment}
    _event(rec, "target_set")
    return rec


def add_file(rec, path, action):
    _require_active(rec)
    _require_state(rec, ("IMPLEMENTATION",), "files")
    if action not in FILE_ACTIONS:
        raise InvalidInput("action must be one of %s" % ", ".join(FILE_ACTIONS))
    p = _text(path, "path", 400).replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        raise InvalidInput("file paths are relative to the repository root, without '..'")
    rec["files"] = [f for f in rec["files"] if f["path"] != p] + [{"path": p, "action": action}]
    _event(rec, "file_recorded", path=p, action=action)
    return rec


def _add_check(rec, bucket, check, result, detail):
    if result not in CHECK_RESULTS:
        raise InvalidInput("result must be one of %s" % ", ".join(CHECK_RESULTS))
    name = _text(check, "check name", 200)
    for c in rec[bucket]:
        if c["check"] == name and not c.get("stale"):
            c["stale"] = True
    item = {"check": name, "result": result, "at": now()}
    if detail:
        item["detail"] = _text(detail, "detail")
    rec[bucket].append(item)
    _event(rec, bucket + "_recorded", check=name, result=result)
    return item


def add_validation(rec, check, result, detail=None):
    _require_active(rec)
    _require_state(rec, ("VALIDATION",), "validation results")
    return _add_check(rec, "validation", check, result, detail)


def set_validation_run(rec, tree_hash, tools):
    """Binds the current validation results to the exact files that were checked."""
    _require_active(rec)
    _require_state(rec, ("VALIDATION",), "a validation run")
    if not isinstance(tree_hash, str) or not re.match(r"^[0-9a-f]{64}$", tree_hash):
        raise InvalidInput("tree_hash must be a sha256 hex digest")
    if not isinstance(tools, dict):
        raise InvalidInput("tools must be an object")
    rec["validation_run"] = {"tree_hash": tree_hash, "at": now(), "tools": tools}
    _event(rec, "validation_run", hash=short(tree_hash))
    return rec


def add_verification(rec, check, result, detail=None):
    _require_active(rec)
    _require_state(rec, ("VERIFICATION",), "verification results")
    return _add_check(rec, "verification", check, result, detail)


def set_git(rec, branch=None, commit=None, pr_url=None, published=None):
    _require_active(rec)
    _require_state(rec, STATES[IDX["GIT_REVIEW"]:], "git details")
    g = dict(rec.get("git") or {})
    if branch is not None:
        g["branch"] = _text(branch, "branch", 200)
    if commit is not None:
        if not SHA.match(commit):
            raise InvalidInput("commit must be a lower-case hex SHA")
        g["commit"] = commit
    if pr_url is not None:
        if not re.match(r"^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+$", pr_url):
            raise InvalidInput("pr_url must be https://github.com/<owner>/<repo>/pull/<n>")
        g["pr_url"] = pr_url
    if published is not None:
        # Written by the publish tool after it read the remote back (github/publisher.py).
        g["published"] = published
    rec["git"] = g
    _event(rec, "git_set")
    return rec


def set_plan(rec, plan):
    _require_active(rec)
    _require_state(rec, STATES[IDX["GIT_REVIEW"]:], "the deployment plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("change_set"), list):
        raise InvalidInput("plan must be an object with a change_set list")
    extra = set(plan) - {"change_set", "risk_flags", "summary", "deployment", "uncertain"}
    if extra:
        raise InvalidInput("unknown plan fields: %s" % ", ".join(sorted(extra)))
    flags = plan.get("risk_flags", [])
    if not isinstance(flags, list) or any(f not in RISK_FLAGS for f in flags):
        raise InvalidInput("risk_flags must be a list drawn from %s" % ", ".join(RISK_FLAGS))
    secret_guard.check(plan, "plan")
    dep = plan.get("deployment")
    if dep is not None and (not isinstance(dep, dict) or set(dep) - {
            "scope", "location", "name", "inputs_hash", "resource_group", "template", "parameters"}):
        raise InvalidInput("plan.deployment has unknown fields")
    rec["plan"] = {"change_set": plan["change_set"], "risk_flags": sorted(set(flags)),
                   "summary": plan.get("summary"), "deployment": dep,
                   "uncertain": plan.get("uncertain") or [], "at": now()}
    _event(rec, "plan_set")
    return rec


def set_deployment(rec, status, detail=None, info=None):
    _require_active(rec)
    _require_state(rec, ("DEPLOYMENT",), "the deployment result")
    if status not in DEPLOYMENT_STATUSES:
        raise InvalidInput("status must be one of %s" % ", ".join(DEPLOYMENT_STATUSES))
    rec["deployment"] = {"status": status, "at": now()}
    if detail:
        rec["deployment"]["detail"] = _text(detail, "detail")
    if info is not None:
        if not isinstance(info, dict):
            raise InvalidInput("info must be an object")
        secret_guard.check(info, "deployment info")
        rec["deployment"]["info"] = info
    _event(rec, "deployment_recorded", result=status)
    return rec


def summary(rec, production_envs=DEFAULT_PRODUCTION_ENVS):
    nxt = STATES[IDX[rec["state"]] + 1] if IDX[rec["state"]] + 1 < len(STATES) else None
    return {
        "id": rec["id"], "state": rec["state"], "status": rec["status"],
        "next_state": nxt if rec["status"] == "active" else None,
        "next_action": NEXT_ACTION[rec["state"]] if rec["status"] == "active" else None,
        "step": rec.get("step"),
        "profile": rec.get("profile"),
        "open_questions": [q for q in rec["questions"] if q["status"] == "open"],
        "unconfirmed_topics": unconfirmed_topics(rec, production_envs),
        "assumptions_reviewed": assumptions_reviewed(rec),
        "assumptions_hash": short(assumptions_hash(rec)) if open_assumptions(rec) else None,
        "proposal_problems": proposal.problems(rec["architecture"]) if rec["architecture"] else None,
        "assumptions": [a for a in rec["assumptions"] if a["status"] == "assumed"],
        "approvals": {k: (dict(v, hash=short(v["hash"])) if v else None)
                      for k, v in rec["approvals"].items()},
        "pending_approval": pending_approval(rec, production_envs),
        "risk_flags": risk_flags(rec, production_envs),
        "validation": summarise_checks(rec["validation"]),
        "validation_run": rec.get("validation_run"),
        "verification": summarise_checks(rec["verification"]),
    }
