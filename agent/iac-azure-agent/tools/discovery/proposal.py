"""Architecture proposal: required sections, completeness check, plain-text rendering.

The proposal is the JSON object stored with set-architecture and covered by the
architecture approval hash. render() produces what the user is shown from that same
object, so what is approved is what was displayed. Pure; no I/O.
"""
TEXT_SECTIONS = (
    ("objective", "Objective"),
    ("scope", "Scope"),
    ("overview", "Architecture overview"),
    ("dependencies", "Resource dependencies"),
    ("naming_tagging_region", "Naming, tagging and region"),
    ("identity_access", "Identity and access"),
    ("network_security", "Network and security"),
    ("monitoring", "Monitoring and diagnostics"),
    ("repository_changes", "Repository changes"),
    ("deployment_strategy", "Deployment strategy"),
)
LIST_SECTIONS = (("risks", "Risks"), ("unresolved", "Unresolved questions"))
RESOURCE_FIELDS = ("name", "type", "purpose")


def _nonempty(v):
    return isinstance(v, str) and bool(v.strip())


def problems(arch):
    """Returns a list of what is missing or malformed; empty means complete."""
    if not isinstance(arch, dict):
        return ["proposal is not an object"]
    out = []
    for key, _ in TEXT_SECTIONS:
        if not _nonempty(arch.get(key)):
            out.append("missing section '%s'" % key)
    res = arch.get("resources")
    if not isinstance(res, list):
        out.append("missing section 'resources' (a list; empty only when nothing is created or changed)")
    else:
        for i, r in enumerate(res):
            if not isinstance(r, dict) or any(not _nonempty(r.get(f)) for f in RESOURCE_FIELDS):
                out.append("resources[%d] needs name, type and purpose" % i)
    cost = arch.get("cost")
    if not isinstance(cost, dict) or not (_nonempty(cost.get("estimate")) or _nonempty(cost.get("limitations"))):
        out.append("missing section 'cost' (an estimate, or the limitations that prevent one)")
    for key, _ in LIST_SECTIONS:
        v = arch.get(key)
        if not isinstance(v, list) or any(not _nonempty(x) for x in v):
            out.append("missing section '%s' (a list of text items; may be empty)" % key)
    if "diagram" in arch and not _nonempty(arch["diagram"]):
        out.append("'diagram' must be text (for example Mermaid) when present")
    return out


def template():
    t = {key: "" for key, _ in TEXT_SECTIONS}
    t["resources"] = [{"name": "", "type": "", "purpose": ""}]
    t["cost"] = {"estimate": "", "limitations": ""}
    t["risks"] = []
    t["unresolved"] = []
    t["diagram"] = "optional: Mermaid text, only when it helps understanding"
    return t


def _clean(text):
    return " ".join(str(text).split())


def render(rec, pending_hash=None):
    """Markdown for the user. Confirmed requirements and assumptions are kept apart."""
    arch = rec.get("architecture") or {}
    profile = rec.get("profile") or {}
    lines = ["# Architecture proposal", "", "Request: %s" % rec["id"],
             "Intent: %s" % _clean(rec["intent"])]
    if profile:
        lines += ["Kind: %s. Categories: %s. Environments: %s." % (
            profile["kind"], ", ".join(profile["categories"]), ", ".join(profile["environments"]))]
    missing = problems(arch)
    if missing:
        lines += ["", "INCOMPLETE - not ready for approval:"] + ["- %s" % m for m in missing]
    for key, title in TEXT_SECTIONS[:2]:
        lines += ["", "## %s" % title, "", arch.get(key) or "(not provided)"]
    lines += ["", "## Resource inventory", ""]
    res = arch.get("resources") if isinstance(arch.get("resources"), list) else []
    if res:
        lines += ["| Name | Type | Purpose |", "| --- | --- | --- |"]
        for r in res:
            if isinstance(r, dict):
                lines.append("| %s |" % " | ".join(_clean(r.get(f, "")).replace("|", "/")
                                                   for f in RESOURCE_FIELDS))
    else:
        lines.append("No resources are created or changed.")
    if isinstance(arch.get("diagram"), str) and arch["diagram"].strip():
        lines += ["", "## Diagram", "", "```mermaid", arch["diagram"].strip(), "```"]
    for key, title in TEXT_SECTIONS[2:]:
        lines += ["", "## %s" % title, "", arch.get(key) or "(not provided)"]
    cost = arch.get("cost") if isinstance(arch.get("cost"), dict) else {}
    lines += ["", "## Estimated cost", ""]
    if _nonempty(cost.get("estimate")):
        lines.append("Estimate: %s" % cost["estimate"])
    if _nonempty(cost.get("limitations")):
        lines.append("Limitations: %s" % cost["limitations"])
    if not cost:
        lines.append("(not provided)")
    lines += ["", "## Confirmed by you", ""]
    lines += ["- %s: %s" % (r["id"], _clean(r["text"])) for r in rec["requirements"]] or ["- none"]
    assumed = [a for a in rec["assumptions"] if a["status"] == "assumed"]
    lines += ["", "## Assumptions (not confirmed)", ""]
    lines += ["- %s: %s" % (a["id"], _clean(a["text"])) for a in assumed] or ["- none"]
    for key, title in LIST_SECTIONS:
        items = arch.get(key) if isinstance(arch.get(key), list) else []
        lines += ["", "## %s" % title, ""]
        lines += ["- %s" % _clean(x) for x in items] or ["- none"]
    if pending_hash:
        lines += ["", "Approval hash: `%s`. This hash covers the proposal, the confirmed "
                      "requirements and the assumptions above; any change to them needs a "
                      "new approval." % pending_hash]
    return "\n".join(lines) + "\n"
