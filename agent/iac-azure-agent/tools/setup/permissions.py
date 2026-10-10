"""Checks that the user's Claude Code settings contain the required `ask` rules.

A plugin cannot ship permission rules (docs/decisions.md ADR-001), so the user adds them
(README, "Required permission rules"). The rules make Claude Code prompt the user before
any command that records an approval or changes the configured repository, independently
of the plugin hook (ADR-018).

A rule counts when it is in a `permissions.ask` list and its pattern, read as a glob,
matches every sample form of the command. Rules in `allow` do not count, and an `allow`
rule that matches a sample is reported as a conflict.

Settings files read (https://code.claude.com/docs/en/settings):
  <config dir>/settings.json           user     ($CLAUDE_CONFIG_DIR or ~/.claude)
  <project>/.claude/settings.json       project
  <project>/.claude/settings.local.json local
Managed settings are not read; their location differs per platform.
"""
import fnmatch
import json
import os
import re

REQUIRED = {
    "approve": {
        "rule": "Bash(*tools/state/cli.py* approve *)",
        "samples": ['python3 "/p/tools/state/cli.py" approve req-1 --kind deployment --confirm abc',
                    "python3 /p/tools/state/cli.py --project-dir /x approve req-1 --kind architecture --confirm abc"]},
    "confirm-risk": {
        "rule": "Bash(*tools/state/cli.py* confirm-risk *)",
        "samples": ['python3 "/p/tools/state/cli.py" confirm-risk req-1 --phrase "ACCEPT-RISK abc deletion"',
                    "python3 /p/tools/state/cli.py --project-dir /x confirm-risk req-1 --phrase x"]},
    "set-repo": {
        "rule": "Bash(*tools/config/cli.py* set-repo *)",
        "samples": ['python3 "/p/tools/config/cli.py" set-repo org/infra --default-branch main',
                    "python3 /p/tools/config/cli.py --project-dir /x set-repo org/infra --confirm-switch-from a/b"]},
    "publish": {
        "rule": "Bash(*tools/github/cli.py* publish *)",
        "samples": ['python3 "/p/tools/github/cli.py" publish req-1 --message "Add storage"',
                    "python3 /p/tools/github/cli.py --project-dir /x publish req-1 --message x"]},
    "accept-finding": {
        "rule": "Bash(*tools/config/cli.py* set accepted_findings.*)",
        "samples": ['python3 "/p/tools/config/cli.py" set accepted_findings.CKV_AZURE_206@infra/main.bicep:Microsoft.Storage/storageAccounts.sa "LRS is fine in dev"',
                    "python3 /p/tools/config/cli.py --project-dir /x set accepted_findings.CKV_AZURE_35@infra/a.bicep:T.x reason"]},
    "install-workflow": {
        "rule": "Bash(*tools/github/cli.py* install-workflow*)",
        "samples": ['python3 "/p/tools/github/cli.py" install-workflow',
                    "python3 /p/tools/github/cli.py --project-dir /x install-workflow"]},
    "deploy": {
        "rule": "Bash(*tools/deploy/cli.py* deploy *)",
        "samples": ['python3 "/p/tools/deploy/cli.py" deploy req-1',
                    "python3 /p/tools/deploy/cli.py --project-dir /x deploy req-1 "]},
    "clear": {
        "rule": "Bash(*tools/config/cli.py* clear *)",
        "samples": ['python3 "/p/tools/config/cli.py" clear --confirm-project /x',
                    "python3 /p/tools/config/cli.py --project-dir /x clear --confirm-project /x"]},
}
RULE = re.compile(r"^Bash\((.*)\)$", re.S)


def settings_files(project_root, environ=None):
    env = os.environ if environ is None else environ
    user_dir = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return [("user", os.path.join(user_dir, "settings.json")),
            ("project", os.path.join(project_root, ".claude", "settings.json")),
            ("local", os.path.join(project_root, ".claude", "settings.local.json"))]


def _matches(rule, sample):
    m = RULE.match(rule) if isinstance(rule, str) else None
    if not m:
        return False
    pattern = m.group(1)
    if pattern.endswith(":*"):          # legacy prefix form
        pattern = pattern[:-2] + "*"
    return fnmatch.fnmatchcase(sample, pattern)


def check(project_root, environ=None):
    ask, allow, problems, read, bypass = [], [], [], [], []
    for scope, path in settings_files(project_root, environ):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            problems.append("%s settings (%s) could not be read: %s" % (scope, path, e))
            continue
        read.append(path)
        perms = data.get("permissions") if isinstance(data, dict) else None
        if not isinstance(perms, dict):
            continue
        ask += [r for r in perms.get("ask", []) if isinstance(r, str)]
        allow += [r for r in perms.get("allow", []) if isinstance(r, str)]
        if perms.get("defaultMode") == "bypassPermissions":
            bypass.append(path)
    missing, conflicts = [], []
    for name, spec in sorted(REQUIRED.items()):
        if not any(all(_matches(r, s) for s in spec["samples"]) for r in ask):
            missing.append(spec["rule"])
        for r in allow:
            if any(_matches(r, s) for s in spec["samples"]):
                conflicts.append("allow rule %r also matches the %s command" % (r, name))
    for p in bypass:
        problems.append("%s sets defaultMode to bypassPermissions; do not use that mode with "
                        "this plugin" % p)
    ok = not missing and not conflicts and not problems
    return {"ok": ok, "missing_rules": missing, "conflicts": conflicts, "problems": problems,
            "settings_read": read, "required_rules": [REQUIRED[k]["rule"] for k in sorted(REQUIRED)],
            "note": "Checked by matching rule text against sample commands. That Claude Code "
                    "prompts on these rules in every permission mode is not verified here."}
