# Security model

What protects the user's Azure estate, repository and secrets as of Milestone 4, what each layer
guarantees, and what it does not. Decisions and sources: [decisions.md](decisions.md).

## Threats

1. The model acts without the user's approval (misunderstanding, or instructions hidden
   in repository files or tool output).
2. An approval is reused for content the user did not see.
3. Azure is changed directly from the shell, skipping the workflow.
4. A secret is stored in config, state, logs, Bicep or git history.
5. Workflow records are edited directly to fake an approval or hide a failure.
6. The model guesses a decision that was the user's to make (subscription, address range,
   production size, public exposure, deletion) and builds on the guess.
7. Text from a repository (file names, branch names) steers the model.
8. A check that could not be made is reported as passed.
9. The agent changes files it has no business changing (application code, workflows,
   git metadata, the user's settings), or discards someone's uncommitted work.
10. Insecure or secret-bearing Bicep is written and carried forward.
11. Unvalidated or unreviewed code reaches the repository's default branch, other
    people's commits are overwritten, or a push is reported that did not happen.

## Layers

| # | Layer | Enforced by | Guarantees | Does not guarantee |
| --- | --- | --- | --- | --- |
| 1 | Workflow rules in `tools/state/` | Python code, every call | No move past APPROVAL or DEPLOYMENT_APPROVAL without an approval whose hash matches the current content; content changes invalidate approvals and move the workflow back; high-risk plans need a second, distinct confirmation; skipped or unavailable checks are never counted as passes; refusals exit 2 and save nothing | That the human, not the model, decided. The model runs the CLI and can pass the hash it was shown. |
| 2 | Shell guard hook (`hooks/azure_guard.py`) | Claude Code `PreToolUse`, exit 2 | Blocks the command text Claude writes when it calls Azure CLI or Azure PowerShell to change resources, calls ARM directly, publishes Bicep modules, runs encoded PowerShell, prints tokens or keys, or touches the store directly. Unknown `az` commands are blocked. Errors inside the hook block. | Commands it cannot see: variables (`$AZ group delete`), aliases, `eval`, scripts written to disk and then run, SDK calls from Python or Node, other shells' quoting tricks. Does not run, so does not block, if `python3` is missing or the hook times out. A mod handling `tool.check` can override it. |
| 3 | User prompt on approval commands | The user's own `ask` permission rules (required; `tools/setup/permissions.py` checks them and `tools/state/cli.py` refuses deployment approval, high-risk confirmation and the move to DEPLOYMENT without them), plus the hook's `permissionDecision: "ask"` | Claude Code shows the user the approval, high-risk, repository set/switch or clear command and waits for a yes. An `allow` rule that would swallow these commands, or `bypassPermissions` as default mode, makes the check fail. | The check matches rule text against sample commands; it does not prove Claude Code prompts in every mode (not tested in a live session). Managed settings are not read. Architecture approval is not gated on the rules. |
| 4 | Agent instructions (`agents/iac-azure-agent.md`) | The model | Approval only on explicit user consent in the conversation; untrusted-content rule; no secrets; no direct Azure changes; stop conditions. | Prompt rules are a mitigation, not a control. |
| 5 | Tool budget | Agent frontmatter `tools` | The agent has Read, Grep, Glob, Bash, Skill, Write and Edit: no web or MCP tools. Write and Edit are narrowed by layer 10. | Bash is broad; layer 2 narrows only its Azure surface. |
| 6 | Secret guard (`tools/lib/secret_guard.py`) | Python code on every write | Secret-shaped keys and values never reach config or records; error messages do not echo them. | Secrets without a recognisable shape (see "Secret guard"). Nothing outside the two stores (Bicep and git are Milestones 3-4). |
| 7 | Storage hygiene (`tools/lib/storage.py`) | Python code | Files 0600, directories 0700, atomic replace, read-back check, corrupt files refused and left in place, concurrent edits detected by revision. | Protection from the user's own account or from root. Locking between simultaneous writers. |

Plugins cannot ship permission rules (ADR-001). The user adds the `ask` rules (README,
"Required permission rules"); the tools check for them (ADR-018).

| # | Layer (Milestone 2) | Enforced by | Guarantees | Does not guarantee |
| --- | --- | --- | --- | --- |
| 8 | Discovery rules in `tools/state/machine.py` | Python code | No ARCHITECTURE without a profile, the user's answer to every must-confirm topic for that profile, and a review bound to the current assumptions; must-confirm topics and questions cannot be recorded as assumptions; no APPROVAL without every proposal section | That the recorded answer is what the user said: the model writes the text. That the catalog covers every decision an architecture needs. |
| 9 | Read-only repository inspection (`tools/repo/`) | Python code | Two GET calls through `gh`; no file contents read; nothing written; names from the repository are length-capped and stripped of control and bidirectional characters; every failure is classified and exits non-zero without `verified` | That a file name cannot mislead the model: the untrusted-data rule is a prompt rule. Completeness on a truncated listing (reported). |

| # | Layer (Milestone 3) | Enforced by | Guarantees | Does not guarantee |
| --- | --- | --- | --- | --- |
| 10 | Write limits (`hooks/azure_guard.py`) | Claude Code `PreToolUse`, exit 2 | For every session: inside a working copy, Write and Edit succeed only under the configured infrastructure root, never under `.git`, with symlinks resolved; an unreadable config or unset root blocks. For the iac-azure-agent agent (identified by `agent_type`): no Write or Edit anywhere else at all. | Files written by a shell command. That `agent_type` is present and correct in every Claude Code version (documented; not exercised in a live session). |
| 11 | git guard (same hook) | Claude Code `PreToolUse`, exit 2 | In a working copy (by path or current directory), and everywhere for the agent, only read-only git subcommands pass: no commit, push, reset, clean, checkout, branch changes, remote or config changes. | git reached through a variable, alias or script. |
| 12 | Working-copy tool (`tools/workspace/`) | Python code | The clone must point at the configured repository or every operation is refused; no reset, clean or forced checkout exists in the code; a dirty tree or a non-fast-forward is refused and left as it is; prompts are disabled so a missing credential fails instead of asking; no token is read or stored. | Protection of work the user deletes themselves. |
| 13 | Recorded files (`workspace/cli.py record-files`) | Python code | The request's file list comes from git, not from the model; any changed path outside the infrastructure root refuses the whole operation. This is what catches a shell-written file that layer 10 could not see. | Detection before the file is written. |
| 14 | Validation (`tools/validate/`) | Python code, Bicep CLI, Checkov | Each check is passed, failed, warning, skipped or unavailable; a missing or crashing tool is never a pass; results carry a hash of the files checked; leaving VALIDATION with a failure is refused, and with skipped or unavailable checks needs recorded acceptance; secret-shaped values in code fail the run without being echoed; suppressed findings show as warnings. | That passing code is secure or deployable: static checks only. Coverage beyond what Bicep's linter and Checkov implement. |

| # | Layer (Milestone 4) | Enforced by | Guarantees | Does not guarantee |
| --- | --- | --- | --- | --- |
| 15 | Publish preflight (`tools/github/publisher.py`) | Python code | Nothing is committed unless the request is in GIT_REVIEW, the working copy is the configured repository on `iac/<id>`, every change is under the infrastructure root and equals the recorded files, the validation run's hash equals the current files, the verdict is not failed or none, and any skipped or unavailable check was accepted. The required permission rules must be present. | That validation was meaningful beyond what the checks cover. |
| 16 | Publish mechanics | Python code | Stages only the infrastructure root. Pushes one refspec, `iac/<id>`, never with force. Refuses to target the default branch. Has no merge, delete or rebase. A rejected push is reported as someone else's change. | Server-side rules: branch protection and required reviews are the repository owner's. |
| 17 | Read-back | Python code | After the push, `git ls-remote` must show the commit or the result is an error. After creating the pull request it is fetched again; its address must belong to the configured repository. Each completed stage is recorded before the next starts, and a failure message lists what was verified. | That GitHub's state stays that way afterwards; `verify` re-reads it on request. |
| 18 | `gh` limits for the agent (hook) | Claude Code `PreToolUse`, exit 2 | When `agent_type` names this agent: only read-only `gh` commands pass; `gh pr create/merge/close`, repository changes, secrets, workflow runs, `gh auth token` and `gh api` with a write method or fields are blocked. | Other sessions (by design). `gh` reached through a variable or script. |
| 19 | Accepted findings | Python code, `ask` rule, hook | A finding is accepted only through the config with a reason of at least ten characters; the command prompts the user; the finding then shows as accepted with its reason, result `warning`. | That the reason is a good one. |

## Secret guard

Rejected key names (case and separators ignored): anything containing password, passwd,
passphrase, secret, token, apikey, accesskey, accountkey, sharedaccesskey, privatekey,
connectionstring, connstr, credential, sastoken, sasurl, pfx; and exactly pwd, pat, sas,
key, sig, signature, cert, certificate.

Rejected value shapes: PEM `-----BEGIN ...-----`, `PRIVATE KEY`, GitHub tokens (`ghp_`,
`gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), JWT-shaped `eyJ...eyJ...`, 86 base64
characters plus `==` (storage account keys), `AccountKey=` / `SharedAccessKey=` /
`SharedAccessSignature=` / `Password=` / `Pwd=` in connection strings, SAS `sig=`, URLs
with `user:password@`, and the Entra client secret shape (heuristic).

Allowed on purpose: tenant and subscription GUIDs, resource IDs, git SHAs, sha256 hashes,
GitHub URLs without credentials, Key Vault names and secret names.

Not caught: a secret with no recognisable shape, encoded or split secrets, secrets in
places the tools never write.

## What a reviewer should check in later milestones

- Every tool that writes to GitHub or Azure loads the request record and refuses without
  a valid approval for the exact target and change set it is about to apply.
- The deploy tool will call Azure from inside its own process, where the hook cannot see
  it. Its own approval check is therefore the only gate on that path and needs tests that
  prove it refuses without a valid approval.
- No command ever passes a secret on the command line.
