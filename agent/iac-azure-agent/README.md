# iac-azure-agent

Provision, change, validate and manage Azure infrastructure in plain English. Infrastructure
is defined in Bicep, kept in a GitHub repository you configure per project, previewed
with what-if, and deployed only after an approval bound to the exact target and change set.

**Status: in development, Milestone 5 of 6 (Azure integration).** It sets up and inspects
your repository, asks the questions a request needs, produces an architecture proposal
for your approval, writes the Bicep in a local working copy, validates it, publishes it to
a branch and pull request, previews it with Azure what-if, deploys it after you approve
that exact change set, and verifies the result. The CI workflow is still to come. See
[docs/milestones.md](docs/milestones.md).

| Component | Type | Job |
| --- | --- | --- |
| `iac-azure-agent` | Subagent | Orchestrator: scope, workflow, approvals, operating rules |
| `/iac-setup` | Skill | First run: choose, verify and save the repository; defaults; prerequisites |
| `/iac-repo` | Skill | Show, re-inspect or switch the repository; change defaults |
| `/iac-discover` | Skill | Adaptive question rounds and the architecture proposal |
| `/iac-implement` | Skill | Write the approved design as modular Bicep and validate it |
| `/iac-publish` | Skill (you invoke it) | Commit, push, confirm the remote, open a pull request |
| `/iac-deploy` | Skill (you invoke it) | Azure context check, what-if, approval, deployment, verification |
| `tools/deploy/cli.py` | CLI | `context`, `plan`, `deploy`, `status`, `verify`. Only `deploy` changes Azure |
| `tools/github/cli.py` | CLI | Publish preview, publish, and re-verification against GitHub |
| `tools/workspace/cli.py` | CLI | Working copy: clone, sync, work branch, Bicep inventory, record changed files |
| `tools/validate/cli.py` | CLI | Structure, Bicep build and lint, secret scan, Checkov security scan |
| `tools/setup/cli.py` | CLI (Python 3, stdlib) | Setup status: config, required permission rules, installed tools |
| `tools/repo/cli.py` | CLI | Read-only repository inspection through your `gh` login |
| `tools/discovery/cli.py` | CLI | Next questions for a request; proposal template and rendering |
| `tools/config/cli.py` | CLI | Per-user, per-project configuration |
| `tools/state/cli.py` | CLI | Workflow state machine and request records |
| `hooks/azure_guard.py` | PreToolUse hook | Blocks direct Azure changes from the shell; limits writes to the infrastructure directory of the working copy; blocks git changes there and, for the agent, `gh` commands that write; asks you before approval and publish commands |

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
- **Working copy under your project.** The configured repository is cloned to
  `<project>/.iac-azure-agent/workspace/<owner>--<name>/` (ignored by your project's own
  git). Each request gets a local branch `iac/<request-id>`. Nothing is committed or
  pushed yet. Uncommitted work and diverged branches are reported, never discarded.
- **Inspection before writing.** The existing Bicep is inventoried (entry points, modules,
  parameter files, resource types and API versions, `existing` references) so new code
  follows what is there and does not duplicate it.
- **Bicep generation** to written standards
  ([bicep-standards.md](skills/iac-implement/references/bicep-standards.md)): modules per
  component, per-environment `.bicepparam` files, typed and described parameters,
  `@secure()` where needed, explicit security settings, managed identities, least-privilege
  RBAC, diagnostics, and a README for every deployment.
- **Write limits.** The agent can write only under the infrastructure directory of the
  working copy. Changed files are taken from git, and a change anywhere else is refused.
- **Validation** with established tools: `bicep build`, `bicep build-params`, `bicep lint`,
  Checkov, plus a structure check, a Bicep-only check and a secret scan. Each result is
  passed, failed, warning, skipped or unavailable; a missing tool is `unavailable`, never
  a pass. Results are bound to a hash of the files checked. Moving on with anything
  skipped or unavailable needs your explicit acceptance, which is recorded.
- **Publishing** (`/iac-publish`), always to the request's own branch `iac/<request-id>`
  and a pull request into the default branch. Never the default branch, never a force
  push, never a merge: the pull request is yours to review and merge.
  - Refused unless validation ran on exactly the files being committed.
  - Only the infrastructure directory is staged.
  - After the push the remote branch is read back and compared with the commit; the
    pull request is fetched after it is created. Only what was read back is reported as done.
  - A failure part-way (for example pushed, but the pull request failed) is reported as
    exactly that, and running publish again continues without committing twice.
  - The pull request body is generated from the request: files, validation results,
    confirmed requirements and assumptions.
- **Azure planning** (`/iac-deploy`). The tool confirms `az` is signed in to the tenant and
  subscription you confirmed, then runs Azure's own deployment validation and what-if on
  the published commit. Both are read-only. The result becomes the change set: every
  resource to be created, modified or deleted. Risk flags (deletion, stateful replacement,
  data loss, public exposure, broad RBAC, production target) are derived from it, and
  anything what-if could not evaluate is listed as uncertain.
- **Deployment only on approval of that change set.** One command changes Azure, and it
  refuses unless the approval is valid and, at that moment, `az` is signed in to the
  approved target, the files are the published ones, the compiled parameters are
  unchanged, and a fresh what-if gives exactly the approved change set. It runs once, in
  incremental mode, and is never retried.
- **Honest outcomes.** A failed deployment is recorded as failed or partial, with the
  resources Azure created before the failure. Nothing is rolled back or deleted. An
  interrupted run is resolved by reading the deployment's real state from Azure.
- **Verification.** Each planned resource is read back from Azure, VM power state is
  checked, and resources that were not in the plan are reported. Reports keep three lists
  apart: planned, reported by Azure, independently verified.
- **Failures explained.** Sign-in, authorization, policy, quota, SKU availability,
  template and conflict errors are told apart, each with what can be done. Only read
  operations are retried, and only for temporary errors.
- **Accepted findings.** A scanner finding you decide to live with is recorded in the
  project config with its reason (`accepted_findings.<check id>`). It is then reported as
  accepted with that reason: a warning, never a pass.
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

Not yet: the GitHub Actions validation workflow for your infrastructure repository
(Milestone 6). Removing a deployment is not automated: deleting is yours to do. The agent says so instead
of pretending.

## Install

```text
/plugin marketplace add nbarbosa98/claude-plugins
/plugin install iac-azure-agent@claude-agents
```

Requires `python3` (3.8 or later) on `PATH`. Without it the hook cannot run and does not
block anything. `/iac-setup` reports which of these are installed:

| Tool | Needed for | Install |
| --- | --- | --- |
| `gh`, signed in (`gh auth login`, then `gh auth setup-git`) | Repository inspection, cloning, pushing, pull requests | https://cli.github.com |
| `git` | The working copy | your package manager |
| `bicep` | Build and lint | https://github.com/Azure/bicep/releases (standalone binary) |
| `checkov` | Static security analysis | `pipx install checkov` |
| `az`, signed in by you (`az login`) | Azure validation, what-if, deployment, verification | https://learn.microsoft.com/cli/azure/install-azure-cli |

Without `bicep` or `checkov` the matching checks are reported as `unavailable`.

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
      "Bash(*tools/config/cli.py* clear *)",
      "Bash(*tools/config/cli.py* set accepted_findings.*)",
      "Bash(*tools/github/cli.py* publish *)",
      "Bash(*tools/deploy/cli.py* deploy *)"
    ]
  }
}
```

`tools/setup/cli.py status` checks them. **Until they are present the tools refuse to
publish, to record a deployment approval and to deploy.** If you added rules for an
earlier version, add the ones you are missing; there are seven. An `allow` rule that matches these commands (for example
`Bash(python3 *)`) counts as a conflict and is refused too. Do not use `bypassPermissions`
mode with this plugin.

**The shell guard applies to every session where the plugin is enabled**, not only when
the agent is running: direct Azure changes from Bash are blocked while it is installed. Publishing tests push to a local bare repository and use a fake `gh` that
keeps pull requests in a file.
Disable the plugin (`/plugin`) if you need to run such commands yourself.

## Usage

```text
/iac-setup my-org/platform-infra
/iac-discover I need a storage account for application logs in dev, private only.
/iac-implement <request id>
/iac-publish <request id>
/iac-deploy <request id>
/iac-repo show
Where is my Azure infrastructure request?
```

The CLIs can be run directly from the project directory:

```text
python3 <plugin>/tools/setup/cli.py status
python3 <plugin>/tools/repo/cli.py inspect my-org/platform-infra
python3 <plugin>/tools/discovery/cli.py next <id>
python3 <plugin>/tools/discovery/cli.py proposal <id>
python3 <plugin>/tools/workspace/cli.py status
python3 <plugin>/tools/workspace/cli.py inventory
python3 <plugin>/tools/validate/cli.py run            # checks only, records nothing
python3 <plugin>/tools/validate/cli.py run <id>       # records results on the request
python3 <plugin>/tools/deploy/cli.py context
python3 <plugin>/tools/deploy/cli.py status <id>
python3 <plugin>/tools/github/cli.py preview <id>
python3 <plugin>/tools/github/cli.py verify <id>
python3 <plugin>/tools/config/cli.py show
python3 <plugin>/tools/config/cli.py set-repo my-org/platform-infra --default-branch main
python3 <plugin>/tools/config/cli.py set environments dev,test,prod
python3 <plugin>/tools/state/cli.py create --intent "Storage account for logs"
python3 <plugin>/tools/state/cli.py list
python3 <plugin>/tools/state/cli.py show <id>
```

Output is JSON. Exit codes: 0 ok, 1 invalid input, 2 refused by a rule, 3 storage
unavailable (nothing saved), 4 corrupt file (left untouched for inspection), 5 an external
tool or service was missing or failed (nothing was verified; for a deployment, read the
message: it says what Azure did before the failure).

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
| Files changed outside the infrastructure directory | Write and Edit limited to it inside the working copy; changed files taken from git and refused if any is outside | Hook and tool code |
| Other people's work overwritten | No reset, clean or forced checkout; dirty or diverged working copies are refused | Tool code |
| Code published or deployed unvalidated | Results bound to a hash of the files checked; publish refuses when the hash differs, the verdict is failed, or unaccepted checks are missing | Tool code |
| Deployment without approval | The deploy tool re-checks the approval itself; a record forced into the DEPLOYMENT state still cannot deploy | Tool code |
| Deploying something other than what you approved | Approval covers target, change set, risk flags, commit, files and compiled inputs; a fresh what-if must match before the create | Tool code |
| Deploying to the wrong place | `az` must be signed in to exactly the approved tenant and subscription; the tool never switches | Tool code |
| High-risk change approved casually | Flags derived from what-if; a separate typed phrase is needed | Tool code |
| A failed deployment "fixed" destructively | No delete, rollback or retry exists in the tool; failures are classified and handed to you | Tool code and prompt |
| Success reported without evidence | Status comes from Azure's deployment record; "done" needs verification that reads each resource back | Tool code |
| Publishing without you | `/iac-publish` is user-invoked; the publish command prompts | Your `ask` rule (required, checked by the tool) and the hook |
| Default branch changed, history rewritten, pull request merged | The tool pushes one refspec, `iac/<id>`, without force, and has no merge operation; for the agent, `gh` commands that write are blocked | Tool code and hook |
| A push reported as done when it was not | Remote head read back with `git ls-remote` and compared; pull request fetched after creation | Tool code |
| Someone else's commits overwritten | A rejected push is reported and never forced | Tool code |
| Secrets in code | Secret scan over every file under the infrastructure directory; values never echoed | Tool code (pattern-based) |
| Insecure configuration | Bicep linter security rules and Checkov; suppressions are reported as warnings, never as passes | Tool code |
| Commit or push outside the workflow | git changes in the working copy are blocked for every session; for the agent, everywhere | Hook (command text only) |
| A finding silently ignored | Accepting one needs a reason, prompts you, and shows as a warning with the reason in every later run | Tool code, `ask` rule, hook |
| Direct Azure changes from the shell | Allowlist of read-only `az`, Azure PowerShell and `bicep` commands | Hook (command text only) |
| Secrets stored | Secret guard on every write | Tool code (pattern-based) |
| Records edited directly | Store paths blocked for Bash/Write/Edit; hashes re-checked | Hook and tool code |
| Prompt injection from repo or tool output | Untrusted-data rule | Prompt only |


## Tools the agent uses

| Tool | Why |
| --- | --- |
| Read, Grep, Glob | Read the project and its infrastructure files |
| Bash | Run the plugin's CLIs and read-only `git`, `bicep` and `az` commands |
| Skill | Load `/iac-setup`, `/iac-repo`, `/iac-discover` and `/iac-implement` |
| Write, Edit | Bicep and its documentation, only under the infrastructure directory of the working copy (enforced by the hook) |

No web or MCP tools. Azure use: `az account show`; `az deployment sub|group validate`,
`what-if`, `create` (only through the deploy tool), `show` and `operation list`;
`az resource show|list`, `az group show`, `az vm get-instance-view`. Other network use: two read-only GitHub API calls through `gh` for
inspection; `git clone`, `git fetch`, `git push` (one branch) and `git ls-remote` for the
working copy; `gh pr list`, `gh pr create` and `gh pr view`; `bicep` restoring public
registry modules if the code uses them.

## Tests

From this folder:

```text
python3 -m unittest discover -s tools/tests -v
```

Standard library only; no network, Azure or GitHub access. Each test uses a temporary
store via `IAC_AZURE_AGENT_HOME`. GitHub is replaced by a fake `gh` script and by local
bare repositories reached through git's `insteadOf`; `bicep` and `checkov` are fake
scripts. Two tests run the real Bicep CLI and Checkov and are skipped when those are not
installed. One test (read-only directory) is skipped when run as root, because root
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
- The approval gate cannot prove a human approved, as before; for deployment the
  permission prompt on the deploy command is the human check.
- What-if is Azure's prediction. It can miss changes (for example resources Azure creates
  implicitly, such as a VM's disk) and it does not predict failures such as capacity.
- Risk flags are derived by rules over the what-if result. They catch the listed cases;
  an unusual exposure or privilege change may not be flagged. Read the change set.
- Code is deployed from the request's published branch. It does not have to be merged
  first (see docs/decisions.md ADR-031).
- There is no rollback and no removal of resources. Cleaning up is a deletion you perform.
- Subscription-scope and resource-group deployments are supported; management-group and
  tenant scope are not.
- Tested against the real services once: one subscription-scope deployment of a small VM
  (2026-10-05). Everything else is tested against a fake `az`.
- Publishing always opens a pull request. Direct commits to a development branch are not
  supported.
- Branch protection, required reviews and CI on your repository are yours to configure;
  the agent neither reads nor changes them. If protection rejects the push, that is
  reported.
- A commit is made with your git identity. No co-author line is added.
- After a pull request is merged and its branch deleted, `verify` reports the branch as
  gone; that is expected.
- Write limits cover the Write and Edit tools. A file written by a shell command is not
  blocked at the time; it is caught when changed files are recorded, which refuses
  anything outside the infrastructure directory.
- The documentation for a deployment lives under the infrastructure directory
  (`<infra_root>/README.md`). The repository's root README and `docs/` are outside the
  write limit, so the agent does not update them.
- The Bicep inventory is a line scan for declarations, not a parser. `bicep build` is the
  authority.
- Checkov runs without a platform key, so its findings carry no severity; every finding
  fails the check until it is fixed or you agree to suppress it.
- API versions are checked by the Bicep linter (`use-recent-api-versions`) when the
  recommended `bicepconfig.json` is in place; nothing is checked against live Azure yet.
- Validation proves the files compile and pass static checks. It does not prove a
  deployment will succeed: Azure-side validation and what-if are Milestone 5.
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

- `0.5.0` - Milestone 5: `/iac-deploy` and `tools/deploy/cli.py` (context, plan, deploy,
  status, verify); risk flags from what-if; classified Azure failures; one more required
  permission rule (deploy). Fixes from the first real run: identifiers removed from pull
  request text and refused in commit messages; a VM with its network counts as a simple
  request and topics with a conventional default are offered as assumptions; defaults no
  longer add a Key Vault or a workspace unasked; accepted findings are listed in the pull
  request.
- `0.4.0` - Milestone 4: `/iac-publish` and `tools/github/cli.py` (preview, publish,
  verify) with the remote read back; pull request body generated from the request;
  accepted scanner findings with reasons; two more required permission rules; the agent's
  `gh` use limited to reads.
- `0.3.0` - Milestone 3: working copy under the project, Bicep inventory, `/iac-implement`
  and the Bicep standards reference, changed files recorded from git, validation pipeline
  (structure, Bicep-only, secret scan, `bicep build`/`build-params`/`lint`, Checkov) bound
  to a file hash, hook write limits and git guard, agent gains Write and Edit.
- `0.2.0` - Milestone 2: first-run repository setup with read-only inspection, `/iac-setup`,
  `/iac-repo`, `/iac-discover`, adaptive question catalog and planner, must-confirm topics,
  assumptions review, proposal sections and rendering, required permission rules checked
  before deployment approval, `set-repo` always prompts, agent model `inherit`.
  The 0.1.0 "optional hardening" rules did not match a quoted script path; use the rules above.
- `0.1.0` - Milestone 1: config store, workflow state machine, secret guard, shell guard,
  agent definition, tests, docs.
