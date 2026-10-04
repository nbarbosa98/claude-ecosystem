# iac-azure-agent

Provision, change, validate and manage Azure infrastructure in plain English. Infrastructure
is defined in Bicep, kept in a GitHub repository you configure per project, previewed
with what-if, and deployed only after an approval bound to the exact target and change set.

**Status: in development, Milestone 1 of 6 (foundation).** It cannot yet generate Bicep,
use GitHub or touch Azure. See [docs/milestones.md](docs/milestones.md).

| Component | Type | Job |
| --- | --- | --- |
| `iac-azure-agent` | Subagent | Orchestrator: scope, workflow, approvals, operating rules |
| `tools/config/cli.py` | CLI (Python 3, stdlib) | Per-user, per-project configuration |
| `tools/state/cli.py` | CLI (Python 3, stdlib) | Workflow state machine and request records |
| `hooks/azure_guard.py` | PreToolUse hook | Blocks direct Azure changes from the shell; asks you before approval commands |

## What works today

- **Configuration** per project: GitHub repository (validated, normalised, switching needs
  explicit confirmation), default branch, infrastructure root, region, environment names,
  production environments, naming and tagging conventions, deployment authentication
  method, other non-secret preferences. Survives restarts and plugin updates.
- **Workflow records** that persist across sessions:
  `REQUEST -> DISCOVERY -> ARCHITECTURE -> APPROVAL -> IMPLEMENTATION -> VALIDATION -> GIT_REVIEW -> DEPLOYMENT_APPROVAL -> DEPLOYMENT -> VERIFICATION`.
  Each record keeps intent, confirmed requirements, open questions, assumptions (separate
  from requirements), the approved architecture, target, files, check results, git
  details, plan, approvals, deployment result, verification, and a full history.
  Approvals are bound to a hash of the content; changing it invalidates the approval.
  High-risk plans need a second confirmation phrase. Interrupted steps are detected on resume.
- **Secret guard** on every write: secret-shaped keys and values are refused.
- **Shell guard**: `az ... create/delete/update`, `az role assignment create`,
  `az ad sp create-for-rbac`, mutating `az rest`, `New-AzResourceGroupDeployment` and
  similar are blocked; `az account show`, `what-if`, `validate`, `bicep build/lint` pass.

Not yet: discovery conversation design, Bicep generation, validation runs, git, pull
requests, what-if, deployment, verification (Milestones 2-6). The agent says so instead
of pretending.

## Install

```text
/plugin marketplace add nbarbosa98/claude-plugins
/plugin install iac-azure-agent@claude-agents
```

Requires `python3` (3.8 or later) on `PATH`. Without it the hook cannot run and does not
block anything.

**The shell guard applies to every session where the plugin is enabled**, not only when
the agent is running: direct Azure changes from Bash are blocked while it is installed.
Disable the plugin (`/plugin`) if you need to run such commands yourself.

## Usage

```text
Use iac-azure-agent to set this project's infrastructure repo to my-org/platform-infra.
I need a storage account for application logs in dev, private only.
Where is my Azure infrastructure request?
```

The CLIs can be run directly from the project directory:

```text
python3 <plugin>/tools/config/cli.py show
python3 <plugin>/tools/config/cli.py set-repo my-org/platform-infra --default-branch main
python3 <plugin>/tools/config/cli.py set environments dev,test,prod
python3 <plugin>/tools/state/cli.py create --intent "Storage account for logs"
python3 <plugin>/tools/state/cli.py list
python3 <plugin>/tools/state/cli.py show <id>
```

Output is JSON. Exit codes: 0 ok, 1 invalid input, 2 refused by a rule, 3 storage
unavailable (nothing saved), 4 corrupt file (left untouched for inspection).

## Configuration and state location

```text
$IAC_AZURE_AGENT_HOME                 if set (absolute path)
%APPDATA%\iac-azure-agent              Windows
$XDG_CONFIG_HOME/iac-azure-agent       if XDG_CONFIG_HOME is set
~/.config/iac-azure-agent              otherwise
  projects/<project-name>-<hash>/config.json
  projects/<project-name>-<hash>/requests/<request-id>.json
```

A project is the nearest directory with `.git` above where you run Claude Code. Moving
the project directory starts a fresh config. `config/cli.py path` prints the location.
The store is kept on plugin update and uninstall; to reset, delete the directory yourself (the hook blocks Claude from touching it).
Never edit these files by hand: approvals are hash-checked and a hand edit invalidates them.

## Safety model

Details: [docs/security-model.md](docs/security-model.md).

| Risk | Control | Enforced by |
| --- | --- | --- |
| Moving on without approval | State tool refuses; approval bound to a content hash | Tool code |
| Approval reused after a change | Hash recomputed on every change; workflow returns to the approval state | Tool code |
| High-risk deployment approved casually | Separate exact phrase per flag set | Tool code |
| Approval recorded without the user | Hook makes Claude Code prompt you before approval commands | Hook (`ask`); unverified in bypass/auto mode |
| Direct Azure changes from the shell | Allowlist of read-only `az`, Azure PowerShell and `bicep` commands | Hook (command text only) |
| Secrets stored | Secret guard on every write | Tool code (pattern-based) |
| Records edited directly | Store paths blocked for Bash/Write/Edit; hashes re-checked | Hook and tool code |
| Prompt injection from repo or tool output | Untrusted-data rule | Prompt only |

### Optional hardening

A plugin cannot ship permission rules. To add a hard prompt that does not depend on the
hook, put this in `~/.claude/settings.json`:

```json
{
  "permissions": {
    "ask": [
      "Bash(*tools/state/cli.py approve*)",
      "Bash(*tools/state/cli.py confirm-risk*)",
      "Bash(*tools/config/cli.py set-repo*)",
      "Bash(*tools/config/cli.py clear*)"
    ]
  }
}
```

Do not use `bypassPermissions` mode with this plugin.

## Tools the agent uses

| Tool | Why |
| --- | --- |
| Read, Grep, Glob | Read the project and its infrastructure files |
| Bash | Run the two CLIs (and, from later milestones, read-only `az`/`bicep` commands) |

No Write, Edit, web or MCP tools in Milestone 1.

## Tests

From this folder:

```text
python3 -m unittest discover -s tools/tests -v
```

Standard library only; no network, Azure or GitHub access. Each test uses a temporary
store via `IAC_AZURE_AGENT_HOME`. One test (read-only directory) is skipped when run as
root, because root ignores directory permissions.

## Known limitations

- The approval gate cannot prove a human approved: the model runs the CLIs. The hook's
  permission prompt is the human check, and its behaviour in bypass and auto modes is
  unverified.
- The hook matches command text. Indirection (variables, `eval`, scripts, SDKs) is not seen.
  It also blocks harmless text that mentions a blocked command.
- The secret guard is pattern-based; unrecognisable secrets pass.
- GitHub.com only; GitHub Enterprise Server is not supported.
- Region names are checked for shape only, not against the live Azure region list.
- No file locking; two simultaneous writers to one request can race.
- Project identity is the directory path.

## Changelog

- `0.1.0` - Milestone 1: config store, workflow state machine, secret guard, shell guard,
  agent definition, tests, docs.
