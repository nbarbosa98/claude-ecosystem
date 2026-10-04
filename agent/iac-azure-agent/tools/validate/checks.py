"""Validation checks over the infrastructure root of the working copy.

Each check returns {"check", "result", "detail", "findings"} with result one of passed,
failed, warning, skipped, unavailable (state/machine.py CHECK_RESULTS):
  unavailable  the tool is not installed or could not run; nothing was checked
  skipped      the check does not apply (for example no parameter files)
Neither is ever reported as passed. Established tools do the real work (Bicep CLI,
Checkov); the only custom checks are structure, a secret scan and a Bicep-only check.

Nothing here contacts Azure. Deployment validation and what-if belong to Milestone 5.
"""
import json
import os
import re
import shutil
import subprocess

from lib import secret_guard
from workspace import bicep_scan

TIMEOUT = 300
MAX_FINDINGS = 50
DIAG = re.compile(r"^(?P<file>.*?)\((?P<line>\d+),(?P<col>\d+)\) : (?P<level>Error|Warning|Info) "
                  r"(?P<code>[\w-]+): (?P<msg>.*)$")
FOREIGN_IAC = re.compile(r"(\.tf|\.tfvars|(^|/)Pulumi\.ya?ml|(azuredeploy|template)(\.parameters)?\.json)$", re.I)
SUBSCRIPTION_ID = re.compile(r"/subscriptions/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def result(check, res, detail, findings=None):
    f = findings or []
    out = {"check": check, "result": res, "detail": detail, "findings": f[:MAX_FINDINGS]}
    if len(f) > MAX_FINDINGS:
        out["findings_truncated"] = len(f) - MAX_FINDINGS
    return out


def run_tool(cmd, cwd):
    """Returns (exit_code, stdout, stderr), or None when the tool cannot be run."""
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.returncode, p.stdout, p.stderr


def tool_versions():
    out = {}
    for name, cmd in (("bicep", ["bicep", "--version"]), ("checkov", ["checkov", "--version"])):
        r = run_tool(cmd, None)
        out[name] = (r[1] or r[2]).strip().splitlines()[-1][:80] if r and r[0] == 0 and (r[1] or r[2]).strip() else None
    return out


def _rel(path, base):
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        rel = path
    return bicep_scan.clean(path if rel.startswith("..") else rel.replace(os.sep, "/"))


def parse_diagnostics(text, base):
    found = []
    for line in text.splitlines():
        m = DIAG.match(line.strip())
        if m:
            found.append({"file": _rel(m.group("file"), base), "line": int(m.group("line")),
                          "level": m.group("level").lower(), "code": m.group("code"),
                          "message": bicep_scan.clean(m.group("msg"), 300)})
    return found


def _bicep(check, subcommand, files, ws_dir, extra=()):
    if not files:
        return result(check, "skipped", "no files for this check")
    findings, broken = [], []
    for rel in files:
        r = run_tool(["bicep", subcommand, rel] + list(extra), ws_dir)
        if r is None:
            return result(check, "unavailable", "the Bicep CLI is not installed or did not run; "
                                                "nothing was checked")
        code, out, err = r
        diags = parse_diagnostics(err + "\n" + (out if subcommand == "lint" else ""), ws_dir)
        findings += diags
        if code != 0 and not any(d["level"] == "error" for d in diags):
            broken.append(rel)
            findings.append({"file": rel, "level": "error", "code": "exit-%d" % code,
                             "message": bicep_scan.clean(" ".join(err.split()), 300)})
    errors = [f for f in findings if f["level"] == "error"]
    warnings = [f for f in findings if f["level"] == "warning"]
    if errors:
        return result(check, "failed", "%d error(s), %d warning(s) in %d file(s)"
                      % (len(errors), len(warnings), len(files)), errors + warnings)
    if warnings:
        return result(check, "warning", "%d warning(s) in %d file(s)" % (len(warnings), len(files)), warnings)
    return result(check, "passed", "%d file(s), no errors or warnings" % len(files))


def check_structure(ws, inv, files):
    problems = []
    root = ws.infra_root
    if not inv["bicep_files"]:
        problems.append("no Bicep files under %s" % root)
    elif not inv["entry_points"]:
        problems.append("no entry point: every Bicep file is referenced as a module by another")
    if not any(f.lower() == ("%s/readme.md" % root).lower() for f in files):
        problems.append("%s/README.md is missing: every deployment needs its documentation" % root)
    for rel, info in inv["files"].items():
        if rel.endswith(".bicepparam"):
            target = info.get("using")
            if not target:
                problems.append("%s has no using statement" % rel)
            else:
                resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target)).replace(os.sep, "/")
                if resolved not in inv["files"]:
                    problems.append("%s uses %s, which does not exist" % (rel, bicep_scan.clean(target)))
        for m in info["modules"]:
            src = m["source"]
            if src.startswith(("br:", "br/", "ts:", "ts/")):
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(rel), src)).replace(os.sep, "/")
            if resolved not in inv["files"]:
                problems.append("%s references module %s, which does not exist" % (rel, src))
        if rel.endswith(".bicep") and os.path.getsize(os.path.join(ws.dir, *rel.split("/"))) == 0:
            problems.append("%s is empty" % rel)
    if problems:
        return result("structure", "failed", "%d problem(s)" % len(problems),
                      [{"message": p} for p in problems])
    return result("structure", "passed", "entry point(s): %s; README present; module and "
                                         "parameter references resolve" % ", ".join(inv["entry_points"]))


def check_bicep_only(files):
    foreign = [f for f in files if FOREIGN_IAC.search(f)]
    if foreign:
        return result("bicep-only", "failed", "non-Bicep infrastructure definitions under the "
                                              "infrastructure root",
                      [{"file": bicep_scan.clean(f)} for f in foreign])
    return result("bicep-only", "passed", "no Terraform, ARM JSON or Pulumi definitions")


def check_secrets(ws, files):
    findings = []
    for rel in files:
        full = os.path.join(ws.dir, *rel.split("/"))
        try:
            if os.path.getsize(full) > 1024 * 1024:
                continue
            with open(full, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            for name in secret_guard.value_findings(line):
                findings.append({"file": bicep_scan.clean(rel), "line": n, "kind": name})
            if SUBSCRIPTION_ID.search(line) and rel.lower().endswith(".bicep"):
                findings.append({"file": bicep_scan.clean(rel), "line": n,
                                 "kind": "hard-coded subscription id (parameterise it)", "level": "warning"})
    secrets = [f for f in findings if f.get("level") != "warning"]
    if secrets:
        return result("secret-scan", "failed", "%d secret-shaped value(s) found. The values are "
                                               "not shown. Remove them and rotate them if real."
                      % len(secrets), findings)
    if findings:
        return result("secret-scan", "warning", "%d hard-coded identifier(s)" % len(findings), findings)
    return result("secret-scan", "passed", "%d file(s) scanned; pattern-based, see "
                                           "docs/security-model.md for its limits" % len(files))


def check_security(ws, accepted=None):
    accepted = accepted or {}
    r = run_tool(["checkov", "-d", ws.infra_root, "--framework", "bicep", "-o", "json",
                  "--quiet", "--compact"], ws.dir)
    if r is None:
        return result("security-scan", "unavailable", "Checkov is not installed or did not run; "
                                                      "no static security analysis was done")
    code, out, err = r
    try:
        data = json.loads(out)
    except ValueError:
        return result("security-scan", "unavailable", "Checkov produced no readable report "
                                                      "(exit %d); no static security analysis was done" % code)
    reports = data if isinstance(data, list) else [data]
    failed, suppressed, passed, parse_errors = [], [], 0, 0
    for rep in reports:
        if not isinstance(rep, dict):
            continue
        res = rep.get("results") or {}
        summary = rep.get("summary") or rep
        passed += int(summary.get("passed", 0) or 0)
        parse_errors += int(summary.get("parsing_errors", 0) or 0)
        for item in res.get("failed_checks", []) or []:
            f = _finding(item, ws.infra_root)
            if f["code"] in accepted:
                f["accepted"] = bicep_scan.clean(accepted[f["code"]], 200)
                suppressed.append(f)
            else:
                failed.append(f)
        for item in res.get("skipped_checks", []) or []:
            f = _finding(item, ws.infra_root)
            f["suppressed"] = bicep_scan.clean((item.get("check_result") or {}).get("suppress_comment") or "no reason given", 200)
            suppressed.append(f)
    if parse_errors:
        return result("security-scan", "failed", "Checkov could not parse %d file(s)" % parse_errors, failed)
    if failed:
        return result("security-scan", "failed", "%d finding(s), %d check(s) passed, %d suppressed"
                      % (len(failed), passed, len(suppressed)), failed + suppressed)
    if suppressed:
        return result("security-scan", "warning", "%d check(s) passed; %d finding(s) accepted in "
                                                  "the project config or suppressed in code. They "
                                                  "are not passes."
                      % (passed, len(suppressed)), suppressed)
    if not passed:
        return result("security-scan", "skipped", "Checkov found no Bicep resources to check")
    return result("security-scan", "passed", "%d check(s) passed, no findings" % passed)


def _finding(item, infra_root):
    rng = item.get("file_line_range") or [None]
    return {"code": bicep_scan.clean(item.get("check_id"), 40),
            "message": bicep_scan.clean(item.get("check_name"), 200),
            "file": bicep_scan.clean("%s/%s" % (infra_root, str(item.get("file_path") or "").lstrip("/"))),
            "line": rng[0], "resource": bicep_scan.clean(item.get("resource"), 120),
            "guideline": bicep_scan.clean(item.get("guideline") or "", 300)}


NOT_RUN = ["Azure deployment validation and what-if: these need Azure and arrive with Milestone 5."]


def run_all(ws, accepted=None):
    files = ws.infra_files() if os.path.isdir(ws.infra_dir()) else []
    inv = bicep_scan.inventory(ws.dir, files)
    every = inv["bicep_files"]
    return [
        check_structure(ws, inv, files),
        check_bicep_only(files),
        check_secrets(ws, files),
        _bicep("bicep-build", "build", inv["entry_points"], ws.dir, ["--stdout"]),
        _bicep("bicep-build-params", "build-params", inv["parameter_files"], ws.dir, ["--stdout"]),
        _bicep("bicep-lint", "lint", every, ws.dir),
        check_security(ws, accepted),
    ]
