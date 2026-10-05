#!/usr/bin/env python3
"""PreToolUse hook (Bash, Write, Edit, NotebookEdit) shipped by the iac-azure-agent plugin.

Second enforcement layer (docs/security-model.md). It matches the command TEXT that Claude
writes; it is a guardrail, not a sandbox. A command that reaches Azure through a variable,
an alias, eval, a script file, or a language runtime is not seen here. The first layer is
the tools themselves, which refuse to act without a valid approval record.

Bash rules:
  1. Azure CLI (`az`): only an allowlist of read-only and local commands passes (show, list,
     what-if, validate, bicep build/lint, login, account set, ...). Every other az command,
     including any unknown one, is denied. `az rest` passes only with an explicit
     --method/-m get or head. Reads that print secrets (get-access-token, keys list,
     keyvault secret show, show-connection-string, ...) and `az login` with a secret
     argument are denied.
  2. Azure PowerShell: Verb-Az* cmdlets pass only for read verbs (Get, Test, Find, Resolve,
     Connect, Disconnect) and context switching; everything else is denied.
  3. Standalone `bicep`: build, build-params, lint, format, decompile, generate-params,
     restore, --version and --help pass; publish and anything else are denied.
  4. Raw calls to Azure Resource Manager hosts from anything other than `az` are denied.
  5. Encoded PowerShell (-EncodedCommand and abbreviations) is denied: it hides intent.
  6. Any reference to the agent's config/state store path is denied: records change only
     through tools/config/cli.py and tools/state/cli.py.
  7. Approval commands (state cli.py approve / confirm-risk, config cli.py set-repo,
     config cli.py clear) are answered with permissionDecision "ask", so Claude Code shows
     the user a permission prompt for them. Setting the repository for the first time
     prompts too: establishing the source of truth needs the user's confirmation.
     The deploy command (deploy cli.py deploy) prompts the same way. The az call it makes
     runs inside that tool and is not seen by this hook; the tool's own approval checks are
     the gate on that path.
  9. Publishing (github cli.py publish) and accepting a scanner finding (config cli.py set
     accepted_findings.<id>) are answered with "ask" like the approval commands.
 10. GitHub CLI, only when the caller is the iac-azure-agent agent: read-only `gh` commands
     pass; `gh pr create/merge/close`, `gh repo` changes, `gh api` with a write method or
     fields, and everything else are denied. Publishing goes through github/cli.py, which
     checks validation first; merging is the user's decision. Other sessions are untouched.
  8. git in the working copy (<project>/.iac-azure-agent/workspace/...), or anywhere when
     the caller is the iac-azure-agent agent: only read-only git subcommands pass. Branches
     are created by tools/workspace/cli.py; commit and push belong to the publish tool
     (Milestone 4), which checks validation and approval first.
Write/Edit/NotebookEdit:
  - a file_path inside the store is denied;
  - a file_path inside a working copy is allowed only under that project's configured
    infrastructure root, and never under .git (docs/decisions.md ADR-022);
  - when the hook input names the iac-azure-agent agent (agent_type), every other
    file_path is denied too: the agent writes Bicep and its documentation, nothing else.
    Other sessions keep their normal write access outside the working copy.
Not covered: files written by a shell command (redirection, tee, cp). The tools catch
those afterwards: record-files refuses when anything outside the infrastructure root changed.

Fail closed: unparseable input, an unexpected payload, or any internal error exits 2.
Limits (https://code.claude.com/docs/en/hooks): only exit code 2 blocks. If python3 is
missing, or the hook times out, Claude Code does not block the call.
"""
import json
import os
import re
import shlex
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AZ_READ_VERBS = {"show", "list", "exists", "what-if", "validate", "wait", "version", "help",
                 "check-name-availability", "list-locations", "show-deleted"}
AZ_LOCAL = {
    ("login",), ("logout",), ("version",), ("upgrade",), ("find",), ("interactive",),
    ("account", "set"), ("account", "clear"), ("account", "list-locations"),
    ("bicep", "build"), ("bicep", "build-params"), ("bicep", "lint"), ("bicep", "format"),
    ("bicep", "decompile"), ("bicep", "decompile-params"), ("bicep", "generate-params"),
    ("bicep", "install"), ("bicep", "upgrade"), ("bicep", "version"), ("bicep", "restore"),
    ("bicep", "list-versions"), ("config", "get"), ("extension", "list"),
    ("extension", "show"), ("extension", "list-available"),
}
# Commands that print secrets (keys, connection strings, credentials, tokens) into the
# transcript are denied even though they are reads.
SECRET_WORDS = {"key", "keys", "secret", "secrets", "credential", "credentials", "password",
                "get-access-token", "show-connection-string", "list-keys", "keys-list",
                "generate-sas", "list-secrets", "list-publishing-credentials",
                "list-credentials", "connection-string"}
SECRET_ARGS = {"-p", "--password", "--client-secret", "--secret"}
BICEP_OK = {"build", "build-params", "lint", "format", "decompile", "decompile-params",
            "generate-params", "restore", "--version", "-v", "--help", "-h", "help"}
PS_READ_VERBS = {"get", "test", "find", "resolve", "connect", "disconnect"}
PS_OK_CMDLETS = {"set-azcontext", "select-azsubscription", "select-azcontext"}
ARM_HOST = re.compile(r"management\.(azure\.com|usgovcloudapi\.net|chinacloudapi\.cn)|management\.core\.windows\.net", re.I)
ENCODED_PS = re.compile(r"(?:^|\s)-(e|ec|en|enc|enco|encod|encode|encoded|encodedc\w*)(?:\s|$)", re.I)
PS_HOST = re.compile(r"\b(pwsh|powershell)(\.exe)?\b", re.I)
# Approved PowerShell verbs (Get-Verb). Matching only these keeps names such as
# "iac-azure-agent" from reading as a cmdlet, while staying case-insensitive like PowerShell.
PS_VERBS = ("add|approve|assert|backup|block|build|checkpoint|clear|close|compare|complete|"
            "compress|confirm|connect|convert|convertfrom|convertto|copy|debug|deny|deploy|"
            "disable|disconnect|dismount|edit|enable|enter|exit|expand|export|find|format|get|"
            "grant|group|hide|import|initialize|install|invoke|join|limit|lock|measure|merge|"
            "mount|move|new|open|optimize|out|ping|pop|protect|publish|push|read|receive|redo|"
            "register|remove|rename|repair|request|reset|resize|resolve|restart|restore|resume|"
            "revoke|save|search|select|send|set|show|skip|split|start|step|stop|submit|suspend|"
            "switch|sync|test|trace|unblock|undo|uninstall|unlock|unprotect|unpublish|"
            "unregister|update|use|wait|watch|write")
PS_CMDLET = re.compile(r"\b(" + PS_VERBS + r")-(Az[A-Za-z]*)\b", re.I)
PS_DENY_CMDLETS = {"get-azaccesstoken"}
# Segment separators: ; & | newline, backticks, $( and parentheses.
SEP = re.compile(r"\$\(|[;&|\n`()]")
APPROVAL_CMD = re.compile(r"tools/state/cli\.py\b.*\s(approve|confirm-risk)\b", re.S)
REPO_SWITCH_CMD = re.compile(r"tools/config/cli\.py\b.*\s(set-repo|clear)\b", re.S)
DEPLOY_CMD = re.compile(r"tools/deploy/cli\.py\b.*\sdeploy\b", re.S)
PUBLISH_CMD = re.compile(r"tools/github/cli\.py\b.*\spublish\b", re.S)
ACCEPT_CMD = re.compile(r"tools/config/cli\.py\b.*\sset\s+accepted_findings\.", re.S)
GH_READ = {("auth", "status"), ("repo", "view"), ("repo", "list"), ("pr", "view"), ("pr", "list"),
           ("pr", "status"), ("pr", "checks"), ("pr", "diff"), ("issue", "view"), ("issue", "list"),
           ("run", "view"), ("run", "list"), ("workflow", "view"), ("workflow", "list"),
           ("release", "view"), ("release", "list"), ("status",), ("version",), ("--version",),
           ("help",), ("--help",), ("search",), ("browse",)}
GH_API_WRITE = re.compile(r"(^|\s)(-X|--method)(\s+|=)(?!GET\b|get\b)|(^|\s)(-f|-F|--field|--raw-field|--input)(\s|=)")


WORKSPACE_MARK = "/.iac-azure-agent/workspace/"
AGENT_NAME = "iac-azure-agent"
GIT_READ = {"status", "diff", "log", "show", "ls-files", "ls-tree", "rev-parse", "rev-list",
            "describe", "blame", "grep", "cat-file", "shortlog", "merge-base", "name-rev",
            "for-each-ref", "check-ignore", "version", "help", "--version", "--help"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


class Deny(Exception):
    pass


def is_agent(data):
    kind = data.get("agent_type")
    return isinstance(kind, str) and kind.split(":")[-1] == AGENT_NAME


def git_invocations(command):
    """Yields (subcommand, args after it) for each `git` program token in the command."""
    for seg in SEP.split(strip_quotes(command)):
        w = words(seg)
        for i, tok in enumerate(w):
            if os.path.basename(tok).lower() not in ("git", "git.exe"):
                continue
            rest, k = w[i + 1:], 0
            while k < len(rest) and rest[k].startswith("-") and rest[k] not in ("--version", "--help"):
                k += 2 if rest[k] in GIT_OPTS_WITH_VALUE else 1
            if k < len(rest):
                yield rest[k].lower(), rest[k + 1:]


def git_is_read_only(sub, args):
    if sub in GIT_READ:
        return True
    first = args[0] if args else None
    if sub == "branch":
        return all(a.startswith("-") for a in args) and not any(
            a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "-f", "--force")
            for a in args)
    if sub == "remote":
        return first in (None, "-v", "--verbose", "get-url", "show")
    if sub == "config":
        return first in ("--get", "--get-all", "--list", "-l")
    if sub == "stash":
        return first in ("list", "show")
    if sub == "tag":
        return first in (None, "-l", "--list")
    return False


def check_git(command, cwd, agent):
    norm = command.replace("\\", "/")
    here = str(cwd or "").replace("\\", "/") + "/"
    if not (agent or ".iac-azure-agent/workspace" in norm or WORKSPACE_MARK in here):
        return
    for sub, args in git_invocations(command):
        if not git_is_read_only(sub, args):
            raise Deny("`git %s` is blocked in the iac-azure-agent working copy. Use "
                       "tools/workspace/cli.py (clone, sync, begin). Commit and push belong "
                       "to the publish tool (Milestone 4)" % sub)


def check_gh(command, agent):
    if not agent:
        return
    for seg in SEP.split(strip_quotes(command)):
        w = words(seg)
        for i, tok in enumerate(w):
            if os.path.basename(tok).lower() not in ("gh", "gh.exe"):
                continue
            rest = [a for a in w[i + 1:]]
            path = []
            for a in rest:
                if a.startswith("-") and a not in ("--version", "--help"):
                    break
                path.append(a.lower())
            if path[:1] == ["api"]:
                if GH_API_WRITE.search(" " + " ".join(rest[1:]) + " "):
                    raise Deny("`gh api` with a write method or fields is blocked for this agent")
                continue
            if tuple(path[:2]) in GH_READ or tuple(path[:1]) in GH_READ:
                continue
            raise Deny("`gh %s` is blocked for this agent. Publishing goes through "
                       "tools/github/cli.py publish; merging a pull request is the user's "
                       "decision" % " ".join(path[:2]))


def workspace_write_ok(real):
    """For a path inside a working copy: None when writable, else the reason it is not."""
    norm = real.replace("\\", "/")
    project, _, tail = norm.partition(WORKSPACE_MARK)
    parts = tail.split("/")
    if len(parts) < 2 or not parts[0]:
        return "this is not a file inside a working copy"
    if ".git" in parts[1:]:
        return "git metadata is never edited directly"
    sys.path.insert(0, os.path.join(PLUGIN_ROOT, "tools"))
    from config.store import ConfigStore
    try:
        cfg = ConfigStore(project).load() or {}
    except Exception:  # noqa: BLE001 - unreadable config: fail closed
        return "the project config could not be read, so the infrastructure root is unknown"
    infra = cfg.get("infra_root")
    repo = cfg.get("repository") or {}
    if not infra or parts[0] != "%s--%s" % (repo.get("owner"), repo.get("name")):
        return "this working copy does not belong to the configured repository, or no infrastructure root is configured"
    inside = "/".join(parts[1:])
    if inside == infra or not inside.startswith(infra + "/"):
        return "only files under the infrastructure root %r may be written" % infra
    return None


def words(segment):
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace split rather than skipping the check.
        return segment.replace('"', " ").replace("'", " ").split()


def strip_quotes(text):
    """Quoted strings are checked as code too (bash -c '...', pwsh -Command "...")."""
    return text.replace('"', " ").replace("'", " ")


def az_invocations(command):
    """Yields the argument words after each `az` program token in the command."""
    flat = strip_quotes(command)
    for seg in SEP.split(flat):
        w = words(seg)
        for i, tok in enumerate(w):
            if os.path.basename(tok).lower() in ("az", "az.cmd", "az.exe"):
                yield w[i + 1:]


def check_az(args):
    path = []
    for t in args:
        if t.startswith("-"):
            break
        path.append(t.lower())
    if not path or path[0] in ("--version", "--help", "-h"):
        return
    if any(a in ("--help", "-h") for a in args) and len(path) <= 3:
        return
    tpath = tuple(path)
    if any(t in SECRET_WORDS for t in tpath) or any(t.endswith(("-keys", "-secret", "-sas", "-credentials")) for t in tpath):
        raise Deny("`az %s` reads or creates keys, secrets, tokens or credentials; this agent "
                   "never handles secret values" % " ".join(path))
    if tpath[0] == "login" and any(a.split("=", 1)[0] in SECRET_ARGS for a in args):
        raise Deny("a secret on the command line is blocked; use interactive or federated "
                   "sign-in")
    if tpath[0] == "rest":
        method = None
        for i, a in enumerate(args):
            if a in ("--method", "-m") and i + 1 < len(args):
                method = args[i + 1].lower()
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].lower()
        if method not in ("get", "head"):
            raise Deny("`az rest` is allowed only with an explicit --method get or head")
        return
    if tpath in AZ_LOCAL or tpath[:2] in AZ_LOCAL:
        return
    verb = tpath[-1]
    if verb in AZ_READ_VERBS or verb.startswith(("show-", "list-", "get-")):
        return
    raise Deny("`az %s` can change Azure resources or is not on the read-only allowlist. "
               "Changes to Azure go through the plugin's approval-checked deploy tool "
               "(Milestone 5), never directly from the shell" % " ".join(path))


def check_bicep(command):
    for seg in SEP.split(strip_quotes(command)):
        w = words(seg)
        for i, tok in enumerate(w):
            if os.path.basename(tok).lower() in ("bicep", "bicep.exe") and (i == 0 or os.path.basename(w[i - 1]).lower() not in ("az", "az.cmd", "az.exe")):
                sub = w[i + 1] if i + 1 < len(w) else "--help"
                if sub.lower() not in BICEP_OK:
                    raise Deny("`bicep %s` is not on the allowlist (publishing to a registry "
                               "is a change)" % sub)


def check_powershell(command):
    if PS_HOST.search(command) and ENCODED_PS.search(command):
        raise Deny("encoded PowerShell commands are blocked: they hide what will run")
    for verb, noun in PS_CMDLET.findall(command):
        name = ("%s-%s" % (verb, noun)).lower()
        if name in PS_DENY_CMDLETS:
            raise Deny("%s prints an access token; blocked" % name)
        if verb.lower() in PS_READ_VERBS or name in PS_OK_CMDLETS:
            continue
        raise Deny("Azure PowerShell cmdlet %s-%s can change Azure resources; blocked"
                   % (verb, noun))


def check_arm_rest(command):
    if not ARM_HOST.search(command):
        return
    for seg in SEP.split(command):
        if ARM_HOST.search(seg):
            w = words(strip_quotes(seg))
            if not w or os.path.basename(w[0]).lower() not in ("az", "az.cmd", "az.exe"):
                raise Deny("raw calls to Azure Resource Manager are blocked; use read-only "
                           "`az` commands")


def store_root():
    sys.path.insert(0, os.path.join(PLUGIN_ROOT, "tools"))
    from lib import paths
    return paths.store_root()


def in_store(text, root):
    norm = lambda s: os.path.normcase(s.replace("\\", "/")).rstrip("/")
    r = norm(root)
    home = norm(os.path.expanduser("~"))
    candidates = {r}
    if r.startswith(home + "/"):
        candidates.add("~" + r[len(home):])
        candidates.add("$HOME" + r[len(home):])
        candidates.add("${HOME}" + r[len(home):])
    t = norm(text)
    return any(c in t for c in candidates) or "iac-azure-agent/projects" in t


def ask(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "ask",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def check_bash(command, cwd=None, agent=False):
    root = store_root()
    if in_store(command, root):
        raise Deny("direct shell access to the iac-azure-agent store is blocked; use "
                   "tools/config/cli.py and tools/state/cli.py")
    check_powershell(command)
    check_arm_rest(command)
    for args in az_invocations(command):
        check_az(args)
    check_bicep(command)
    check_git(command, cwd, agent)
    check_gh(command, agent)
    norm = command.replace("\\", "/")
    if APPROVAL_CMD.search(norm):
        return "ask", ("iac-azure-agent: this records YOUR approval. Allow it only if you "
                       "approved this exact hash in the conversation.")
    if DEPLOY_CMD.search(norm):
        return "ask", ("iac-azure-agent: this DEPLOYS to Azure. Allow it only if you approved "
                       "this exact what-if change set in the conversation.")
    if PUBLISH_CMD.search(norm):
        return "ask", ("iac-azure-agent: this commits the validated files, pushes a branch to "
                       "GitHub and opens a pull request. Allow it only if you asked for that.")
    if ACCEPT_CMD.search(norm):
        return "ask", ("iac-azure-agent: this accepts a security scanner finding for this "
                       "project. Allow it only if you agreed to accept that finding.")
    if REPO_SWITCH_CMD.search(norm):
        return "ask", ("iac-azure-agent: this sets, switches or clears the configured "
                       "repository for this project. Allow it only if you confirmed it.")
    return None


def decide(data):
    if not isinstance(data, dict):
        raise Deny("hook input is not a JSON object; failing closed")
    tool = data.get("tool_name")
    tin = data.get("tool_input")
    if not isinstance(tool, str):
        raise Deny("hook input has no tool_name; failing closed")
    if tool == "Bash":
        if not isinstance(tin, dict) or not isinstance(tin.get("command"), str):
            raise Deny("Bash hook input has no command string; failing closed")
        return check_bash(tin["command"], data.get("cwd"), is_agent(data))
    if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        if not isinstance(tin, dict):
            raise Deny("%s hook input has no tool_input; failing closed" % tool)
        target = tin.get("file_path") or tin.get("notebook_path")
        if not isinstance(target, str):
            raise Deny("%s hook input has no file path; failing closed" % tool)
        root = os.path.realpath(store_root())
        real = os.path.realpath(os.path.expanduser(target))
        if real == root or real.startswith(root + os.sep):
            raise Deny("writing inside the iac-azure-agent store is blocked; use the CLIs")
        if WORKSPACE_MARK in real.replace("\\", "/"):
            why = workspace_write_ok(real)
            if why:
                raise Deny("writing %s is blocked: %s" % (target, why))
        elif is_agent(data):
            raise Deny("iac-azure-agent writes only under the infrastructure root of the "
                       "working copy (<project>/.iac-azure-agent/workspace/...); %s is outside it"
                       % target)
    return None


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
        verdict = decide(data)
    except Deny as e:
        print("iac-azure-agent guard: %s" % e, file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # noqa: BLE001 - fail closed on anything unexpected
        print("iac-azure-agent guard: internal error (%s); failing closed"
              % type(e).__name__, file=sys.stderr)
        sys.exit(2)
    if verdict:
        ask(verdict[1])
    sys.exit(0)


if __name__ == "__main__":
    main()
