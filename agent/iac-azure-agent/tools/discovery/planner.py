"""Picks the next small batch of discovery questions for a request. Pure; no I/O.

Adaptive rules (docs/decisions.md ADR-015):
  - Nothing is asked before the profile exists (round 1).
  - A topic is skipped when the project config already answers it, or the request already
    has an answer, a deferral or an open question for it.
  - Rounds are worked in order; a batch holds at most catalog.MAX_BATCH topics from one round.
  - Must-confirm topics come first within a round.
  - For a simple request, a topic that has a conventional default is not asked, whether
    it is optional or not: the default is returned as a suggested assumption, which the
    user reviews before ARCHITECTURE. Topics without a default, and must-confirm topics,
    are always asked.
"""
from discovery import catalog
from state import machine

FIRST_RUN_QUESTION = ("Which GitHub repository should I use as the source of truth for your "
                      "Azure Infrastructure-as-Code? Please provide the repository URL or "
                      "`owner/repository`.")


def _config_answers(topic, config):
    field = catalog.TOPICS[topic].get("config")
    return field if field and config and config.get(field) else None


def plan(rec, config, production_envs):
    profile = rec.get("profile")
    if not profile:
        return {"round": 1, "ready_for_architecture": False, "ask": [], "skipped": [],
                "suggested_assumptions": [],
                "next": "Restate the request in plain English, name any ambiguity, and record "
                        "the profile (state/cli.py set-profile): kind, resource categories, "
                        "environments, whether existing infrastructure is involved."}
    status = machine.topic_status(rec)
    how_deep = catalog.depth(profile, production_envs)
    assumed = {a.get("topic") for a in rec["assumptions"]}
    ask, skipped, suggested = [], [], []
    for tid in sorted(catalog.TOPICS, key=lambda t: (catalog.TOPICS[t]["round"],
                                                     not catalog.TOPICS[t].get("must_confirm"), t)):
        t = catalog.TOPICS[tid]
        if not catalog.applies(tid, profile, production_envs):
            continue
        if tid in status:
            skipped.append({"topic": tid, "reason": "already %s in this request" % status[tid]})
            continue
        field = _config_answers(tid, config)
        if field and not t.get("must_confirm"):
            skipped.append({"topic": tid, "reason": "answered by project config (%s)" % field})
            continue
        if how_deep == "simple" and t.get("default") and not t.get("must_confirm"):
            suggested.append({"topic": tid, "assumption": t["default"], "why": t["why"]})
            continue
        item = {"topic": tid, "round": t["round"], "text": t["text"], "why": t["why"],
                "must_confirm": bool(t.get("must_confirm"))}
        if t.get("default") and not t.get("must_confirm"):
            item["default"] = t["default"]
        ask.append(item)
    current = min((a["round"] for a in ask), default=None)
    batch = [a for a in ask if a["round"] == current][:catalog.MAX_BATCH]
    open_q = [q["id"] for q in rec["questions"] if q["status"] == "open"]
    unconfirmed = machine.unconfirmed_topics(rec, production_envs)
    reviewed = machine.assumptions_reviewed(rec)
    ready = not ask and not open_q and not unconfirmed and reviewed and bool(rec["requirements"])
    if batch:
        nxt = ("Ask these %d questions (round %d). Record each answer with add-requirement "
               "--topic, or add-question --topic and answer/defer." % (len(batch), current))
    elif open_q:
        nxt = "Resolve the open questions: %s." % ", ".join(open_q)
    elif not reviewed:
        nxt = ("Round 4: show the user every assumption and trade-off, then record their "
               "review (state/cli.py review-assumptions --confirm <assumptions_hash>).")
    elif not rec["requirements"]:
        nxt = "Record at least one confirmed requirement."
    else:
        nxt = "Discovery is complete: advance to ARCHITECTURE."
    return {"round": current if batch else (4 if not ready else None), "depth": how_deep,
            "ready_for_architecture": ready, "ask": batch,
            "remaining_after_batch": len(ask) - len(batch),
            "suggested_assumptions": [s for s in suggested if s["topic"] not in assumed],
            "skipped": skipped, "open_questions": open_q, "unconfirmed_topics": unconfirmed,
            "assumptions_reviewed": reviewed, "next": nxt}
