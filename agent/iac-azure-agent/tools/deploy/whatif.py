"""Turns Azure what-if output into a change set and risk flags. Pure; no I/O.

The change set is what the user approves: one entry per resource with its change type.
Resource IDs keep the provider path but lose the subscription prefix, so the approved
content holds no subscription ID of its own (the target carries that).

What-if is Azure's prediction, not a guarantee. Changes it marks Unsupported or Ignore
are listed as `uncertain`: they could not be evaluated and are not counted as "no change".
"""
import re

CHANGE_TYPES = ("Create", "Modify", "Delete", "Deploy", "NoChange", "Ignore", "Unsupported")
# Resource types that hold data: changing or deleting them can lose it.
STATEFUL = (
    "microsoft.storage/storageaccounts", "microsoft.sql/servers", "microsoft.sql/managedinstances",
    "microsoft.dbforpostgresql/", "microsoft.dbformysql/", "microsoft.documentdb/",
    "microsoft.keyvault/vaults", "microsoft.compute/disks", "microsoft.compute/virtualmachines",
    "microsoft.cache/redis", "microsoft.recoveryservices/vaults", "microsoft.containerregistry/registries",
    "microsoft.operationalinsights/workspaces", "microsoft.netapp/", "microsoft.kusto/clusters",
)
# Properties whose change forces Azure to replace a resource rather than update it in place.
REPLACING_PATHS = re.compile(r"(^|\.)(location|zones|sku\.name|properties\.hardwareProfile\.vmSize|"
                             r"properties\.storageProfile\.osDisk|properties\.osProfile|kind|"
                             r"properties\.createMode|properties\.addressSpace)(\.|$)", re.I)
BROAD_ROLES = ("8e3af657-a8ff-443c-a75c-2fe8c4bcb635",   # Owner
               "b24988ac-6180-42a0-ab88-20f7382dd24c",   # Contributor
               "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9")   # User Access Administrator
SUB_PREFIX = re.compile(r"^/subscriptions/[^/]+", re.I)


def short_id(resource_id):
    return SUB_PREFIX.sub("", resource_id or "") or "/"


def split_id(resource_id):
    """Returns (resource_group, type, name) from an Azure resource ID."""
    rid = short_id(resource_id)
    m = re.match(r"^/resourceGroups/([^/]+)$", rid, re.I)
    if m:
        return m.group(1), "Microsoft.Resources/resourceGroups", m.group(1)
    rg = None
    m = re.match(r"^/resourceGroups/([^/]+)(/.*)$", rid, re.I)
    if m:
        rg, rid = m.group(1), m.group(2)
    # An extension resource (a role assignment, a lock) has two provider segments; its own
    # type is the last one.
    pieces = re.split(r"/providers/", rid, flags=re.I)
    if len(pieces) < 2 or "/" not in pieces[-1]:
        return rg, "unknown", rid
    provider, _, rest = pieces[-1].partition("/")
    parts = rest.split("/")
    return rg, "%s/%s" % (provider, "/".join(parts[0::2])), "/".join(parts[1::2])


def _delta_paths(delta, prefix=""):
    out = []
    for d in delta or []:
        path = (prefix + "." if prefix else "") + str(d.get("path", ""))
        out.append(path)
        out += _delta_paths(d.get("children"), path)
    return out


def parse(result):
    """result: the JSON object from `az deployment ... what-if --no-pretty-print`."""
    if not isinstance(result, dict) or not isinstance(result.get("changes"), list):
        raise ValueError("what-if output has no changes list")
    changes, uncertain, flags = [], [], set()
    for c in result["changes"]:
        kind = c.get("changeType")
        rid = c.get("resourceId") or ""
        rg, rtype, name = split_id(rid)
        entry = {"id": short_id(rid), "type": rtype, "name": name, "change": kind}
        if rg:
            entry["resource_group"] = rg
        low = rtype.lower()
        stateful = any(low.startswith(s) for s in STATEFUL)
        if kind in ("Unsupported", "Ignore"):
            entry["reason"] = " ".join(str(c.get("unsupportedReason") or "not evaluated by what-if").split())[:300]
            uncertain.append(entry)
            if kind == "Ignore":
                continue
        if kind == "Delete":
            flags.add("deletion")
            if stateful:
                flags.add("data_loss")
        if kind == "Modify":
            paths = _delta_paths(c.get("delta"))
            entry["properties_changed"] = sorted(set(paths))[:40]
            if stateful and any(REPLACING_PATHS.search(p) for p in paths):
                flags.add("stateful_replacement")
        after = c.get("after") or {}
        props = after.get("properties") or {}
        if kind in ("Create", "Modify", "Deploy"):
            if low == "microsoft.network/publicipaddresses":
                flags.add("public_exposure")
            if str(props.get("publicNetworkAccess", "")).lower() == "enabled" or props.get("allowBlobPublicAccess") is True:
                flags.add("public_exposure")
            if low == "microsoft.network/networksecuritygroups" and _open_inbound(props):
                flags.add("public_exposure")
            if low == "microsoft.authorization/roleassignments" and _broad(rid, props):
                flags.add("broad_rbac")
        changes.append(entry)
    for c in result.get("potentialChanges") or []:
        rg, rtype, name = split_id(c.get("resourceId") or "")
        uncertain.append({"id": short_id(c.get("resourceId") or ""), "type": rtype, "name": name,
                          "change": c.get("changeType"), "reason": "potential change: what-if could not be certain"})
    order = {k: i for i, k in enumerate(CHANGE_TYPES)}
    changes.sort(key=lambda e: (order.get(e["change"], 99), e["id"]))
    counts = {k: sum(1 for e in changes if e["change"] == k) for k in CHANGE_TYPES}
    return {"change_set": changes, "uncertain": uncertain, "risk_flags": sorted(flags),
            "counts": {k: v for k, v in counts.items() if v}}


def _open_inbound(props):
    for rule in props.get("securityRules") or []:
        p = rule.get("properties") or {}
        src = str(p.get("sourceAddressPrefix", "")).lower()
        if (str(p.get("direction", "")).lower() == "inbound" and str(p.get("access", "")).lower() == "allow"
                and src in ("*", "internet", "0.0.0.0/0", "any")):
            return True
    return False


def _broad(resource_id, props):
    """A role assignment at subscription or resource-group scope, or of a powerful role."""
    scope = short_id(resource_id).lower().split("/providers/microsoft.authorization/roleassignments")[0]
    at_wide_scope = scope == "" or re.match(r"^/resourcegroups/[^/]+$", scope) is not None
    role = str(props.get("roleDefinitionId", "")).lower()
    return at_wide_scope or any(r in role for r in BROAD_ROLES)


def summarise(parsed):
    parts = ["%d %s" % (n, k.lower()) for k, n in parsed["counts"].items()]
    text = ", ".join(parts) or "no changes"
    if parsed["uncertain"]:
        text += "; %d could not be evaluated" % len(parsed["uncertain"])
    return text
