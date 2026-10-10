"""Plans, executes and verifies a deployment. The only code path that changes Azure.

The hook cannot see the `az deployment ... create` call made here, because it runs inside
this process. The checks in `deploy()` are therefore the gate on that path
(docs/security-model.md). In order, deploy() refuses unless:
  - the request is active and in DEPLOYMENT, with no unresolved deploy step;
  - the deployment approval (and the high-risk confirmation when flags exist) is valid
    for the current target, change set, commit and inputs;
  - the required permission rules are present;
  - az is signed in to exactly the approved tenant and subscription;
  - the working copy is at the approved commit, clean, with the validated files;
  - for a production environment, GitHub reports the request's pull request as merged
    into the default branch at exactly the published commit (ADR-031);
  - the compiled template and parameters hash to the approved inputs hash;
  - a fresh what-if returns exactly the approved change set and risk flags.
Then it runs one create, never retried, in Incremental mode. Nothing is deleted or
rolled back to make a deployment succeed. The result is recorded as succeeded, failed or
partial from what Azure reports, and verified separately by verify().
"""
import hashlib
import os
import subprocess

from deploy import whatif
from deploy.azrun import AzFailure
from github import publisher
from lib.errors import ExternalUnavailable, InvalidInput, Refused
from setup import permissions
from state import machine
from workspace import bicep_scan

SCOPES = {"subscription": "sub", "resourceGroup": "group"}
IMPLICIT_TYPES = ("microsoft.compute/disks",)   # created by Azure alongside a planned resource


def deployment_name(rec_id):
    return "iac-" + rec_id


def inputs(ws, environment):
    """Locates the entry point and parameter file and hashes their compiled form."""
    files = ws.infra_files() if os.path.isdir(ws.infra_dir()) else []
    inv = bicep_scan.inventory(ws.dir, files)
    entry = "%s/main.bicep" % ws.infra_root
    if entry not in inv["files"]:
        if len(inv["entry_points"]) != 1:
            raise Refused("cannot tell which Bicep file is the entry point: %s"
                          % (", ".join(inv["entry_points"]) or "none found"))
        entry = inv["entry_points"][0]
    params = "%s/parameters/%s.bicepparam" % (ws.infra_root, environment)
    if params not in inv["files"]:
        raise Refused("no parameter file %s for environment %r" % (params, environment))
    scope = inv["files"][entry]["target_scope"]
    if scope not in SCOPES:
        raise Refused("deployment scope %r is not supported (subscription and resourceGroup are)" % scope)
    try:
        p = subprocess.run(["bicep", "build-params", params, "--stdout"], cwd=ws.dir,
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        raise ExternalUnavailable("the Bicep CLI is needed to bind the deployment inputs and "
                                  "could not be run; nothing was planned")
    if p.returncode != 0:
        raise Refused("the parameter file does not compile: %s. Is an environment variable it "
                      "reads missing?" % " ".join(p.stderr.split())[:400])
    return {"template": entry, "parameters": params, "scope": scope,
            "inputs_hash": hashlib.sha256(p.stdout.encode("utf-8")).hexdigest()}


def az_args(verb, dep):
    kind = SCOPES[dep["scope"]]
    args = ["deployment", kind, verb, "--name", dep["name"]]
    if kind == "sub":
        args += ["--location", dep["location"]]
    else:
        args += ["--resource-group", dep["resource_group"]]
        if verb == "create":
            args += ["--mode", "Incremental"]
    args += ["--template-file", dep["template"], "--parameters", dep["parameters"]]
    if verb == "what-if":
        args += ["--no-pretty-print"]
    return args


def require_published_tree(ws, rec):
    """The working copy must hold exactly the commit that was validated and published."""
    g = rec.get("git") or {}
    if not g.get("commit") or not (g.get("published") or {}).get("tree_hash"):
        raise Refused("the request's code has not been published (github/cli.py publish); "
                      "only published, validated code is deployed")
    ws.require()
    if ws.head() != g["commit"] or ws.dirty():
        raise Refused("the working copy is not at the published commit %s with a clean tree; "
                      "nothing was done" % g["commit"][:12])
    if ws.tree_hash() != g["published"]["tree_hash"]:
        raise Refused("the files differ from the validated, published files; nothing was done")


def require_merged(ws, rec, run_gh=None):
    """Production only (ADR-031): the pull request must be merged, and what was merged must
    be the commit that was validated, published and approved. Read from GitHub each time."""
    m = publisher.merge_state(ws, rec, run_gh or publisher.gh)
    if not m["merged"]:
        raise Refused("a production deployment needs the pull request to be merged first; "
                      "%s is %s. Merging is yours to do. Nothing was deployed."
                      % (m["url"], str(m["state"] or "unknown").lower()))
    if not m["base_is_default"]:
        raise Refused("the pull request was merged into %r, not the default branch %r. "
                      "Nothing was deployed." % (m["base"], ws.default_branch))
    if not m["head_is_published_commit"]:
        raise Refused("the pull request was merged at %s, not at the published and approved "
                      "commit %s: commits were added after publishing. Nothing was deployed; "
                      "this needs a new request from the merged code."
                      % (str(m["head"])[:12], rec["git"]["commit"][:12]))
    return m


def preview(az, dep):
    """Azure-side validation, then what-if. Both are read-only."""
    val = az.read(az_args("validate", dep))
    err = (val or {}).get("error") or ((val or {}).get("properties") or {}).get("error")
    if err:
        raise AzFailure("invalid_template", str(err.get("message") or err))
    result = az.read(az_args("what-if", dep), timeout=900)
    if not isinstance(result, dict) or result.get("status") not in (None, "Succeeded") or result.get("error"):
        raise AzFailure("error", "what-if did not succeed: %s" % (result or {}).get("error"))
    try:
        return whatif.parse(result)
    except ValueError as e:
        raise AzFailure("error", "what-if output could not be read: %s" % e)


def plan(az, ws, store, rec, environment, tenant, subscription, location=None, resource_group=None):
    if rec["status"] != "active" or rec["state"] not in ("GIT_REVIEW", "DEPLOYMENT_APPROVAL"):
        raise Refused("a deployment is planned in GIT_REVIEW or DEPLOYMENT_APPROVAL (request is %s, %s)"
                      % (rec["state"], rec["status"]))
    require_published_tree(ws, rec)
    ctx = az.require_context(tenant, subscription)
    dep = inputs(ws, environment)
    dep["name"] = deployment_name(rec["id"])
    if dep["scope"] == "subscription":
        if not location:
            raise InvalidInput("a subscription-scope deployment needs --location (or config region)")
        dep["location"] = location
    else:
        if not resource_group:
            raise InvalidInput("a resource-group deployment needs --resource-group")
        dep["resource_group"] = resource_group
    parsed = preview(az, dep)
    groups = sorted({c["name"] for c in parsed["change_set"] if c["type"] == "Microsoft.Resources/resourceGroups"}
                    | {c["resource_group"] for c in parsed["change_set"] if c.get("resource_group")})
    target_rg = resource_group or (groups[0] if len(groups) == 1 else "subscription-scope")

    def apply(r, envs):
        machine.set_target(r, ctx["tenant_id"], ctx["subscription_id"], target_rg, environment)
        machine.set_plan(r, {"change_set": parsed["change_set"], "risk_flags": parsed["risk_flags"],
                             "summary": whatif.summarise(parsed), "deployment": dep,
                             "uncertain": parsed["uncertain"]})
    rec, _ = store.mutate(rec["id"], apply)
    envs = store.production_envs()
    return {"id": rec["id"], "context": ctx, "deployment": dep, "azure_validation": "passed",
            "counts": parsed["counts"], "change_set": parsed["change_set"],
            "uncertain": parsed["uncertain"], "risk_flags": machine.risk_flags(rec, envs),
            "resource_groups": groups, "state": rec["state"],
            "requires_merged_pull_request": environment in envs,
            "note": "What-if is Azure's prediction, not a guarantee. Entries under `uncertain` "
                    "could not be evaluated."}


def _operations(az, dep):
    kind = SCOPES[dep["scope"]]
    args = ["deployment", "operation", kind, "list", "--name", dep["name"]]
    if kind == "group":
        args += ["--resource-group", dep["resource_group"]]
    try:
        ops = az.read(args) or []
    except AzFailure:
        return None
    out = []
    for o in ops:
        p = o.get("properties") or {}
        tr = p.get("targetResource") or {}
        if not tr.get("id"):
            continue
        msg = ((p.get("statusMessage") or {}).get("error") or {}) if isinstance(p.get("statusMessage"), dict) else {}
        out.append({"id": whatif.short_id(tr["id"]), "type": tr.get("resourceType"),
                    "state": p.get("provisioningState"),
                    "error": " ".join(str(msg.get("message") or "").split())[:300] or None})
    return out


def _show(az, dep):
    kind = SCOPES[dep["scope"]]
    args = ["deployment", kind, "show", "--name", dep["name"]]
    if kind == "group":
        args += ["--resource-group", dep["resource_group"]]
    return az.read(args)


def _info(dep, shown):
    p = (shown or {}).get("properties") or {}
    outputs = {k: v.get("value") for k, v in (p.get("outputs") or {}).items()
               if isinstance(v, dict) and str(v.get("type", "")).lower() in ("string", "int", "bool")}
    return {"name": dep["name"], "scope": dep["scope"], "provisioning_state": p.get("provisioningState"),
            "correlation_id": p.get("correlationId"), "timestamp": p.get("timestamp"),
            "outputs": outputs,
            "output_resources": [whatif.short_id(r.get("id", "")) for r in p.get("outputResources") or []]}


def deploy(az, ws, store, rec, run_gh=None):
    envs = store.production_envs()
    machine.reconcile(rec, envs)
    if rec["status"] != "active" or rec["state"] != "DEPLOYMENT":
        raise Refused("deployment runs in DEPLOYMENT, after approval (request is %s, %s)"
                      % (rec["state"], rec["status"]))
    st = rec.get("step")
    if st and st["name"] == "deploy" and st["status"] in ("in_progress", "interrupted"):
        raise Refused("an earlier deployment attempt was interrupted and its outcome is unknown. "
                      "Run deploy/cli.py status first; do not deploy again blindly.")
    done = rec.get("deployment")
    if done and not done.get("stale") and done["status"] == "succeeded":
        raise Refused("this request is already deployed; advance to VERIFICATION")
    if not machine._approval_valid(rec, "deployment", envs):
        raise Refused("there is no valid deployment approval for the current target and change set")
    flags = machine.risk_flags(rec, envs)
    if flags and not machine._approval_valid(rec, "high_risk", envs):
        raise Refused("the plan carries high-risk flags (%s) without a valid confirmation" % ", ".join(flags))
    perms = permissions.check(store.project_root)
    if not perms["ok"]:
        raise Refused("the required permission rules are missing: %s"
                      % "; ".join(perms["missing_rules"] + perms["conflicts"] + perms["problems"]))
    target, dep = rec["target"], rec["plan"]["deployment"]
    az.require_context(target["tenant_id"], target["subscription_id"])
    require_published_tree(ws, rec)
    merged = require_merged(ws, rec, run_gh) if target["environment"] in envs else None
    now = inputs(ws, target["environment"])
    if now["inputs_hash"] != dep["inputs_hash"] or now["template"] != dep["template"]:
        raise Refused("the compiled template or parameters differ from what was approved (for "
                      "example a changed environment variable). Plan and approve again.")
    fresh = preview(az, dep)
    if fresh["change_set"] != rec["plan"]["change_set"] or fresh["risk_flags"] != rec["plan"]["risk_flags"]:
        raise Refused("Azure's what-if now differs from the approved change set (Azure or the "
                      "subscription changed since approval). Nothing was deployed. Plan again "
                      "and get a new approval.")

    store.mutate(rec["id"], lambda r, e: machine.step_start(r, "deploy"))
    try:
        az.change(az_args("create", dep))
    except AzFailure as failure:
        ops = _operations(az, dep)
        try:
            shown = _show(az, dep)
        except AzFailure:
            shown = None
        ok = [o for o in ops or [] if o["state"] == "Succeeded"]
        bad = [o for o in ops or [] if o["state"] not in ("Succeeded", None)]
        status = "partial" if ok else "failed"
        detail = ("%s. Resources Azure reports as created or updated before the failure: %s. "
                  "Failed: %s. Nothing was rolled back or deleted."
                  % (str(failure)[:900], ", ".join(o["id"] for o in ok) or
                     ("none" if ops is not None else "unknown, the operations could not be read"),
                     ", ".join("%s (%s)" % (o["id"], o["error"] or o["state"]) for o in bad) or "none listed"))
        info = _info(dep, shown)
        info["operations"] = ops

        def record(r, e):
            machine.set_deployment(r, status, detail[:machine.MAX_TEXT], info)
            machine.step_end(r, "failed", str(failure)[:1000])
        store.mutate(rec["id"], record)
        failure.args = (detail,)
        raise
    shown = _show(az, dep)
    info = _info(dep, shown)
    state = info["provisioning_state"]
    status = "succeeded" if state == "Succeeded" else "failed"

    def record(r, e):
        machine.set_deployment(r, status, "Azure reports provisioning state %s" % state, info)
        machine.step_end(r, "succeeded" if status == "succeeded" else "failed")
    rec, _ = store.mutate(rec["id"], record)
    return {"id": rec["id"], "status": status, "deployment": info,
            "merged_pull_request": merged,
            "planned": rec["plan"]["change_set"],
            "reported_by_azure": info["output_resources"],
            "independently_verified": [],
            "next": "advance to VERIFICATION and run deploy/cli.py verify; until then nothing "
                    "above has been checked against the resources themselves"}


def status(az, rec, store, record=False):
    """Reads the real state of the request's deployment from Azure. Read-only unless
    `record` is set and an interrupted deploy step can be resolved from Azure's answer."""
    plan_ = rec.get("plan") or {}
    dep = plan_.get("deployment")
    if not dep:
        raise Refused("no deployment has been planned for this request")
    az.require_context(rec["target"]["tenant_id"], rec["target"]["subscription_id"])
    try:
        shown = _show(az, dep)
    except AzFailure as e:
        if "DeploymentNotFound" in str(e) or "could not be found" in str(e) or "ResourceGroupNotFound" in str(e):
            shown = None
        else:
            raise
    info = _info(dep, shown) if shown else None
    out = {"id": rec["id"], "deployment_name": dep["name"], "exists_in_azure": bool(shown),
           "azure": info, "operations": _operations(az, dep) if shown else None,
           "recorded": rec.get("deployment"), "step": rec.get("step"), "resolved": False}
    st = rec.get("step") or {}
    unresolved = st.get("name") == "deploy" and st.get("status") in ("in_progress", "interrupted")
    if record and unresolved and rec["state"] == "DEPLOYMENT":
        state = (info or {}).get("provisioning_state")
        if not shown:
            def fix(r, e):
                machine.resume(r)
                machine.step_start(r, "deploy")
                machine.step_end(r, "failed", "Azure has no deployment with this name; nothing was started")
            store.mutate(rec["id"], fix)
            out["resolved"] = "no deployment exists in Azure; it is safe to deploy"
        elif state in ("Succeeded", "Failed", "Canceled"):
            ok = [o for o in out["operations"] or [] if o["state"] == "Succeeded"]
            result = "succeeded" if state == "Succeeded" else ("partial" if ok else "failed")
            info["operations"] = out["operations"]

            def fix(r, e):
                machine.resume(r)
                machine.step_start(r, "deploy")
                machine.set_deployment(r, result, "Recorded from Azure after an interrupted run: %s" % state, info)
                machine.step_end(r, "succeeded" if result == "succeeded" else "failed")
            store.mutate(rec["id"], fix)
            out["resolved"] = "recorded as %s from Azure's answer" % result
        else:
            out["resolved"] = False
            out["note"] = "Azure reports %s: the deployment is still running. Check again later." % state
    return out


def verify(az, store, rec):
    if rec["status"] != "active" or rec["state"] != "VERIFICATION":
        raise Refused("verification runs in VERIFICATION (request is %s)" % rec["state"])
    target, dep = rec["target"], rec["plan"]["deployment"]
    az.require_context(target["tenant_id"], target["subscription_id"])
    sub = "/subscriptions/%s" % target["subscription_id"]
    shown = _show(az, dep)
    info = _info(dep, shown)
    checks, verified, missing, vms = [], [], [], []
    planned = [c for c in rec["plan"]["change_set"] if c["change"] in ("Create", "Modify", "Deploy")]
    for c in planned:
        try:
            if c["type"] == "Microsoft.Resources/resourceGroups":
                res = az.read(["group", "show", "--name", c["name"]])
            else:
                res = az.read(["resource", "show", "--ids", sub + c["id"]])
        except AzFailure as e:
            missing.append({"id": c["id"], "problem": str(e)[:200]})
            continue
        state = ((res or {}).get("properties") or {}).get("provisioningState")
        entry = {"id": c["id"], "type": c["type"], "provisioning_state": state}
        if state in ("Succeeded", None):
            verified.append(entry)
        else:
            missing.append({"id": c["id"], "problem": "provisioning state %s" % state})
        if c["type"].lower() == "microsoft.compute/virtualmachines":
            vms.append(c)
    state = info["provisioning_state"]
    checks.append(("deployment-state", "passed" if state == "Succeeded" else "failed",
                   "Azure reports the deployment %s as %s" % (dep["name"], state)))
    checks.append(("resources-exist", "failed" if missing else ("passed" if planned else "skipped"),
                   "%d of %d planned resources read back from Azure" % (len(verified), len(planned))
                   + ("; problems: %s" % "; ".join("%s: %s" % (m["id"], m["problem"]) for m in missing) if missing else "")))
    power = []
    for vm in vms:
        try:
            view = az.read(["vm", "get-instance-view", "--ids", sub + vm["id"],
                            "--query", "instanceView.statuses[].code"])
        except AzFailure as e:
            power.append((vm["name"], "unknown: %s" % str(e)[:120]))
            continue
        codes = [c for c in view or [] if str(c).startswith("PowerState/")]
        power.append((vm["name"], codes[0] if codes else "unknown"))
    if vms:
        bad = [p for p in power if p[1] != "PowerState/running"]
        checks.append(("vm-power-state", "failed" if bad else "passed",
                       ", ".join("%s is %s" % p for p in power)))
    extra = []
    groups = sorted({c["name"] for c in planned if c["type"] == "Microsoft.Resources/resourceGroups"})
    known = {c["id"].lower() for c in rec["plan"]["change_set"]}
    for g in groups:
        try:
            for r in az.read(["resource", "list", "--resource-group", g]) or []:
                rid = whatif.short_id(r.get("id", ""))
                if rid.lower() not in known:
                    extra.append({"id": rid, "type": r.get("type"),
                                  "implicit": str(r.get("type", "")).lower() in IMPLICIT_TYPES})
        except AzFailure:
            checks.append(("unplanned-resources", "unavailable", "could not list resources in %s" % g))
    unexpected = [e for e in extra if not e["implicit"]]
    if groups and not any(c[0] == "unplanned-resources" for c in checks):
        checks.append(("unplanned-resources", "warning" if unexpected else "passed",
                       ("resources present that were not in the plan: %s" % ", ".join(e["id"] for e in unexpected))
                       if unexpected else "nothing beyond the plan%s"
                       % (" and the disks Azure creates with a VM" if extra else "")))

    def record(r, e):
        for c in r["verification"]:
            c["stale"] = True
        for name, result, detail in checks:
            machine.add_verification(r, name, result, detail[:machine.MAX_TEXT])
    rec, _ = store.mutate(rec["id"], record)
    return {"id": rec["id"], "verdict": machine.summarise_checks(rec["verification"])["verdict"],
            "checks": [{"check": n, "result": r, "detail": d} for n, r, d in checks],
            "planned": [c["id"] for c in planned],
            "reported_by_azure": info["output_resources"],
            "independently_verified": verified, "problems": missing,
            "also_present": extra, "outputs": info["outputs"],
            "deployment": {k: info[k] for k in ("name", "provisioning_state", "correlation_id", "timestamp")}}
