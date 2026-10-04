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

## Open

- ADR-004 project identity by path versus remote URL.
- ADR-003 store location versus `CLAUDE_PLUGIN_DATA`.
- ADR-011 session-wide shell guard versus a narrower scope.
