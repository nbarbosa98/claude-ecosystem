# iac-azure-agent

Provision, change, validate and manage Azure infrastructure in plain English. Infrastructure
is defined in Bicep, kept in a GitHub repository you configure per project, previewed
with what-if, and deployed only after an approval bound to the exact target and change set.

**Status: in development, Milestone 2 of 6 (discovery).** It sets up and inspects your
repository, asks the questions a request needs, and produces an architecture proposal for
your approval. It cannot yet write Bicep, publish to GitHub or touch Azure. See
[docs/milestones.md](docs/milestones.md).

| Component | Type | Job |
| --- | --- | --- |
| `iac-azure-agent` | Subagent | Orchestrator: scope, workflow, approvals, operating rules |
| `/iac-setup` | Skill | First run: choose, verify and save the repository; defaults; prerequisites |
| `/iac-repo` | Skill | Show, re-inspect or switch the repository; change defaults |
| `/iac-discover` | Skill | Adaptive question rounds and the architecture proposal |
| `tools/setup/cli.py` | CLI (Python 3, stdlib) | Setup status: config, required permission rules, installed tools |
| `tools/repo/cli.py` | CLI | Read-only repository inspection through your `gh` login |
| `tools/discovery/cli.py` | CLI | Next questions for a request; proposal template and rendering |
| `tools/config/cli.py` | CLI | Per-user, per-project configuration |
| `tools/state/cli.py` | CLI | Workflow state machine and request records |
| `hooks/azure_guard.py` | PreToolUse hook | Blocks direct Azure changes from the shell; asks you before approval commands |

## What works today

- **First-run setup.** The agent asks once which GitHub repository is the source of truth,
  checks it through your `gh` login (access, default branch, whether you can push, existing
  Bicep, parameter files, workflows, READMEs, other infrastructure-as-code), asks which
  directory holds the infrastructure code when that is ambiguous, proposes a layout when
  there is none, and saves the choice only after you confirm. It is not asked again.
- **Adaptive discovery.** Questions come in rounds of at most four, chosen for the request:
  a private storage account in dev gets a handful, a production landing zone gets many
  more. Anything your config, the request or an earlier answer settles is skipped. For a
  simple request, conventional defaults are offered as assumptions instead of questions.
- **Never guessed:** subscription, network address ranges, production sizing, public
  exposure, and what may be deleted. The tools refuse to record these as assumptions.
- **Assumptions review.** Everything not confirmed by you is listed as an assumption and
  must be reviewed by you before an architecture is proposed.
- **Architecture proposal** with fixed sections (objective, scope, resource inventory,
  overview, dependencies, naming/tagging/region, identity, network and security,
  monitoring, cost or why it cannot be estimated, repository changes, deployment strategy,
  risks, unresolved questions, optional diagram), shown with what you confirmed kept apart
  from what is assumed, and approved by hash.
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

Not yet: reading repository file contents, Bicep generation, validation runs, git, pull
requests, Azure inventory, what-if, deployment, verification (Milestones 3-6). The agent says so instead
of pretending.

## Install

```text
/plugin marketplace add nbarbosa98/claude-plugins
/plugin install iac-azure-agent@claude-agents
```

Requires `python3` (3.8 or later) on `PATH`. Without it the hook cannot run and does not
block anything. Repository inspection needs the GitHub CLI (`gh`) signed in with
`gh auth login`; `az` and `bicep` are needed from Milestones 3 and 5. `/iac-setup` reports
which are installed.

### Required permission rules

A plugin cannot ship permission rules, so you add them. They make Claude Code ask you
before any command that records an approval or sets the repository, independently of the
plugin hook. Put this in `~/.claude/settings.json`, or in the project's
`.claude/settings.json` or `.claude/settings.local.json`:

```json
{
  "permissions": {
    "ask": [
      "Bash(*tools/state/cli.py* approve *)",
      "Bash(*tools/state/cli.py* confirm-risk *)",
      "Bash(*tools/config/cli.py* set-repo *)",
      "Bash(*tools/config/cli.py* clear *)"
    ]
  }
}
```

`tools/setup/cli.py status` checks them. **Until they are present the tools refuse to
record a deployment approval.** An `allow` rule that matches these commands (for example
`Bash(python3 *)`) counts as a conflict and is refused too. Do not use `bypassPermissions`
mode with this plugin.

**The shell guard applies to every session where the plugin is enabled**, not only when
the agent is running: direct Azure changes from Bash are blocked while it is installed.
Disable the plugin (`/plugin`) if you need to run such commands yourself.

## Usage

```text
/iac-setup my-org/platform-infra
/iac-discover I need a storage account for application logs in dev, private only.
/iac-repo show
Where is my Azure infrastructure request?
```

The CLIs can be run directly from the project directory:

```text
python3 <plugin>/tools/setup/cli.py status
python3 <plugin>/tools/repo/cli.py inspect my-org/platform-infra
python3 <plugin>/tools/discovery/cli.py next <id>
python3 <plugin>/tools/discovery/cli.py proposal <id>
python3 <plugin>/tools/config/cli.py show
python3 <plugin>/tools/config/cli.py set-repo my-org/platform-infra --default-branch main
python3 <plugin>/tools/config/cli.py set environments dev,test,prod
python3 <plugin>/tools/state/cli.py create --intent "Storage account for logs"
python3 <plugin>/tools/state/cli.py list
python3 <plugin>/tools/state/cli.py show <id>
```

Output is JSON. Exit codes: 0 ok, 1 invalid input, 2 refused by a rule, 3 storage
unavailable (nothing saved), 4 corrupt file (left untouched for inspection), 5 an external
tool or service was missing or failed (nothing was verified).

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
| Approval recorded without the user | Claude Code prompts you before approval commands | Your `ask` rules (required, checked by the tools before deployment approval) and the hook |
| Guessing what must be asked | Must-confirm topics cannot be recorded as assumptions; discovery cannot end without them | Tool code |
| Assumptions passed off as facts | Kept in a separate list; your review is bound to the list's hash | Tool code |
| Thin proposal approved | Required sections checked before APPROVAL | Tool code |
| Repository set or switched without you | Saved only by `set-repo`, which prompts; a switch needs the current repository named | Tool code, `ask` rule, hook |
| A failed check reported as fine | Inspection failures exit 5 and never return `verified` | Tool code |
| Direct Azure changes from the shell | Allowlist of read-only `az`, Azure PowerShell and `bicep` commands | Hook (command text only) |
| Secrets stored | Secret guard on every write | Tool code (pattern-based) |
| Records edited directly | Store paths blocked for Bash/Write/Edit; hashes re-checked | Hook and tool code |
| Prompt injection from repo or tool output | Untrusted-data rule | Prompt only |


## Tools the agent uses

| Tool | Why |
| --- | --- |
| Read, Grep, Glob | Read the project and its infrastructure files |
| Bash | Run the plugin's CLIs (and, from later milestones, read-only `az`/`bicep` commands) |
| Skill | Load `/iac-setup`, `/iac-repo` and `/iac-discover` |

No Write, Edit, web or MCP tools in Milestone 2. Repository inspection makes two read-only
GitHub API calls through `gh` (repository metadata and the file list); it reads no file
contents and writes nothing.

## Tests

From this folder:

```text
python3 -m unittest discover -s tools/tests -v
```

Standard library only; no network, Azure or GitHub access. Each test uses a temporary
store via `IAC_AZURE_AGENT_HOME`. GitHub is replaced by a fake `gh` script that answers
from a fixture. One test (read-only directory) is skipped when run as root, because root
ignores directory permissions.

## Known limitations

- The approval gate cannot prove a human approved: the model runs the CLIs. Claude Code's
  permission prompt is the human check. The tools verify that your `ask` rules exist by
  matching their text against sample commands; that Claude Code prompts on them in every
  permission mode has not been tested in a live session.
- The question catalog is a starting point, not complete Azure knowledge. The agent adds
  questions of its own; those are not enforced by the tools unless marked must-confirm.
- Discovery cannot look at Azure yet (Milestone 5), so "what already exists" comes from you
  and is recorded as your statement or as an unverified assumption.
- Cost figures in a proposal are the model's estimate from stated assumptions, or an
  explanation of why no estimate is possible. No pricing API is called.
- Repository inspection lists file names only. A very large repository is reported as
  truncated. GitHub answers "not found" both for a missing repository and for one your
  login cannot see.
- Records created by 0.1.0 have no profile; an architecture approval made with 0.1.0 is
  invalid under 0.2.0 and must be given again.
- The hook matches command text. Indirection (variables, `eval`, scripts, SDKs) is not seen.
  It also blocks harmless text that mentions a blocked command.
- The secret guard is pattern-based; unrecognisable secrets pass.
- GitHub.com only; GitHub Enterprise Server is not supported.
- Region names are checked for shape only, not against the live Azure region list.
- No file locking; two simultaneous writers to one request can race.
- Project identity is the directory path.

## Changelog

- `0.2.0` - Milestone 2: first-run repository setup with read-only inspection, `/iac-setup`,
  `/iac-repo`, `/iac-discover`, adaptive question catalog and planner, must-confirm topics,
  assumptions review, proposal sections and rendering, required permission rules checked
  before deployment approval, `set-repo` always prompts, agent model `inherit`.
  The 0.1.0 "optional hardening" rules did not match a quoted script path; use the rules above.
- `0.1.0` - Milestone 1: config store, workflow state machine, secret guard, shell guard,
  agent definition, tests, docs.
