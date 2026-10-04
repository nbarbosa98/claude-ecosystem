# Security model

What protects the user's Azure estate and secrets as of Milestone 2, what each layer
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

## Layers

| # | Layer | Enforced by | Guarantees | Does not guarantee |
| --- | --- | --- | --- | --- |
| 1 | Workflow rules in `tools/state/` | Python code, every call | No move past APPROVAL or DEPLOYMENT_APPROVAL without an approval whose hash matches the current content; content changes invalidate approvals and move the workflow back; high-risk plans need a second, distinct confirmation; skipped or unavailable checks are never counted as passes; refusals exit 2 and save nothing | That the human, not the model, decided. The model runs the CLI and can pass the hash it was shown. |
| 2 | Shell guard hook (`hooks/azure_guard.py`) | Claude Code `PreToolUse`, exit 2 | Blocks the command text Claude writes when it calls Azure CLI or Azure PowerShell to change resources, calls ARM directly, publishes Bicep modules, runs encoded PowerShell, prints tokens or keys, or touches the store directly. Unknown `az` commands are blocked. Errors inside the hook block. | Commands it cannot see: variables (`$AZ group delete`), aliases, `eval`, scripts written to disk and then run, SDK calls from Python or Node, other shells' quoting tricks. Does not run, so does not block, if `python3` is missing or the hook times out. A mod handling `tool.check` can override it. |
| 3 | User prompt on approval commands | The user's own `ask` permission rules (required; `tools/setup/permissions.py` checks them and `tools/state/cli.py` refuses deployment approval, high-risk confirmation and the move to DEPLOYMENT without them), plus the hook's `permissionDecision: "ask"` | Claude Code shows the user the approval, high-risk, repository set/switch or clear command and waits for a yes. An `allow` rule that would swallow these commands, or `bypassPermissions` as default mode, makes the check fail. | The check matches rule text against sample commands; it does not prove Claude Code prompts in every mode (not tested in a live session). Managed settings are not read. Architecture approval is not gated on the rules. |
| 4 | Agent instructions (`agents/iac-azure-agent.md`) | The model | Approval only on explicit user consent in the conversation; untrusted-content rule; no secrets; no direct Azure changes; stop conditions. | Prompt rules are a mitigation, not a control. |
| 5 | Tool budget | Agent frontmatter `tools` | The agent has Read, Grep, Glob, Bash and Skill only: no Write, Edit, web or MCP tools. | Bash is broad; layer 2 narrows only its Azure surface. |
| 6 | Secret guard (`tools/lib/secret_guard.py`) | Python code on every write | Secret-shaped keys and values never reach config or records; error messages do not echo them. | Secrets without a recognisable shape (see "Secret guard"). Nothing outside the two stores (Bicep and git are Milestones 3-4). |
| 7 | Storage hygiene (`tools/lib/storage.py`) | Python code | Files 0600, directories 0700, atomic replace, read-back check, corrupt files refused and left in place, concurrent edits detected by revision. | Protection from the user's own account or from root. Locking between simultaneous writers. |

Plugins cannot ship permission rules (ADR-001). The user adds the `ask` rules (README,
"Required permission rules"); the tools check for them (ADR-018).

| # | Layer (Milestone 2) | Enforced by | Guarantees | Does not guarantee |
| --- | --- | --- | --- | --- |
| 8 | Discovery rules in `tools/state/machine.py` | Python code | No ARCHITECTURE without a profile, the user's answer to every must-confirm topic for that profile, and a review bound to the current assumptions; must-confirm topics and questions cannot be recorded as assumptions; no APPROVAL without every proposal section | That the recorded answer is what the user said: the model writes the text. That the catalog covers every decision an architecture needs. |
| 9 | Read-only repository inspection (`tools/repo/`) | Python code | Two GET calls through `gh`; no file contents read; nothing written; names from the repository are length-capped and stripped of control and bidirectional characters; every failure is classified and exits non-zero without `verified` | That a file name cannot mislead the model: the untrusted-data rule is a prompt rule. Completeness on a truncated listing (reported). |

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
