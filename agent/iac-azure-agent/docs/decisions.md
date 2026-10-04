# Decision log

ADR-style. Each entry: decision, alternatives, trade-off, source, status.
Status values: `ACCEPTED` (owner decided), `DEFAULT` (chosen during the build, owner has
not reviewed it yet, can be revisited), `PROPOSED` (needs an owner decision), `OPEN`.
`UNVERIFIED` marks a claim that could not be checked against an official source.

Docs checked on 2026-10-04:
[plugins reference](https://code.claude.com/docs/en/plugins-reference),
[plugin components](https://code.claude.com/docs/en/plugins/components),
[hooks](https://code.claude.com/docs/en/hooks),
[permissions](https://code.claude.com/docs/en/permissions).

---

## ADR-001 Packaging: installable plugin

- **Decision:** `agent/iac-azure-agent/` is an installable plugin, registered in
  `.claude-plugin/marketplace.json` and the root README catalog.
- **Alternatives:** a project opened as its own root with `.claude/settings.json`
  permission rules (the `intune-rmd-scr-agent` model, its ADR-001); a separate repository.
- **Trade-off:** a plugin cannot ship permission rules. Fact: "Only two keys take effect
  from a plugin, `agent` and `subagentStatusLine`; every other key is dropped"
  (plugin components, "Default settings"). Plugin agents also ignore `permissionMode`,
  `hooks` and `mcpServers` in their frontmatter (plugin components, "Frontmatter fields in
  plugin agents"). So the approval gate cannot rest on `ask`/`deny` rules; see ADR-002.
  In exchange the agent installs and updates like every other plugin in this catalog.
- **Status:** ACCEPTED (owner decision before Milestone 1).

## ADR-002 Approval gate: enforced in the tools, hook as second layer

- **Decision:** three layers, in order of strength.
  1. The deterministic tools (`tools/state/`) refuse every move past an approval state
     without a valid approval record whose hash matches the current content (ADR-008).
     Later milestones will route every Azure and GitHub write through tools that check
     the same record.
  2. A plugin `PreToolUse` hook (`hooks/azure_guard.py`) blocks direct Azure changes from
     the shell, so writes cannot skip layer 1 by calling `az` directly (ADR-011).
  3. The same hook answers approval commands with `permissionDecision: "ask"`, so Claude
     Code shows the user a permission prompt for them. Fact: a PreToolUse hook "can deny
     the tool call, force a prompt, or skip the prompt" (permissions, "Extend permissions
     with hooks").
- **Alternatives:** permission `ask` rules (not shippable by a plugin, ADR-001); prompt-only
  rules (not a control).
- **Trade-off:** none of the layers is a security boundary against a model that sets out
  to bypass them: the model runs the CLIs and could run them with arguments the user did
  not approve, and hooks match command text. Fact: a Bash rule "covers the invocation
  Claude usually produces and isn't a security boundary" (permissions, "Bash rules").
  Layer 3 puts a human prompt in front of the approval command, which is the strongest
  gate a plugin can ship. Users who want a hard rule can add `ask` rules in their own
  settings (README, "Optional hardening").
- **UNVERIFIED:** whether the hook's `ask` decision still prompts in `bypassPermissions`
  or auto mode. Not documented on the pages above. Treat it as not guaranteed.
- **Fact:** an installed mod handling `tool.check` can override a PreToolUse block unless
  the hook is in managed settings (permissions, "Extend permissions with hooks").
- **Status:** ACCEPTED (layering required by the owner); layer 3 is DEFAULT.

## ADR-003 Where config and state live

- **Decision:** a per-user store outside the plugin: `$IAC_AZURE_AGENT_HOME` if set
  (absolute path; used by tests), else `%APPDATA%\iac-azure-agent` on Windows, else
  `$XDG_CONFIG_HOME/iac-azure-agent`, else `~/.config/iac-azure-agent`.
  Layout: `projects/<project-key>/config.json` and `projects/<project-key>/requests/<id>.json`.
- **Alternatives:**
  - `${CLAUDE_PLUGIN_ROOT}`: rejected. Fact: it "changes when the plugin updates, so don't
    write state there" (plugins reference, "Environment variables").
  - `${CLAUDE_PLUGIN_DATA}` (`~/.claude/plugins/data/<id>/`): fact: "kept across plugin
    updates", but "Claude Code deletes the `${CLAUDE_PLUGIN_DATA}` directory when you
    uninstall the plugin from the last place it's installed", and the variables "aren't
    present in the environment of commands Claude runs through the Bash tool"
    (plugins reference). The CLIs run through Bash, so they would have to be told the
    path, and an uninstall would delete workflow records and approvals. Rejected.
  - The project repository: rejected; config is per user and state can hold target
    identifiers that should not be committed.
- **Trade-off:** the store survives plugin updates and uninstall; the user deletes it by
  hand (README). It is not synced between machines.
- **Status:** DEFAULT. Owner may prefer `CLAUDE_PLUGIN_DATA` despite the uninstall behaviour.

## ADR-004 Project identity

- **Decision:** the project is the nearest ancestor of the working directory (or
  `--project-dir`) that contains `.git`, else the directory itself. Its key is
  `<basename>-<first 16 hex of sha256(realpath)>`.
- **Alternatives:** the git remote URL (two clones would share config; needs git or a
  config parser; a repo without a remote has no key); `CLAUDE_PROJECT_DIR` (UNVERIFIED
  whether it is set in the Bash tool environment; not relied on).
- **Trade-off:** moving or renaming the project directory starts a fresh config; the old
  one stays on disk. Two worktrees of one repo are two projects.
- **Status:** DEFAULT (owner decision requested before Milestone 2).

## ADR-005 Repository identifiers

- **Decision:** github.com only. Accepted: `owner/name`, `https://github.com/owner/name[.git]`,
  `git@github.com:owner/name[.git]`, `ssh://git@github.com/owner/name[.git]`. Stored
  lower-case as `owner`, `name`, `slug` (`owner/name`) and `url`
  (`https://github.com/owner/name`). URLs with credentials, `http://` and other hosts are
  rejected. Owner: 1-39 characters, letters, digits and single hyphens; name: up to 100
  of letters, digits, `.`, `_`, `-`.
- **Alternatives:** keep the user's casing (two spellings would compare unequal); support
  GitHub Enterprise Server hosts (not requested).
- **Trade-off:** displayed names are lower-case. GHES needs a schema change later.
- **Source:** GitHub name rules are applied as commonly documented; the exact limits are
  UNVERIFIED against a GitHub reference in this build.
- **Status:** DEFAULT.

## ADR-006 Switching the configured repository

- **Decision:** `set-repo NEW` on a project that already has a different repository is
  refused unless `--confirm-switch-from CURRENT` names the current repository (any accepted
  spelling). The hook also asks the user before that command runs (ADR-002 layer 3).
  The old repository's default branch is not carried over. `clear` needs
  `--confirm-project` equal to the resolved project root.
- **Alternatives:** a yes/no flag (satisfiable by accident); a typed random code (needs a
  second round trip and state).
- **Trade-off:** the refusal message names the flag and the current repository, so the
  model can satisfy it deliberately; the user prompt from the hook is what stops that.
- **Status:** DEFAULT.

## ADR-007 Workflow state machine

- **Decision:** ten states, forward one at a time, with guards:
  DISCOVERY needs at least one confirmed requirement and no open questions (a question can
  be answered or deferred as a recorded assumption); ARCHITECTURE needs a proposal;
  APPROVAL needs a valid architecture approval; IMPLEMENTATION needs recorded files;
  VALIDATION needs results with no failure (ADR-013); GIT_REVIEW needs branch and commit;
  DEPLOYMENT_APPROVAL needs target, plan, valid deployment approval and, with risk flags,
  a valid high-risk confirmation; DEPLOYMENT needs a recorded `succeeded` result;
  `complete` needs passing verification. Back moves go to any earlier state from DISCOVERY
  on, with a reason; validation, deployment and verification results that rework makes
  stale are marked stale, not deleted. `status` is `active`, `completed` or `cancelled`;
  terminal records accept no changes.
  Steps: `step-start`/`step-end`. An in-progress, failed or interrupted step in the current
  state blocks moves. `resume` marks an in-progress step as `interrupted`, because the
  process that ran it is gone and its outcome is unknown.
- **Alternatives:** free transitions with warnings; a separate record per attempt.
- **Trade-off:** strict guards may feel slow in conversation; they are the point.
- **Status:** DEFAULT (shape required by the owner; guard details chosen here).

## ADR-008 Approval binding

- **Decision:** approvals are bound to sha256 over canonical JSON (sorted keys, no
  whitespace, ASCII). The tool computes the hash; the caller only echoes the 12-character
  short hash that `show` printed, which must match the current content.
  - Architecture approval covers `{architecture, requirements, active assumptions}`.
  - Deployment approval covers `{target, change_set, risk_flags, git_commit, files,
    architecture_hash}`.
  After every change (and on every load before a move) the tool recomputes the hashes. A
  mismatch marks the approval invalid and, if the workflow is past the matching approval
  state, returns it there. Any change to approved content counts as material: there is no
  "cosmetic" exemption, because a tool cannot judge that safely.
- **Alternatives:** caller-supplied hashes (forgeable by mistake); semantic diffing.
- **Trade-off:** a reworded requirement forces a new approval.
- **Status:** ACCEPTED (binding required by the owner; hash content chosen here, DEFAULT).

## ADR-009 High-risk confirmation

- **Decision:** risk flags: `deletion`, `stateful_replacement`, `data_loss`,
  `public_exposure`, `broad_rbac`, `production_target`. `production_target` is added
  automatically when the target environment is `prod`, `production`, `prd` or listed in
  config `production_environments`. With any flag, leaving DEPLOYMENT_APPROVAL needs a
  second record created by `confirm-risk` with the exact phrase
  `ACCEPT-RISK <short-hash> <flags,comma,separated>`, bound to the same hash and flag list.
- **Alternatives:** a `--force` flag; typing the resource group name.
- **Trade-off:** the flags themselves come from the plan; detecting them from what-if
  output is Milestone 5. Until then a plan with no flags set has none.
- **Status:** DEFAULT.

## ADR-010 Secret guard

- **Decision:** `tools/lib/secret_guard.py` runs on every config and record write. It
  rejects secret-shaped key names (password, secret, token, client secret, private key,
  connection string, account key, credential, SAS, ...) and values (PEM blocks, GitHub
  tokens, JWT-shaped strings, 88-character storage account keys, connection strings with
  `AccountKey`/`SharedAccessKey`/`Password`, SAS `sig=`, URLs with credentials, and the
  current Entra client secret shape). GUIDs, resource IDs, git SHAs and sha256 hex pass.
  Error messages name the location, never the value.
- **Limits:** pattern matching only. Not caught: secrets with no recognisable shape
  (a plain password in a field called `note`), base64 of a secret, secrets split across
  fields. The Entra client secret pattern is a heuristic (UNVERIFIED: Microsoft does not
  document the format). Not a substitute for gitleaks or GitHub push protection.
- **Status:** DEFAULT.

## ADR-011 Shell guard hook

- **Decision:** one `PreToolUse` hook, matcher `Bash|Write|Edit|NotebookEdit`, in
  `hooks/hooks.json`. Azure CLI: allowlist (read verbs, what-if, validate, local commands
  such as `login`, `account set`, `bicep build/lint`); everything else is denied, including
  unknown commands. `az rest` only with explicit GET/HEAD. Reads that print secrets are
  denied. Azure PowerShell: read verbs only. Standalone `bicep publish` denied. Raw calls to
  ARM hosts from anything but `az` denied. Encoded PowerShell denied. Shell or file-tool
  access to the store denied. Approval commands get `ask` (ADR-002).
  Fail closed: any parse failure, unexpected payload or exception exits 2.
- **Facts (hooks reference):** exit 2 blocks; "other non-zero exit codes are non-blocking";
  a timed-out PreToolUse command hook "doesn't block the tool call". So the hook catches
  every exception itself. If `python3` is missing the hook cannot run and does not block.
- **Scope (inference):** plugin hooks are declared for the plugin, not for an agent, and
  agent-file `hooks` are ignored for plugin agents (plugin components page). The docs
  describe no way to scope a plugin hook to one agent, so the guard is expected to run for
  every Bash call in any session where the plugin is enabled. Not tested in a live session
  in this build.
- **Alternatives:** a deny-list (misses new commands); scoping by `agent_type` in the hook
  input (UNVERIFIED that the field is present for plugin subagents).
- **Trade-off:** false positives: text that merely mentions `az group delete` (in a
  commit message, say) is blocked. Users who deploy Azure by hand in the same sessions
  will be blocked too (owner decision below).
- **Status:** DEFAULT.

## ADR-012 Agent tool budget and model

- **Decision:** `tools: Read, Grep, Glob, Bash`, `model: opus`. No Write or Edit in
  Milestone 1: there is nothing for the agent to write except through the CLIs. Bash is
  needed to run the CLIs. Opus because architecture reasoning is the core task.
- **Alternatives:** `inherit`; `sonnet` for cost.
- **Trade-off:** Bash is broad; the hook narrows its Azure surface only.
  Milestone 3 will need Write/Edit for Bicep.
- **Status:** DEFAULT.

## ADR-013 Check results

- **Decision:** each validation or verification check is `passed`, `failed`, `warning`,
  `skipped` or `unavailable`. The summary verdict is `failed` (any failure), `incomplete`
  (any skipped/unavailable), `passed_with_warnings`, `passed`, or `none`. `incomplete`
  blocks the move unless the caller passes `--accept-incomplete`, which records the
  checks accepted as missing. Re-recording a check marks the earlier result stale.
- **Status:** DEFAULT.

## ADR-014 Storage durability

- **Decision:** atomic writes (temp file in the same directory, fsync, chmod 0600,
  `os.replace`, directory fsync), directories 0700, read-back comparison before reporting
  success, schema version on every document, corrupt or newer-version files refused and
  left untouched. Records carry a revision; a save fails if the revision on disk changed
  since load. No file locking (portable stdlib locking is not available on all
  platforms); two writers at the same instant can still race between the check and the
  replace.
- **Status:** DEFAULT.

## ADR-015 Adaptive discovery: a topic catalog and a planner, not a questionnaire

- **Decision:** discovery topics are data (`tools/discovery/catalog.py`): round, resource
  categories, request kinds, a condition (production, non-production, integrates existing,
  destructive), whether the config answers it, whether it is optional, a conventional
  default. `planner.py` returns the next batch: at most four topics from the lowest
  unfinished round, must-confirm first, skipping what the config, the request or an
  earlier answer settles. Round 1 is the request profile (kind, categories, environments,
  restatement); nothing is asked before it exists. A "simple" request (new, one category,
  not production, nothing existing) gets optional topics as suggested default
  assumptions, not questions. The model words the questions and may add its own.
- **Alternatives:** leave question selection entirely to the model (not testable, and the
  owner asked for rules that are "explicit and testable"); a fixed questionnaire (the
  owner ruled it out: a storage account must not get a landing-zone interview).
- **Trade-off:** the catalog is a starting set of about forty topics, not complete Azure
  knowledge. Questions the model adds are recorded but not enforced unless it marks them
  must-confirm.
- **Status:** DEFAULT.

## ADR-016 Decisions that are never guessed

- **Decision:** five topics are must-confirm: target subscription, network address space,
  production sizing, public exposure, and destructive scope. For a profile they apply to,
  the workflow cannot leave DISCOVERY until each has a requirement recorded as the user's
  answer. The tools refuse to defer such a question or to record an assumption under such
  a topic. The model can also mark its own question must-confirm.
- **Alternatives:** a prompt rule only.
- **Trade-off:** the tool cannot tell whether the recorded answer came from the user; it
  only prevents the "assumption" path. Which topics apply depends on the profile the model
  recorded, so a wrong profile weakens the guard; the profile is part of the approved
  content (ADR-008), so the user sees it in the proposal.
- **Status:** DEFAULT. The list follows the owner's brief.

## ADR-017 Assumptions review and proposal completeness

- **Decision:** leaving DISCOVERY with open assumptions needs a review record whose hash
  equals the hash of the current assumption list (`review-assumptions --confirm`). Leaving
  ARCHITECTURE needs a proposal with every required section (`discovery/proposal.py`);
  cost needs an estimate or the limitations that prevent one. `discovery/cli.py proposal`
  renders the stored proposal with confirmed requirements and assumptions in separate
  sections and the approval hash. The request profile is added to the content the
  architecture approval covers.
- **Alternatives:** free-form proposals; rendering by the model.
- **Trade-off:** section presence is checked, not quality. Adding the profile changes the
  approval hash, so an architecture approval made with 0.1.0 is invalid under 0.2.0.
- **Status:** DEFAULT.

## ADR-018 Required permission rules, checked by the tools

- **Decision (owner, 2026-10-04):** the `ask` rules are required, not optional.
  `tools/setup/permissions.py` reads the user, project and local settings files and
  reports missing rules, `allow` rules that match the same commands, and
  `bypassPermissions` as default mode. `tools/state/cli.py` refuses `approve --kind
  deployment`, `confirm-risk` and `advance --to DEPLOYMENT` unless the check passes.
- **Alternatives:** documentation only (the 0.1.0 position); gating architecture approval
  as well (rejected: it would block all use before the user edits settings, and no Azure
  change follows from an architecture approval).
- **Trade-off:** the check compares rule text with sample commands using glob matching.
  It does not prove that Claude Code prompts. Managed settings are not read.
- **Correction:** the rules printed in the 0.1.0 README (`Bash(*tools/state/cli.py approve*)`)
  do not match when the script path is quoted, which is how the agent runs it. The
  required rules use `cli.py* approve *`. A test covers both forms.
- **UNVERIFIED:** wildcard matching of `Bash(...)` rules in the middle of a command, and
  prompting in auto mode, in a live session.
- **Status:** ACCEPTED.

## ADR-019 Repository inspection through `gh`, metadata only

- **Decision:** `tools/repo/cli.py inspect` makes two GET calls with the user's `gh` login
  (`repos/<slug>` and the recursive git tree of the default branch). It returns access,
  default branch, push permission, archived state, Bicep and parameter files, workflows,
  READMEs, counts of other infrastructure-as-code, candidate infrastructure directories,
  and a proposed layout when there is no Bicep. It saves nothing. Failures are classified
  (gh missing, not authenticated, not found or no access, forbidden, rate limited,
  network) and exit 5 or 1; none returns `verified`.
- **Alternatives:** cloning (needs a working copy, which is Milestone 3); the GitHub MCP
  connector (not available to a plugin agent by default); a token in config (forbidden).
- **Trade-off:** file names only, so conventions inside files are not seen until
  Milestone 3. GitHub does not distinguish a missing repository from one the login cannot
  see; the tool says so.
- **Status:** DEFAULT.

## ADR-020 First repository setting prompts; agent model inherits

- **Decision:** the hook now answers every `config/cli.py set-repo` with `ask`, not only a
  switch: establishing the source of truth needs the user's confirmation. The agent's
  `model` is `inherit` and its tools gain `Skill` (replaces ADR-012's `opus`).
- **Status:** ACCEPTED (owner, 2026-10-04).

## Owner decisions recorded on 2026-10-04

- ADR-011 session-wide shell guard: ACCEPTED as is. Enable the plugin per project.
- ADR-003 store location and ADR-004 project identity by path: kept.
- GitHub Enterprise Server: out of scope. Auth methods: as in ADR/config schema.
- Discovery strictness (open questions answered or deferred before ARCHITECTURE): kept.

## Open

- Live-session behaviour of the hook, the skills and the `ask` rules (not yet exercised).
